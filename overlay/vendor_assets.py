"""Self-host every front-end library, each at a version with no known advisory.

    python overlay/vendor_assets.py <upstream-checkout> [--notes FILE]

Upstream loads Prism, xterm, Mermaid and PDF.js from cdn.jsdelivr.net at
runtime, and vendors js-yaml, KaTeX and streaming-markdown under static/vendor/.
Two problems for an air-gapped install:

* CDN libraries never load there: no syntax highlighting, terminal, diagrams or
  PDF preview. Each is fetched from the npm registry at build time — tarball
  checked against the registry's sha512 integrity — and served from the wheel.

* Pinned versions collect advisories (js-yaml 4.1.0, Mermaid 10.9.3 and Prism
  1.29.0 had them at exp-v0.52.416). Each library is moved to the LOWEST
  semver-compatible release that has no OSV advisory: the smallest change
  that is clean, and no API break (exceptions: ALLOW_BREAKING).

npm package layout is kept for CDN libraries (`<pkg>/<version>/<path in
package>`), so a URL maps one-to-one and Prism's autoloader finds its language
components next to itself. Already-vendored libraries keep upstream's layout;
their files are looked up in the new tarball under `dist/` or the package root.

A prebuilt bundle can embed a vulnerable copy of a dependency that no release
of the library fixes: every Mermaid release up to 12.1.0 embeds a DOMPurify
with open advisories. Libraries in REBUILD are therefore rebuilt here from the
npm package with esbuild, every package in their resolved tree checked against
OSV, and any vulnerable one overridden to its lowest clean semver-compatible
release. Needs Node and npm (HORO_NPM, default `npm`).

Fails closed: an unparseable reference, a library with no clean semver-compatible
release, a missing file in the new tarball, or a CDN URL left in the front end
stops the build.
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

from packaging.version import InvalidVersion, Version

ROOT = Path(sys.argv[1]).resolve()
NOTES = Path(sys.argv[sys.argv.index("--notes") + 1]) if "--notes" in sys.argv else None
STATIC = ROOT / "static"
VENDOR = STATIC / "vendor"

CDN = re.compile(r"https://cdn\.jsdelivr\.net/npm/"
                 r"(?P<name>(?:@[\w.-]+/)?[\w.-]+)@(?P<ver>\d+\.\d+\.\d+)/(?P<path>[\w./-]+)")
VENDORED = re.compile(r"static/vendor/(?P<name>[\w.-]+)/(?P<ver>\d+\.\d+\.\d+)/")
# Unversioned single files: path → (npm name, where upstream records the version).
SINGLE = {"smd.min.js": ("streaming-markdown", "index.html", r"npm:streaming-markdown@(\d+\.\d+\.\d+)")}
# Whole directories a library loads by itself, not named in any URL.
# Prism's autoloader fetches components/prism-<lang>.min.js on demand.
EXTRA = {"prismjs": ["components/"]}
# Bundles rebuilt from source: package → (bundle path in package, global name).
REBUILD = {"mermaid": ("dist/mermaid.min.js", "mermaid")}
ESBUILD = "0.28.2"
NPM = shlex.split(os.environ.get("HORO_NPM", "npm"))

failures: list[str] = []
notes: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"[vendor] FAIL {msg}")


def get_json(url: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode() if payload else None,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def advisories(name: str, version: str) -> list[str]:
    res = get_json("https://api.osv.dev/v1/query",
                   {"package": {"name": name, "ecosystem": "npm"}, "version": version})
    return [v["id"] for v in res.get("vulns", []) if not v.get("withdrawn")]


# Semver-compatible means same major, or same minor below 1.0. Crossing that
# line can break callers, so it is allowed only for a library listed here, with
# the version line it may move to — each entry checked against how upstream
# uses the library and exercised in a browser before being added.
#   katex 0.16 → 0.18: GHSA-238p-pmpm-9mq7 is fixed only in 0.18.2. Upstream
#   calls katex.render(src, el, {displayMode, throwOnError, trust, strict}),
#   unchanged; 0.18 prefixes internal CSS classes, but the .katex,
#   .katex-display and .katex-html that style.css targets keep their names.
ALLOW_BREAKING = {"katex": "0.18"}


def compatible(name: str, cur: Version, pv: Version) -> bool:
    if pv.major == cur.major and (cur.major > 0 or pv.minor == cur.minor):
        return True
    allowed = ALLOW_BREAKING.get(name)
    return allowed is not None and f"{pv.major}.{pv.minor}" == allowed


def clean_version(name: str, current: str) -> tuple[str | None, list[str]]:
    """Lowest semver-compatible release >= current with no advisory."""
    found = advisories(name, current)
    if not found:
        return current, []
    cur = Version(current)
    versions = []
    for v in get_json(f"https://registry.npmjs.org/{name}")["versions"]:
        try:
            pv = Version(v)
        except InvalidVersion:
            continue
        if not pv.is_prerelease and pv > cur and compatible(name, cur, pv):
            versions.append(pv)
    for pv in sorted(versions):
        if not advisories(name, str(pv)):
            return str(pv), found
    return None, found


def osv_batch(pkgs: list[tuple[str, str]]) -> dict[tuple[str, str], list[str]]:
    hits = {}
    for i in range(0, len(pkgs), 500):
        chunk = pkgs[i:i + 500]
        res = get_json("https://api.osv.dev/v1/querybatch", {"queries": [
            {"package": {"name": n, "ecosystem": "npm"}, "version": v} for n, v in chunk]})
        for key, r in zip(chunk, res["results"]):
            ids = [x["id"] for x in r.get("vulns", []) if not x.get("withdrawn")]
            if ids:
                hits[key] = ids
    return hits


def rebuild(name: str, version: str, bundle: str, global_name: str, dest: Path) -> list[str]:
    """Bundle name@version with esbuild after clearing its tree of advisories.

    Returns the overrides applied, as notes. Raises RuntimeError when an
    advisory has no clean semver-compatible release.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        (work / "entry.mjs").write_text(
            f'import lib from "{name}";\nglobalThis.{global_name} = lib;\n', encoding="utf-8")
        overrides: dict[str, str] = {}
        applied: list[str] = []
        for _ in range(5):
            (work / "package.json").write_text(json.dumps({
                "private": True, "dependencies": {name: version, "esbuild": ESBUILD},
                "overrides": overrides}, indent=1), encoding="utf-8")
            r = subprocess.run([*NPM, "install", "--ignore-scripts", "--no-audit", "--no-fund"],
                               cwd=work, capture_output=True, text=True)
            if r.returncode:
                raise RuntimeError(f"npm install failed: {r.stderr[-400:]}")
            lock = json.loads((work / "package-lock.json").read_text(encoding="utf-8"))
            tree = sorted({(path.rsplit("node_modules/", 1)[-1], meta["version"])
                           for path, meta in lock["packages"].items()
                           if path and "version" in meta and not meta.get("dev")
                           and "esbuild" not in path})
            hits = osv_batch(tree)
            if not hits:
                break
            for (dep, ver), ids in hits.items():
                new, _ = clean_version(dep, ver)
                if new is None:
                    raise RuntimeError(f"bundled {dep}@{ver}: {ids} and no clean semver-compatible release")
                overrides[dep] = new
                applied.append(f"{dep} {ver} → {new} (fixes {', '.join(ids)})")
        else:
            raise RuntimeError(f"tree still has advisories after overrides: {hits}")
        r = subprocess.run([str(work / "node_modules" / ".bin" / "esbuild"), "entry.mjs", "--bundle",
                            "--minify", "--format=iife", "--platform=browser", "--target=es2020",
                            "--legal-comments=inline", f"--outfile={dest}"],
                           cwd=work, capture_output=True, text=True)
        if r.returncode:
            raise RuntimeError(f"esbuild failed: {r.stderr[-400:]}")
        print(f"[vendor] ok   {name}@{version}: rebuilt, {len(tree)} bundled packages, no advisory")
        return applied


