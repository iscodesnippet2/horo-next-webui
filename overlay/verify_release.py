"""Check a release's files against each other before anything is published.

    python overlay/verify_release.py <wheel> <release.cdx.json> <notices> \\
        <requirements.txt> <requirements-hashes.txt> <audit.json> <version>

verify_wheel.py proves the wheel runs. This proves what the wheel says about
itself is true, by reading the build output: the SBOM names what is really in
static/vendor and in the Mermaid bundle, the notices cover it, the SBOM is valid
CycloneDX, it matches the wheel byte for byte, every dependency is hashed, and
the audit covered every component in both databases.

Needs cyclonedx-python-lib[json-validation]. tests/mutate_release.py corrupts
real release files one at a time and asserts this catches each.
"""
from __future__ import annotations

import email
import hashlib
import json
import re
import sys
import zipfile
from pathlib import Path

from cyclonedx.schema import SchemaVersion
from cyclonedx.validation.json import JsonStrictValidator

WHEEL, BOM, NOTICES, REQS, HASHES, AUDIT = map(Path, sys.argv[1:7])
VERSION = sys.argv[7]
DIST = "horo-next-webui"
REPO = "https://github.com/iscodesnippet2/horo-next-webui"

failures: list[str] = []


def check(cond: object, label: str, detail: object = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' — ' + str(detail)) if detail and not cond else ''}")
    if not cond:
        failures.append(label)


raw = BOM.read_text(encoding="utf-8")
bom = json.loads(raw)
err = JsonStrictValidator(SchemaVersion.V1_5).validate_str(raw)
check(err is None and bom.get("specVersion") == "1.5", "SBOM is valid CycloneDX 1.5", err)

with zipfile.ZipFile(WHEEL) as z:
    names = z.namelist()
    di = next(n.split("/")[0] for n in names if n.endswith(".dist-info/RECORD"))
    meta = email.message_from_bytes(z.read(f"{di}/METADATA"))
    embedded = f"{di}/sboms/horo-next-webui.cdx.json"
    inner = json.loads(z.read(embedded)) if embedded in names else {}
    inner_notices = f"{di}/licenses/THIRD_PARTY_LICENSES"
    wheel_notices = z.read(inner_notices) if inner_notices in names else b""
    bundles = {n: z.read(n) for n in names if re.fullmatch(r"static/vendor/mermaid/[^/]+/dist/mermaid\.min\.js", n)}
    record = z.read(f"{di}/RECORD").decode()

# ── Metadata ─────────────────────────────────────────────────────────────
urls = dict(u.split(", ", 1) for u in meta.get_all("Project-URL", []))
check(urls.get("Source") == REPO, "metadata: Source is this repository", urls)
check("nesquena/hermes-webui" in urls.get("Upstream", ""), "metadata: Upstream is named", urls)
check("Unofficial" in (meta["Summary"] or ""), "metadata: says it is unofficial", meta["Summary"])
check(meta["License-Expression"] == "MIT", "metadata: SPDX license expression", meta["License-Expression"])
check(not any(c.startswith("License ::") for c in meta.get_all("Classifier", [])), "metadata: no License classifier")
check(meta["Maintainer"] or meta["Maintainer-email"], "metadata: maintainer")
check(any("THIRD_PARTY" in f for f in meta.get_all("License-File", [])), "metadata: notices are a license file")

# ── The SBOM against the wheel ───────────────────────────────────────────
comp = bom.get("metadata", {}).get("component", {})
this = f"pkg:pypi/{DIST}@{VERSION}"
check(comp.get("purl") == this, "SBOM describes this release", comp.get("purl"))
digest = hashlib.sha256(WHEEL.read_bytes()).hexdigest()
check([h.get("content") for h in comp.get("hashes", [])] == [digest], "SBOM carries the wheel's SHA-256")
check(comp.get("pedigree", {}).get("ancestors", [{}])[0].get("purl", "").startswith("pkg:github/nesquena/hermes-webui@"),
      "SBOM pedigree names the upstream tag")
check(inner and inner["components"] == bom["components"], "embedded SBOM lists the same components (PEP 770)")
check(embedded in record, "embedded SBOM is in RECORD")

comps = bom.get("components", [])
purls = [c.get("purl", "") for c in comps]
check(len(purls) == len(set(purls)), "each component appears once")
npm = {c["purl"]: c for c in comps if c.get("purl", "").startswith("pkg:npm/")}
py = {c["purl"]: c for c in comps if c.get("purl", "").startswith("pkg:pypi/")}
check(all(c.get("licenses") and c.get("scope") for c in comps), "every component has a license and a scope",
      [c.get("purl") for c in comps if not c.get("licenses") or not c.get("scope")])
