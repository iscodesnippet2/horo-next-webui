"""Corrupt real release files one at a time; verify_release.py must catch each.

    python overlay/tests/mutate_release.py <wheel> <release.cdx.json> <notices> \\
        <requirements.txt> <requirements-hashes.txt> <audit.json> <version>

A check that passes on good input proves little: this proves each one can fail.
Each mutation names the check that must catch it, and a corrupted SBOM or wheel
is resealed (embedded copy and wheel hash brought back in line) so that check,
not an earlier one, is what fires. Needs what verify_release.py needs.
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

VERIFY = Path(__file__).resolve().parent.parent / "verify_release.py"
FILES = [Path(p).resolve() for p in sys.argv[1:7]]
VERSION = sys.argv[7]
WHEEL, BOM, NOTICES, REQS, HASHES, AUDIT = range(6)


def bom_edit(fn):
    def apply(files):
        b = json.loads(files[BOM].read_text())
        fn(b)
        files[BOM].write_text(json.dumps(b, indent=1))
    return apply


def audit_edit(fn):
    def apply(files):
        a = json.loads(files[AUDIT].read_text())
        fn(a["audits"])
        files[AUDIT].write_text(json.dumps(a))
    return apply


def text_edit(which, fn):
    def apply(files):
        files[which].write_bytes(fn(files[which].read_bytes().decode()).encode())
    return apply


def wheel_edit(fn):
    """fn(name, data) → data, or None to drop the file."""
    def apply(files):
        src, out = files[WHEEL], files[WHEEL].with_suffix(".new")
        with zipfile.ZipFile(src) as z, zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as w:
            for i in z.infolist():
                data = fn(i.filename, z.read(i.filename))
                if data is not None:
                    w.writestr(i, data)
        shutil.move(out, src)
    return apply


def both_notices(fn):
    """The release copy and the wheel's, so the content check is what must fire."""
    def apply(files):
        text_edit(NOTICES, fn)(files)
        wheel_edit(lambda n, d: fn(d.decode()).encode() if n.endswith("licenses/THIRD_PARTY_LICENSES") else d)(files)
    return apply


def drop_npm(name):
    def fn(b):
        b["components"] = [c for c in b["components"] if c["name"] != name]
    return fn


def field(name, key):
    def fn(b):
        next(c for c in b["components"] if c["name"] == name).pop(key)
    return fn


def set_meta(fn):
    def edit(name, data):
        return fn(data.decode()).encode() if name.endswith(".dist-info/METADATA") else data
    return edit


def reseal(files) -> None:
    """Make the wheel carry the release SBOM, and the SBOM the wheel's new hash."""
    bom = json.loads(files[BOM].read_text())
    wheel_edit(lambda n, d: json.dumps(bom).encode() if n.endswith("/sboms/horo-next-webui.cdx.json") else d)(files)
    for h in bom.get("metadata", {}).get("component", {}).get("hashes", []):
        h["content"] = hashlib.sha256(files[WHEEL].read_bytes()).hexdigest()
    files[BOM].write_text(json.dumps(bom, indent=1))


def no_reseal(fn):
    fn.keep = True
    return fn