def tarball(name: str, version: str) -> tarfile.TarFile:
    dist = get_json(f"https://registry.npmjs.org/{name}/{version}")["dist"]
    with urllib.request.urlopen(dist["tarball"], timeout=300) as r:
        data = r.read()
    algo, _, want = dist["integrity"].partition("-")
    if algo != "sha512" or base64.b64encode(hashlib.sha512(data).digest()).decode() != want:
        raise RuntimeError(f"{name}@{version}: tarball does not match registry integrity")
    return tarfile.open(fileobj=io.BytesIO(data), mode="r:gz")


def extract(tf: tarfile.TarFile, members: dict[str, Path]) -> None:
    """members: path inside package/ → destination file."""
    for inner, dest in members.items():
        m = tf.getmember(f"package/{inner}")
        if not m.isfile():
            raise KeyError(inner)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(tf.extractfile(m).read())


def front_end_files() -> list[Path]:
    return [p for p in STATIC.rglob("*") if p.suffix in (".html", ".js", ".css")
            and VENDOR not in p.parents]


def rewrite(old: str, new: str) -> int:
    n = 0
    for p in front_end_files():
        text = p.read_text(encoding="utf-8")
        if old in text:
            n += text.count(old)
            p.write_text(text.replace(old, new), encoding="utf-8")
    return n


def record(name: str, old: str, new: str, ids: list[str], how: str) -> None:
    fixed = f"{old} → {new}, fixes {', '.join(ids)}" if ids else f"{old}, no advisory"
    notes.append(f"- {name}: {fixed} ({how})")
    print(f"[vendor] ok   {name}: {fixed} ({how})")


# ── 1. CDN libraries → served from the wheel ─────────────────────────────
cdn: dict[tuple[str, str], set[str]] = {}
self_hosted: set[Path] = set()
for p in front_end_files():
    for m in CDN.finditer(p.read_text(encoding="utf-8")):
        cdn.setdefault((m["name"], m["ver"]), set()).add(m["path"])

