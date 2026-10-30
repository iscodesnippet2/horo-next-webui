"""Software bill of materials and third-party notices for a horo-next-webui release.

    python overlay/sbom.py generate <checkout> <version> <upstream-tag> \\
        --requirements requirements-X.txt --out horo-next-webui.cdx.json
    python overlay/sbom.py embed <wheel> <sbom> --release-copy <out.json>

Review systems (Sonatype, Dependency-Track, Black Duck) identify a package's
contents by component coordinates. For the Python dependencies that works from
the wheel metadata alone. It does not for the front end: static/vendor holds
minified JavaScript with no package.json, and Mermaid is rebuilt here, so a
scanner sees nothing or unidentified code. This SBOM names every npm package
that ships — vendored, or inside a bundle according to esbuild's metafile
(horo-vendor.json, written by vendor_assets.py) — with its license and the
integrity hash npm verifies.

`generate` (before the wheel is built) writes the CycloneDX 1.5 SBOM and
THIRD_PARTY_LICENSES, the license texts minification strips from the code, and
adds the latter to the wheel's license files. `embed` (after) puts the SBOM in
the wheel at .dist-info/sboms/ (PEP 770) and writes a copy for the release page
that also carries the finished wheel's own hash.

Python dependencies are not bundled: pip installs each with its own license
files, so they are listed with their license but no notice text.

Fails closed: a component with no license, no hash or no registry entry stops
the build. A blank field would pass a scanner by saying nothing.
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as cf
import csv
import datetime as dt
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import urllib.request
import uuid
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sonatype  # noqa: E402

HERE = Path(__file__).resolve().parent
REPO = "https://github.com/iscodesnippet2/horo-next-webui"
UPSTREAM = "https://github.com/nesquena/hermes-webui"
DIST = "horo-next-webui"
NOTICES = "THIRD_PARTY_LICENSES"
LICENSE_FILES = re.compile(r"^(licen[cs]e|copying|notice)(\.|-|$)", re.I)
REGISTRY = os.environ.get("HORO_NPM_REGISTRY", "https://registry.npmjs.org")
PYPI = os.environ.get("HORO_PYPI_URL", "https://pypi.org")
REQ = re.compile(r"^\s*([A-Za-z0-9_.\-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;]+)")

# Where a package's npm metadata understates what ships. KaTeX declares MIT, and
# its code is; its fonts say otherwise in their own name tables (OFL-1.1, which
# requires its text to travel with them), and static/vendor ships the fonts.
VENDORED_LICENSE = {"katex": ("MIT AND OFL-1.1", "katex-fonts-OFL-1.1.txt")}
# Packages whose npm metadata omits the license field, by the license text the
# tarball carries. Applied only while the field is blank and that text matches.
UNDECLARED_LICENSE = {"khroma": ("MIT", "The MIT License")}


def die(msg: str) -> None:
    sys.exit(f"[sbom] {msg}")


def get_json(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=120) as r:
        return json.load(r)


# ── npm ─────────────────────────────────────────────────────────────────────
def npm_component(item: dict) -> tuple[dict, list[tuple[str, str]]]:
    """(component, [(file name, text), ...]) from the registry's tarball, whose
    integrity is checked: license texts are copied from what was verified."""
    name, version, ships = item["name"], item["version"], item["ships"]
    doc = get_json(f"{REGISTRY}/{name}/{version}")
    lic = doc.get("license")
    lic = lic.get("type") if isinstance(lic, dict) else lic
    dist = doc["dist"]
    with urllib.request.urlopen(dist["tarball"], timeout=300) as r:
        data = r.read()
    algo, _, want = dist["integrity"].partition("-")
    digest = hashlib.sha512(data).digest()
    if algo != "sha512" or base64.b64encode(digest).decode() != want:
        die(f"{name}@{version}: tarball does not match registry integrity")
    texts = []
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            parts = m.name.split("/")
            if m.isfile() and len(parts) == 2 and LICENSE_FILES.match(parts[1]):
                texts.append((parts[1], tf.extractfile(m).read().decode("utf-8", "replace")))
    if not lic and name in UNDECLARED_LICENSE:
        spdx, marker = UNDECLARED_LICENSE[name]
        lic = spdx if any(marker in text for _, text in texts) else None
    if name in VENDORED_LICENSE and "static/vendor" in ships:
        lic, extra = VENDORED_LICENSE[name]
        texts.append((extra, (HERE / "notices" / extra).read_text(encoding="utf-8")))
    purl = sonatype.npm_purl(name, version)
    return {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl,
            "scope": "required",
            "licenses": [{"expression": lic}] if lic else [],
            "hashes": [{"alg": "SHA-512", "content": digest.hex()}],
            "externalReferences": [{"type": "distribution", "url": dist["tarball"]}],
            "properties": [{"name": "horo:ships", "value": s} for s in ships]}, sorted(texts)


# ── Python ──────────────────────────────────────────────────────────────────
def read_reqs(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        m = REQ.match(raw)
        if m:
            out[re.sub(r"[-_.]+", "-", m[1]).lower()] = m[2]
    return out


def python_component(name: str, version: str) -> dict:
    doc = get_json(f"{PYPI}/pypi/{name}/{version}/json")
    info = doc["info"]
    if info.get("license_expression"):
        lic = [{"expression": info["license_expression"]}]
    elif (info.get("license") or "").strip() and len(info["license"]) <= 80 and "\n" not in info["license"]:
        lic = [{"license": {"name": info["license"].strip()}}]
    else:
        lic = [{"license": {"name": c.split(" :: ")[-1]}} for c in info.get("classifiers", [])
               if c.startswith("License :: ")]
    purl = sonatype.pypi_purl(name, version)
    # Which wheel installs depends on the platform, so each file is listed with
    # its own hash rather than one being picked to stand for the component.
    return {"type": "library", "bom-ref": purl, "name": name, "version": version, "purl": purl,
            "scope": "required", "licenses": lic,
            "externalReferences": [{"type": "distribution", "url": f["url"],
                                    "hashes": [{"alg": "SHA-256", "content": f["digests"]["sha256"]}]}
                                   for f in doc["urls"]]}


# ── generate / embed ────────────────────────────────────────────────────────
def generate(a) -> int:
    root = a.checkout.resolve()
    manifest = json.loads((root / "horo-vendor.json").read_text(encoding="utf-8"))
    reqs = read_reqs(a.requirements)
    if not manifest or not reqs:
        die("no npm or no Python components — refusing to write an empty SBOM")
    with cf.ThreadPoolExecutor(8) as pool:
        npm = list(pool.map(npm_component, manifest))
        py = list(pool.map(lambda kv: python_component(*kv), sorted(reqs.items())))

    bad = [c["purl"] for c, _ in npm if not c["licenses"]] + [c["purl"] for c in py if not c["licenses"]]
    if bad:
        die(f"components without a declared license: {bad}")
    bad = [c["purl"] for c in py if not c["externalReferences"]]
    if bad:
        die(f"Python components without release files on PyPI: {bad}")

    this = f"pkg:pypi/{DIST}@{a.version}"
    npm_comps = [c for c, _ in npm]
    bundles: dict[str, list[str]] = {}
    for c in npm_comps:
        for p in c["properties"]:
            if p["value"].startswith("bundled in "):
                n, _, v = p["value"][len("bundled in "):].rpartition("@")
                bundles.setdefault(sonatype.npm_purl(n, v), []).append(c["bom-ref"])
    bom = {
        "bomFormat": "CycloneDX", "specVersion": "1.5", "version": 1,
        "serialNumber": f"urn:uuid:{uuid.uuid4()}",
        "metadata": {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "tools": {"components": [{"type": "application", "name": f"{DIST} overlay/sbom.py",
                                      "externalReferences": [{"type": "vcs", "url": REPO}]}]},
            "component": {
                "type": "application", "bom-ref": this, "name": DIST, "version": a.version,
                "purl": this, "description": "Unofficial PyPI distribution of Hermes WebUI",
                "licenses": [{"expression": "MIT"}],
                "externalReferences": [{"type": "vcs", "url": REPO},
                                       {"type": "distribution", "url": f"https://pypi.org/project/{DIST}/{a.version}/"},
                                       {"type": "website", "url": UPSTREAM}],
                # Repackaging, stated in the vocabulary scanners understand:
                # the ancestor is upstream's tagged release; the change is packaging.
                "pedigree": {
                    "ancestors": [{"type": "application", "bom-ref": f"upstream-{a.upstream_tag}",
                                   "name": "hermes-webui", "version": a.upstream_tag,
                                   "purl": f"pkg:github/nesquena/hermes-webui@{a.upstream_tag}",
                                   "licenses": [{"expression": "MIT"}],
                                   "externalReferences": [{"type": "vcs",
                                                           "url": f"{UPSTREAM}/tree/{a.upstream_tag}"}]}],
                    "notes": "Built from the upstream tag with packaging-only changes: distribution name "
                             "and metadata, launcher that never installs, front-end libraries self-hosted "
                             "and raised, Mermaid rebuilt from a lockfile, dependency floors raised. See "
                             f"{REPO}/tree/horo/overlay",
                },
                "properties": [{"name": "horo:upstream-tag", "value": a.upstream_tag}],
            },
        },
        "components": py + npm_comps,
        "dependencies": [{"ref": this, "dependsOn": sorted(c["bom-ref"] for c in py + npm_comps
                                                           if c["bom-ref"] not in {r for v in bundles.values() for r in v}
                                                           or any(p["value"] == "static/vendor" for p in c.get("properties", [])))}]
                        + [{"ref": b, "dependsOn": sorted(refs)} for b, refs in sorted(bundles.items())],
    }
    a.out.write_text(json.dumps(bom, indent=1) + "\n", encoding="utf-8")

    buf = [f"Third-party software in {DIST} {a.version}", "",
           "The front end (static/vendor) contains the npm packages below, some of them",
           "compiled into a single minified bundle. Their license texts follow, as each",
           "package ships them. Python dependencies are installed as separate packages",
           "and carry their own license files.", ""]
    missing = 0
    for c, texts in sorted(npm, key=lambda x: x[0]["purl"]):
        ships = ", ".join(p["value"] for p in c["properties"])
        buf += ["=" * 78, f"{c['name']}@{c['version']}  —  {c['licenses'][0]['expression']}  —  {ships}", "=" * 78]
        missing += not texts
        for fname, text in texts or [("", "(The package declares this license and ships no license file.)")]:
            buf += [f"--- {fname}" if fname else "", text.strip(), ""]
    (root / NOTICES).write_text("\n".join(buf) + "\n", encoding="utf-8")
    pp = root / "pyproject.toml"
    text = pp.read_text(encoding="utf-8")
    anchor = 'license-files = ["LICENSE"]'
    if text.count(anchor) != 1:
        die(f"pyproject.toml: anchor {anchor!r} not found — cannot ship the notices")
    pp.write_text(text.replace(anchor, f'license-files = ["LICENSE", "{NOTICES}"]'), encoding="utf-8")

    print(f"[sbom] {len(py)} Python components, {len(npm_comps)} npm components "
          f"({sum(len(v) for v in bundles.values())} bundled)")
    print(f"[sbom] {NOTICES}: {len(npm)} entries, {missing} without a license file in the package")
    print(f"[sbom] wrote {a.out}")
    return 0


def embed(a) -> int:
    wheel, sbom = a.wheel.resolve(), a.sbom.resolve()
    with zipfile.ZipFile(wheel) as z:
        names = z.namelist()
        distinfo = next(n.split("/")[0] for n in names if n.endswith(".dist-info/RECORD"))
        record_name = f"{distinfo}/RECORD"
        entries = {n: z.read(n) for n in names}
        infos = {n: z.getinfo(n) for n in names}
    target = f"{distinfo}/sboms/{sbom.name}"
    data = sbom.read_bytes()
    entries[target] = data
    digest = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()
    rows = [r for r in csv.reader(io.StringIO(entries[record_name].decode())) if r and r[0] != target]
    rows.insert(len(rows) - 1, [target, f"sha256={digest}", str(len(data))])  # RECORD stays last
    out = io.StringIO()
    csv.writer(out, lineterminator="\n").writerows(rows)
    entries[record_name] = out.getvalue().encode()
    tmp = wheel.with_suffix(".tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for n in [n for n in names if n != record_name] + [target, record_name]:
            if n == target or n == record_name:
                z.writestr(zipfile.ZipInfo(n, date_time=infos[record_name].date_time), entries[n],
                           compress_type=zipfile.ZIP_DEFLATED)
            else:
                z.writestr(infos[n], entries[n])
    shutil.move(tmp, wheel)

    # The release copy also records the finished wheel, which the embedded
    # copy cannot (it is inside it).
    bom = json.loads(data)
    bom["metadata"]["component"]["hashes"] = [
        {"alg": "SHA-256", "content": hashlib.sha256(wheel.read_bytes()).hexdigest()}]
    a.release_copy.write_text(json.dumps(bom, indent=1) + "\n", encoding="utf-8")
    print(f"[sbom] embedded {target}; release copy {a.release_copy}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("checkout", type=Path)
    g.add_argument("version")
    g.add_argument("upstream_tag")
    g.add_argument("--requirements", type=Path, required=True)
    g.add_argument("--out", type=Path, required=True)
    e = sub.add_parser("embed")
    e.add_argument("wheel", type=Path)
    e.add_argument("sbom", type=Path)
    e.add_argument("--release-copy", type=Path, required=True)
    a = ap.parse_args()
    return generate(a) if a.cmd == "generate" else embed(a)


if __name__ == "__main__":
    sys.exit(main())
