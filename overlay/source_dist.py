"""The source distribution: the overlaid tree, buildable offline, for development.

    python overlay/source_dist.py prepare <upstream-checkout> <version> <upstream-tag>
    python overlay/source_dist.py verify <sdist> <wheel> <version> [--no-rebuild]

`prepare` runs on the finished tree, just before the build. It adds `horo/`
(where the tree came from, the patch against upstream, the overlay that made
it, and how to develop on it offline) and a MANIFEST.in that puts upstream's
tests in the sdist and leaves out what the wheel deliberately does not ship.
`uv build` then builds the sdist and the wheel from it.

`verify` holds a release whose sdist is not the wheel's source:

* every file the wheel installs is in the sdist, byte for byte;
* what only the sdist has is tests, `horo/`, upstream's scripts and docs text
  (its tests read them) and packaging metadata, and none of it is third-party code the wheel's SBOM and audit did not cover: no script,
  stylesheet, WebAssembly or source map unless the wheel ships the same bytes,
  no package manifest or lockfile, no page that loads anything remote;
* `horo/SOURCE.json` names this version, the upstream tag and commit, and the
  overlay commit;
* the sdist, unpacked and built without network or build isolation by the
  setuptools release that built the wheel, gives the same files and metadata.
"""
from __future__ import annotations

import email
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DIST = "horo-next-webui"
PROJECT = "horo_next_webui"
HEX40 = re.compile(r"[0-9a-f]{40}")

# Upstream's tests read some of the repository's own files (the changelog, the
# docs, the launch and container scripts), so those come too, as text. Left out:
# the docs' screenshots and demo pages (one loads KaTeX 0.16.9 from a CDN), and
# the npm/uv/Nix lockfiles, which pin versions the wheel does not ship (an
# enterprise mirror's scanner reads those as components).
MANIFEST = """\
# horo-next-webui: written by overlay/source_dist.py
graft tests
graft horo
graft scripts
graft nix
recursive-include docs *.md
include *.md *.sh *.ps1 *.bash Dockerfile docker-compose*.yml flake.nix
include .env.example .env.docker.example .dockerignore .github/workflows/tests.yml
include pytest.ini requirements.txt requirements-dev.txt
include horo-build.json horo-vendor.json THIRD_PARTY_LICENSES
global-exclude __pycache__ *.py[cod] .DS_Store
"""

# Files only the sdist has: those MANIFEST names, and the sdist's own metadata.
SDIST_ONLY_FILES = re.compile(r"[^/]+\.(md|sh|ps1|bash|yml|txt|ini|toml|cfg|in|nix)|Dockerfile|PKG-INFO|LICENSE|"
                              r"THIRD_PARTY_LICENSES|horo-(build|vendor)\.json|docs/.+\.md|"
                              r"\.env(\.docker)?\.example|\.dockerignore|\.github/workflows/tests\.yml")
SDIST_ONLY_DIRS = ("tests/", "horo/", "scripts/", "nix/", f"{PROJECT}.egg-info/")
CODE = re.compile(r"\.(js|mjs|cjs|css|wasm|map)$")
MANIFESTS = re.compile(r"(^|/)(package(-lock)?\.json|npm-shrinkwrap\.json|yarn\.lock|pnpm-lock\.yaml|uv\.lock|"
                       r"poetry\.lock|Pipfile(\.lock)?|[^/]+\.(whl|tar\.gz|zip|jar|so|dylib|dll|exe))$")
REMOTE = re.compile(rb"""(src|href)\s*=\s*["']?(https?:)?//|@import\s+url\(\s*["']?(https?:)?//""", re.I)


def run(*cmd: str, cwd: Path | None = None, env: dict | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, env=env, check=True, capture_output=True, text=True).stdout


# ── prepare ──────────────────────────────────────────────────────────────

def overlay_commit() -> str:
    """The overlay's commit: CI's checkout, or HORO_OVERLAY_COMMIT for a copy of it."""
    sha = os.environ.get("HORO_OVERLAY_COMMIT") or os.environ.get("GITHUB_SHA")
    if not sha:
        try:
            sha = run("git", "rev-parse", "HEAD", cwd=HERE).strip()
        except (OSError, subprocess.CalledProcessError):
            sha = ""
    if not HEX40.fullmatch(sha):
        sys.exit("[sdist] cannot tell which overlay commit this is: set HORO_OVERLAY_COMMIT")
    return sha


def patch(root: Path) -> str:
    """Every text change against upstream's commit, new files included.

    A scratch index, so the checkout's own is left alone. The libraries under
    static/vendor are not in it: horo-vendor.json names each one.
    """
    with tempfile.TemporaryDirectory() as tmp:
        env = {**os.environ, "GIT_INDEX_FILE": str(Path(tmp) / "index")}
        run("git", "read-tree", "HEAD", cwd=root, env=env)
        run("git", "add", "-A", "--", ".", ":!static/vendor", ":!build", ":!dist", ":!*.egg-info", ":!horo",
            ":!MANIFEST.in", cwd=root, env=env)
        return run("git", "diff", "--cached", "--no-color", "HEAD", "--", ".", ":!static/vendor", cwd=root, env=env)


