"""Audit everything a release ships against OSV and Sonatype.

    python overlay/audit_shipped.py <requirements.txt> <horo-vendor.json> [--notes FILE] [--report FILE]

The enterprise mirror is guarded by Sonatype, whose data overlaps OSV but is
not the same: Sonatype publishes its own findings (sonatype-YYYY-NNNN) that no
CVE or OSV entry carries. A release is held unless BOTH report nothing for:

* every Python package the wheel resolves to on Linux / CPython 3.12
  (requirements-<version>.txt, pinned), and
* every npm component in the wheel's front end (horo-vendor.json, written by
  vendor_assets.py): each vendored library, and each package bundled into a
  library rebuilt from source.

Sonatype is queried through its public API (Sonatype Guide, formerly OSS
Index) with a personal access token in SONATYPE_GUIDE_TOKEN. Without a token the
audit fails: an unchecked release is not a clean one. The public API serves a
subset of what Sonatype's commercial products know, so a clean result narrows
the gap with the mirror; it cannot close it.

Run daily against the latest release by .github/workflows/rescan.yml.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from packaging.markers import Marker
from packaging.utils import canonicalize_name

TARGET = {"python_version": "3.12", "python_full_version": "3.12.0", "sys_platform": "linux",
          "platform_system": "Linux", "platform_machine": "x86_64", "os_name": "posix",
          "implementation_name": "cpython", "platform_python_implementation": "CPython", "extra": ""}
SONATYPE = "https://api.guide.sonatype.com/api/v3/component-report"
BATCH = 128

reqs, manifest = Path(sys.argv[1]), Path(sys.argv[2])
notes = Path(sys.argv[sys.argv.index("--notes") + 1]) if "--notes" in sys.argv else None
report = Path(sys.argv[sys.argv.index("--report") + 1]) if "--report" in sys.argv else None

components: list[tuple[str, str, str]] = []  # (ecosystem, name, version)
for raw in reqs.read_text(encoding="utf-8").splitlines():
    m = re.match(r"^\s*([A-Za-z0-9_.\-]+)(\[[^\]]*\])?\s*==\s*([^\s;]+)\s*(?:;\s*(.+))?$", raw)
    if m and not (m[4] and not Marker(m[4]).evaluate(TARGET)):
        components.append(("PyPI", canonicalize_name(m[1]), m[3]))
for c in json.loads(manifest.read_text(encoding="utf-8")):
    components.append(("npm", c["name"], c["version"]))
components = sorted(set(components))
if not components:
    sys.exit("[audit] nothing to audit — refusing to report a clean result")


def purl(eco: str, name: str, version: str) -> str:
    if eco == "PyPI":
        return f"pkg:pypi/{name}@{version}"
    return f"pkg:npm/{urllib.parse.quote(name, safe='/')}@{version}"


def post(url: str, payload: dict, headers: dict | None = None):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


findings: dict[str, list[str]] = {}  # purl → ["source id (cve, score) title"]

# ── OSV ──────────────────────────────────────────────────────────────────
for i in range(0, len(components), 500):
    chunk = components[i:i + 500]
    res = post("https://api.osv.dev/v1/querybatch", {"queries": [
        {"package": {"name": n, "ecosystem": e}, "version": v} for e, n, v in chunk]})
    for (e, n, v), r in zip(chunk, res["results"]):
        for x in r.get("vulns", []):
            if not x.get("withdrawn"):
                findings.setdefault(purl(e, n, v), []).append(f"OSV {x['id']}")

# ── Sonatype ─────────────────────────────────────────────────────────────
token = os.environ.get("SONATYPE_GUIDE_TOKEN", "").strip()
sonatype_ok = bool(token)
if not token:
    print("[audit] FAIL SONATYPE_GUIDE_TOKEN is not set — Sonatype not checked")
else:
    # Basic auth with the token as password; the user name is ignored for a PAT.
    auth = {"Authorization": "Basic " + base64.b64encode(f"horo:{token}".encode()).decode()}
    purls = [purl(*c) for c in components]
    seen = set()
    try:
        for i in range(0, len(purls), BATCH):
            for entry in post(SONATYPE, {"coordinates": purls[i:i + BATCH]}, auth):
                coord = entry.get("coordinates", "")
                seen.add(coord)
                for x in entry.get("vulnerabilities", []):
                    ident = x.get("cve") or x.get("displayName") or x.get("id")
                    score = x.get("cvssScore")
                    findings.setdefault(coord, []).append(
                        f"Sonatype {ident} (CVSS {score}) {x.get('title', '')}".rstrip())
    except urllib.error.HTTPError as exc:
        sonatype_ok = False
        body = exc.read()[:300] if exc.fp else b""
        print(f"[audit] FAIL Sonatype API: HTTP {exc.code} {body!r}")
    else:
        # Coordinates come back normalized; compare loosely, but every
        # component must have been answered for.
        def norm(p: str) -> str:
            return urllib.parse.unquote(p).lower()
        missing = {norm(p) for p in purls} - {norm(c) for c in seen}
        if missing:
            sonatype_ok = False
            print(f"[audit] FAIL Sonatype did not report on {len(missing)} components: {sorted(missing)[:5]}")

n_py = sum(1 for c in components if c[0] == "PyPI")
lines = [f"Shipped components: {n_py} Python (Linux / 3.12), {len(components) - n_py} npm — "
         f"OSV and {'Sonatype' if sonatype_ok else 'Sonatype NOT CHECKED'}: "
         f"{len(findings)} with findings"]
for coord, items in sorted(findings.items()):
    lines.append(f"  - {coord}: " + "; ".join(sorted(set(items))))
print("\n".join(lines))
for path in (notes, report):
    if path:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n" + "\n".join(lines) + "\n")

if findings or not sonatype_ok:
    sys.exit("[audit] release held: findings, or Sonatype not checked")
print("[audit] clean")