check(all(any(h.get("alg") == "SHA-512" for h in c.get("hashes", [])) for c in npm.values()),
      "every npm component has its tarball hash")
check(all(r.get("hashes") for c in py.values() for r in c.get("externalReferences", [])) and
      all(c.get("externalReferences") for c in py.values()), "every Python component lists hashed files")

# Every library directory in the wheel, read from the wheel.
vendored = {(m[1], m[2]) for n in names
            if (m := re.match(r"static/vendor/((?:@[^/]+/)?[^/]+)/(\d+\.\d+\.\d+)/", n))}
listed = {(c["name"], c["version"]) for c in npm.values()}
check(vendored and vendored <= listed, "every library in static/vendor is in the SBOM", sorted(vendored - listed))
check(any(n == "static/vendor/smd.min.js" for n in names) and any(c["name"] == "streaming-markdown" for c in npm.values()),
      "the unversioned streaming-markdown file is in the SBOM")
# Versions compiled into the bundle, read from its own license comment.
check(len(bundles) == 1, "one Mermaid bundle in the wheel", sorted(bundles))
for body in bundles.values():
    dp = re.search(rb"@license DOMPurify (\d+\.\d+\.\d+)", body)
    check(dp and ("dompurify", dp[1].decode()) in listed, "the DOMPurify in the bundle is the one in the SBOM",
          dp and dp[1])
    check(any(p["value"].startswith("bundled in mermaid@") for c in npm.values() for p in c.get("properties", [])),
          "the SBOM lists packages inside the bundle")
katex = next((c for c in npm.values() if c["name"] == "katex"), {})
if any("/fonts/KaTeX_" in n for n in names):
    check("OFL-1.1" in json.dumps(katex.get("licenses")), "KaTeX's fonts are declared OFL-1.1")

# ── Notices ──────────────────────────────────────────────────────────────
# Bytes: license texts carry CRLF, which read_text() would normalize.
notice_bytes = NOTICES.read_bytes()
check(notice_bytes == wheel_notices, "release notices are the ones in the wheel")
notices = notice_bytes.decode("utf-8")
absent = [c["name"] for c in npm.values() if f"{c['name']}@{c['version']}  —" not in notices]
check(not absent, "notices cover every npm component", absent)
if any("/fonts/KaTeX_" in n for n in names):
    check("SIL OPEN FONT LICENSE" in notices.upper(), "notices carry the OFL text for the fonts")

# ── Dependencies ─────────────────────────────────────────────────────────
pins = {re.sub(r"[-_.]+", "-", m[1]).lower(): m[2]
        for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==(\S+)", REQS.read_text(encoding="utf-8"), re.M)}
check(pins, "requirements list is not empty")
check({f"pkg:pypi/{n}@{v}" for n, v in pins.items()} == set(py), "SBOM lists exactly the resolved Python set",
      sorted({f"pkg:pypi/{n}@{v}" for n, v in pins.items()} ^ set(py)))
hashed = re.split(r"(?m)^(?=[A-Za-z0-9])", HASHES.read_text(encoding="utf-8"))
hashed = {re.sub(r"[-_.]+", "-", b.split("==")[0]).lower(): b for b in hashed if "==" in b.split("\n")[0]}
check(set(hashed) == set(pins) and all("--hash=sha256:" in b and f"=={pins[n]}" in b for n, b in hashed.items()),
      "every dependency is pinned with hashes", sorted(set(hashed) ^ set(pins)))

# ── Audit ────────────────────────────────────────────────────────────────
audit = json.loads(AUDIT.read_text(encoding="utf-8")).get("audits", [])
dbs = {a.get("database", "").split()[0]: a for a in audit}
check(set(dbs) == {"OSV", "Sonatype"}, "audit ran both databases", sorted(dbs))
for name, a in dbs.items():
    check(a.get("result") == "pass" and not a.get("advisories") and not a.get("error"), f"{name}: no finding", a.get("error"))
    check(set(a.get("packages", [])) >= set(purls), f"{name}: covered every component",
          sorted(set(purls) - set(a.get("packages", [])))[:5])

if failures:
    sys.exit(f"[release] {len(failures)} check(s) failed: {failures}")
print("[release] all checks passed")