def prepare(root: Path, version: str, tag: str) -> None:
    built = json.loads((root / "horo-build.json").read_text(encoding="utf-8"))
    if built.get("version") != version:
        sys.exit(f"[sdist] the tree was overlaid as {built.get('version')}, not {version}")
    upstream = run("git", "rev-parse", "HEAD", cwd=root).strip()
    if not HEX40.fullmatch(upstream):
        sys.exit(f"[sdist] the upstream checkout has no commit: {upstream!r}")
    horo = root / "horo"
    if horo.exists():
        sys.exit("[sdist] upstream already has a horo/ directory")
    horo.mkdir()
    (horo / "overlay.patch").write_text(patch(root), encoding="utf-8")
    shutil.copytree(HERE, horo / "overlay",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".DS_Store", "mermaid"))
    shutil.copy(HERE / "DEVELOPING.md", horo / "DEVELOPING.md")
    (horo / "SOURCE.json").write_text(json.dumps({
        "version": version,
        "upstream": {"repository": "https://github.com/nesquena/hermes-webui", "tag": tag, "commit": upstream},
        "overlay": {"repository": "https://github.com/iscodesnippet2/horo-next-webui", "commit": overlay_commit()},
    }, indent=1) + "\n", encoding="utf-8")
    if (root / "MANIFEST.in").exists():
        sys.exit("[sdist] upstream already has a MANIFEST.in")
    (root / "MANIFEST.in").write_text(MANIFEST, encoding="utf-8")
    print(f"[sdist] prepared horo/ for {version} (upstream {tag} @ {upstream[:12]})")


# ── verify ───────────────────────────────────────────────────────────────

def read_sdist(path: Path, version: str) -> tuple[dict[str, bytes], list[str]]:
    top = f"{PROJECT}-{version}/"
    files, problems = {}, []
    with tarfile.open(path) as t:
        for m in t.getmembers():
            if m.name == top.rstrip("/") and m.isdir():
                continue
            if not m.name.startswith(top):
                problems.append(f"outside {top}: {m.name}")
            elif m.isfile():
                files[m.name[len(top):]] = t.extractfile(m).read()
            elif not m.isdir():
                problems.append(f"not a file or directory: {m.name}")
    return files, problems


def read_wheel(path: Path) -> tuple[dict[str, bytes], dict[str, bytes]]:
    """(installed files, dist-info files by base name)."""
    payload, info = {}, {}
    with zipfile.ZipFile(path) as z:
        for n in z.namelist():
            if n.endswith("/"):
                continue
            head, _, rest = n.partition("/")
            if head.endswith(".dist-info"):
                info[rest] = z.read(n)
            else:
                payload[n] = z.read(n)
    return payload, info


def check_contents(sdist: Path, wheel: Path, version: str) -> list[str]:
    """Everything verify checks except the rebuild; a list of failures."""
    failures: list[str] = []

    def check(cond: object, label: str, detail: object = "") -> None:
        print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' — ' + str(detail)) if detail and not cond else ''}")
        if not cond:
            failures.append(label)

    check(sdist.name == f"{PROJECT}-{version}.tar.gz", "sdist is named for this version", sdist.name)
    src, odd = read_sdist(sdist, version)
    check(not odd, "sdist holds only files under one top directory", odd[:5])
    payload, info = read_wheel(wheel)

    missing = sorted(n for n in payload if n not in src)
    check(payload and not missing, "every file the wheel installs is in the sdist", missing[:5])
    changed = sorted(n for n in payload if n in src and src[n] != payload[n])
    check(not changed, "and is the same bytes", changed[:5])

    extra = sorted(n for n in src if n not in payload)
    stray = [n for n in extra if not SDIST_ONLY_FILES.fullmatch(n) and not n.startswith(SDIST_ONLY_DIRS)]
    check(not stray, "what only the sdist has is tests, horo/, scripts, docs text and metadata", stray[:5])
    shipped = set(payload.values())
    code = [n for n in extra if CODE.search(n) and src[n] not in shipped]
    check(not code, "no script, stylesheet or WebAssembly the wheel does not ship", code[:5])
    manifests = [n for n in extra if MANIFESTS.search(n)]
    check(not manifests, "no package manifest, lockfile or archive", manifests[:5])
    remote = [n for n in extra if n.endswith((".html", ".htm", ".svg")) and REMOTE.search(src[n])]
    check(not remote, "no page that loads anything remote", remote[:5])
    check(not any(n.startswith("static/") for n in extra), "static/ is exactly the wheel's",
          [n for n in extra if n.startswith("static/")][:5])
    check(any(n.startswith("tests/test_") for n in extra), "upstream's tests are in the sdist")

    for name in ("LICENSE", "THIRD_PARTY_LICENSES"):
        check(src.get(name) and src[name] == info.get(f"licenses/{name}"), f"{name} is the wheel's")

    pkg = email.message_from_bytes(src.get("PKG-INFO", b""))
    meta = email.message_from_bytes(info.get("METADATA", b""))
    check(pkg["Name"] == DIST and pkg["Version"] == version == meta["Version"],
          "sdist and wheel metadata name this version", (pkg["Name"], pkg["Version"], meta["Version"]))

    try:
        source = json.loads(src["horo/SOURCE.json"])
    except (KeyError, ValueError) as e:
        source = {}
        check(False, "horo/SOURCE.json is readable", e)
    up, ov = source.get("upstream", {}), source.get("overlay", {})
    check(source.get("version") == version, "SOURCE.json names this version", source.get("version"))
    check(HEX40.fullmatch(up.get("commit", "")) and re.fullmatch(r"(exp-)?v\d+\.\d+\.\d+", up.get("tag", "")),
          "SOURCE.json names the upstream tag and commit", up)
    check(HEX40.fullmatch(ov.get("commit", "")), "SOURCE.json names the overlay commit", ov)
    check(src.get("horo/overlay.patch", b"").startswith(b"diff --git "), "horo/overlay.patch is a patch")
    check("horo/overlay/horo_overlay.py" in src and "horo/DEVELOPING.md" in src, "the overlay and its guide are in horo/")
    return failures