# label → (mutation, the check that must report it)
MUTATIONS = {
    "SBOM spec version wrong": (bom_edit(lambda b: b.update(specVersion="9.9")), "valid CycloneDX"),
    "SBOM breaks the schema": (bom_edit(lambda b: b["components"][0].update(type="nonsense")), "valid CycloneDX"),
    "SBOM describes another version": (bom_edit(lambda b: b["metadata"]["component"].update(purl="pkg:pypi/horo-next-webui@0.0.0")), "describes this release"),
    "wheel hash altered": (no_reseal(bom_edit(lambda b: b["metadata"]["component"]["hashes"][0].update(content="0" * 64))), "wheel's SHA-256"),
    "embedded SBOM differs": (no_reseal(bom_edit(drop_npm("xterm"))), "embedded SBOM lists the same"),
    "pedigree removed": (bom_edit(lambda b: b["metadata"]["component"].pop("pedigree")), "pedigree"),
    "vendored library dropped from SBOM": (bom_edit(drop_npm("katex")), "every library in static/vendor"),
    "streaming-markdown dropped from SBOM": (bom_edit(drop_npm("streaming-markdown")), "streaming-markdown"),
    "bundled DOMPurify dropped from SBOM": (bom_edit(drop_npm("dompurify")), "DOMPurify in the bundle"),
    "bundle membership dropped from SBOM": (bom_edit(lambda b: [c.pop("properties", None) for c in b["components"]]), "packages inside the bundle"),
    "Python package dropped from SBOM": (bom_edit(drop_npm("cryptography")), "exactly the resolved Python set"),
    "license removed": (bom_edit(field("dompurify", "licenses")), "license and a scope"),
    "npm hash removed": (bom_edit(field("prismjs", "hashes")), "tarball hash"),
    "Python file hashes removed": (bom_edit(field("pyyaml", "externalReferences")), "hashed files"),
    "scope removed": (bom_edit(field("prismjs", "scope")), "license and a scope"),
    "duplicate component": (bom_edit(lambda b: b["components"].append(b["components"][0])), "appears once"),
    "KaTeX fonts declared MIT only": (bom_edit(lambda b: next(c for c in b["components"] if c["name"] == "katex")
                                               .update(licenses=[{"expression": "MIT"}])), "OFL-1.1"),
    "notices lose a package": (both_notices(lambda t: t.replace("prismjs@", "prism@")), "cover every npm"),
    "notices lose the OFL text": (both_notices(lambda t: re.sub(r"(?i)sil open font license", "x", t)), "OFL text"),
    "release notices differ from the wheel's": (text_edit(NOTICES, lambda t: t + "x"), "ones in the wheel"),
    "a pin loses its hashes": (text_edit(HASHES, lambda t: re.sub(r"\s+\\\n\s+--hash=\S+|\n\s+--hash=\S+", "", t)), "pinned with hashes"),
    "a dependency leaves the hashed list": (text_edit(HASHES, lambda t: re.sub(r"(?ms)^pyyaml==.*?(?=^[a-z]|\Z)", "", t)), "pinned with hashes"),
    "audit skipped Sonatype": (audit_edit(lambda a: a.pop(1)), "both databases"),
    "audit has a finding": (audit_edit(lambda a: a[0]["advisories"].append({"purl": "x", "id": "GHSA"})), "OSV: no finding"),
    "audit recorded an error": (audit_edit(lambda a: a[1].update(error="token rejected")), "Sonatype: no finding"),
    "audit marked failed": (audit_edit(lambda a: a[1].update(result="fail")), "Sonatype: no finding"),
    "audit missed a component": (audit_edit(lambda a: a[0]["packages"].pop()), "OSV: covered"),
    "wheel lost the notices": (wheel_edit(lambda n, d: None if n.endswith("licenses/THIRD_PARTY_LICENSES") else d), "ones in the wheel"),
    "metadata lost the Source URL": (wheel_edit(set_meta(lambda t: re.sub(r"Project-URL: Source.*\n", "", t))), "Source is this repository"),
    "metadata lost the Upstream URL": (wheel_edit(set_meta(lambda t: re.sub(r"Project-URL: Upstream.*\n", "", t))), "Upstream is named"),
    "metadata lost the maintainer": (wheel_edit(set_meta(lambda t: re.sub(r"Maintainer.*\n", "", t))), "maintainer"),
    "metadata lost the SPDX expression": (wheel_edit(set_meta(lambda t: re.sub(r"License-Expression.*\n", "", t))), "SPDX license expression"),
    "metadata gained a License classifier": (wheel_edit(set_meta(lambda t: t.replace("Classifier:", "Classifier: License :: OSI Approved :: MIT License\nClassifier:", 1))), "no License classifier"),
    "metadata no longer says unofficial": (wheel_edit(set_meta(lambda t: t.replace("Unofficial", "Official"))), "says it is unofficial"),
}


def run(files) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(VERIFY), *map(str, files), VERSION], capture_output=True, text=True)


with tempfile.TemporaryDirectory() as tmp:
    base = run(FILES)
    if base.returncode:
        sys.exit(f"[mutate] verify_release.py fails on the unmodified release:\n{base.stdout}{base.stderr}")
    print(f"[mutate] unmodified release passes; {len(MUTATIONS)} mutations")
    missed = []
    for label, (mutate, expected) in MUTATIONS.items():
        d = Path(tempfile.mkdtemp(dir=tmp))
        files = [Path(shutil.copy(f, d)) for f in FILES]
        mutate(files)
        if not getattr(mutate, "keep", False):
            reseal(files)
        r = run(files)
        reported = re.findall(r"^  FAIL (.+?)(?: — .*)?$", r.stdout, re.M)
        hit = any(expected in x for x in reported)
        print(f"  {'caught' if hit else 'MISSED'} {label}" + ("" if hit else f" — reported {reported} {r.stderr[-200:]}"))
        if not hit:
            missed.append(label)
if missed:
    sys.exit(f"[mutate] {len(missed)} mutation(s) not caught by their check: {missed}")
print("[mutate] every mutation caught by its check")