for (name, ver), paths in sorted(cdn.items()):
    new, ids = clean_version(name, ver)
    if new is None:
        fail(f"{name}@{ver}: {len(ids)} advisories and no clean semver-compatible release")
        continue
    dest_root = VENDOR / name / new
    try:
        tf = tarball(name, new)
        want = {path: dest_root / path for path in paths}
        for prefix in EXTRA.get(name, []):
            for m in tf.getmembers():
                inner = m.name.removeprefix("package/")
                if m.isfile() and inner.startswith(prefix):
                    want[inner] = dest_root / inner
        extract(tf, want)
        how = f"self-hosted, {len(want)} files"
        if name in REBUILD:
            bundle, global_name = REBUILD[name]
            if bundle not in paths:
                raise KeyError(f"{bundle} is no longer what upstream loads ({sorted(paths)})")
            applied = rebuild(name, new, bundle, global_name, dest_root / bundle)
            how = "rebuilt with esbuild" + (f"; bundled {'; '.join(applied)}" if applied else "")
    except (KeyError, RuntimeError, OSError) as exc:
        fail(f"{name}@{new}: {exc}")
        continue
    self_hosted.add(dest_root)
    n = rewrite(f"https://cdn.jsdelivr.net/npm/{name}@{ver}/", f"static/vendor/{name}/{new}/")
    record(name, ver, new, ids, f"{how}, {n} references")

# ── 2. Already-vendored libraries ────────────────────────────────────────
for d in sorted(VENDOR.glob("*/*")):
    if d in self_hosted or not d.is_dir() or not VENDORED.fullmatch(f"static/vendor/{d.parent.name}/{d.name}/"):
        continue
    name, ver = d.parent.name, d.name
    new, ids = clean_version(name, ver)
    if new is None:
        fail(f"{name}@{ver}: {len(ids)} advisories and no clean semver-compatible release")
        continue
    if new == ver:
        record(name, ver, ver, [], "vendored upstream")
        continue
    dest_root = VENDOR / name / new
    try:
        tf = tarball(name, new)
        names = {m.name.removeprefix("package/") for m in tf.getmembers() if m.isfile()}
        want = {}
        for f in (p for p in d.rglob("*") if p.is_file()):
            rel = f.relative_to(d).as_posix()
            inner = next((c for c in (f"dist/{rel}", rel) if c in names), None)
            if inner is None:
                raise KeyError(f"{rel} not in {name}@{new}")
            want[inner] = dest_root / rel
        extract(tf, want)
    except (KeyError, RuntimeError, OSError) as exc:
        fail(f"{name}@{new}: {exc}")
        continue
    shutil.rmtree(d)
    n = rewrite(f"static/vendor/{name}/{ver}/", f"static/vendor/{name}/{new}/")
    record(name, ver, new, ids, f"replaced in static/vendor, {n} references")

for fname, (name, src, pattern) in SINGLE.items():
    m = re.search(pattern, (STATIC / src).read_text(encoding="utf-8"))
    if not m or not (VENDOR / fname).is_file():
        fail(f"{fname}: cannot find the vendored file or its recorded version in {src}")
        continue
    ids = advisories(name, m[1])
    if ids:
        fail(f"{name}@{m[1]} ({fname}): advisories {ids} — not handled automatically")
    else:
        record(name, m[1], m[1], [], "vendored upstream")

# ── 3. Subresource integrity for the rewritten tags ──────────────────────
index = STATIC / "index.html"
html = index.read_text(encoding="utf-8")


def resri(m: re.Match) -> str:
    f = STATIC.parent / m["url"]
    if not f.is_file():
        fail(f"index.html references missing {m['url']}")
        return m[0]
    digest = base64.b64encode(hashlib.sha384(f.read_bytes()).digest()).decode()
    return m[0].replace(m["sri"], f"sha384-{digest}")


html = re.sub(r'(?:src|href)="(?P<url>static/vendor/[^"]+)"[^>]*?integrity="(?P<sri>sha384-[^"]+)"', resri, html)
index.write_text(html, encoding="utf-8")

# ── Postconditions ───────────────────────────────────────────────────────
for p in front_end_files():
    left = CDN.findall(p.read_text(encoding="utf-8"))
    if left:
        fail(f"{p.relative_to(ROOT)} still loads from the CDN: {left[:3]}")
for p in front_end_files():
    for m in VENDORED.finditer(p.read_text(encoding="utf-8")):
        if not (VENDOR / m["name"] / m["ver"]).is_dir():
            fail(f"{p.relative_to(ROOT)} references absent static/vendor/{m['name']}/{m['ver']}/")

if NOTES:
    with NOTES.open("a", encoding="utf-8") as f:
        f.write("\n## Front-end libraries (served from the wheel)\n\n" + "\n".join(notes) + "\n")
if failures:
    sys.exit(f"[vendor] {len(failures)} problem(s) — release held")
print("[vendor] done")