def rebuild(sdist: Path, wheel: Path, version: str) -> list[str]:
    """Build the sdist as an air-gapped machine would; compare with the wheel."""
    failures: list[str] = []
    _, info = read_wheel(wheel)
    gen = re.search(rb"^Generator: setuptools \(([\w.]+)\)", info.get("WHEEL", b""), re.M)
    if not gen:
        return ["the wheel names the setuptools that built it"]
    setuptools = gen[1].decode()
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        with tarfile.open(sdist) as t:
            t.extractall(tmp, filter="data")
        tree, venv, out = tmp / f"{PROJECT}-{version}", tmp / "venv", tmp / "out"
        out.mkdir()
        run("uv", "venv", "-q", "--python", "3.12", str(venv))
        py = str(venv / "bin" / "python")
        run("uv", "pip", "install", "-q", "--python", py, f"setuptools=={setuptools}")
        # What `pip wheel --no-build-isolation --no-index .` does, without pip.
        env = {k: v for k, v in os.environ.items() if not k.endswith(("_PROXY", "_proxy"))}
        env.update(HTTPS_PROXY="http://127.0.0.1:9", HTTP_PROXY="http://127.0.0.1:9", PIP_NO_INDEX="1")
        try:
            run(py, "-c", "import sys; from setuptools import build_meta as b; print(b.build_wheel(sys.argv[1]))",
                str(out), cwd=tree, env=env)
        except subprocess.CalledProcessError as e:
            return [f"the sdist builds offline: {e.stderr.strip().splitlines()[-1:]}"]
        built = list(out.glob("*.whl"))
        if len(built) != 1:
            return [f"the offline build made one wheel: {built}"]
        payload, mine = read_wheel(built[0])
        theirs, info = read_wheel(wheel)
        diff = sorted(set(payload) ^ set(theirs)) + sorted(n for n in payload if n in theirs and payload[n] != theirs[n])
        print(f"  {'ok  ' if not diff else 'FAIL'} the offline build installs the same files (setuptools {setuptools})")
        if diff:
            print(f"       {diff[:5]}")
            failures.append("the offline build installs the same files")
        meta = [n for n in ("METADATA", "entry_points.txt", "top_level.txt", "licenses/LICENSE",
                            "licenses/THIRD_PARTY_LICENSES") if mine.get(n) != info.get(n)]
        print(f"  {'ok  ' if not meta else 'FAIL'} and the same metadata{(' — ' + str(meta)) if meta else ''}")
        if meta:
            failures.append("the offline build has the same metadata")
    return failures


def verify(sdist: Path, wheel: Path, version: str, rebuild_too: bool) -> None:
    failures = check_contents(sdist, wheel, version)
    if rebuild_too and not failures:
        failures += rebuild(sdist, wheel, version)
    if failures:
        sys.exit(f"[sdist] {len(failures)} check(s) failed: {failures}")
    print("[sdist] all checks passed")


if __name__ == "__main__":
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "prepare" and len(args) == 3:
        prepare(Path(args[0]).resolve(), args[1], args[2])
    elif cmd == "verify" and len(args) in (3, 4):
        verify(Path(args[0]), Path(args[1]), args[2], "--no-rebuild" not in args[3:])
    else:
        sys.exit(__doc__)
