"""Print Sonatype's findings for each version of some npm/PyPI packages.

    SONATYPE_GUIDE_TOKEN=... python overlay/sonatype_lookup.py <purl> [<purl> ...]

A purl without a version (pkg:npm/mermaid) expands to every final release on
the registry. For choosing a replacement version when Sonatype reports what
OSV does not; the token lives in CI, hence the sonatype-lookup workflow.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.parse
import urllib.request

from packaging.version import InvalidVersion, Version

URL = "https://api.guide.sonatype.com/api/v3/component-report"
token = os.environ["SONATYPE_GUIDE_TOKEN"].strip()
auth = "Basic " + base64.b64encode(f"horo:{token}".encode()).decode()


def versions(eco: str, name: str) -> list[str]:
    if eco == "npm":
        data = json.load(urllib.request.urlopen(f"https://registry.npmjs.org/{name}", timeout=60))["versions"]
    else:
        data = json.load(urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=60))["releases"]
    out = []
    for v in data:
        try:
            if not Version(v).is_prerelease:
                out.append(v)
        except InvalidVersion:
            pass
    return sorted(out, key=Version)


purls = []
for arg in sys.argv[1:]:
    if "@" in arg.split("/")[-1]:
        purls.append(arg)
        continue
    eco = "npm" if arg.startswith("pkg:npm/") else "pypi"
    name = urllib.parse.unquote(arg.split("/", 1)[1])
    purls += [f"{arg}@{v}" for v in versions("npm" if eco == "npm" else "pypi", name)]

for i in range(0, len(purls), 128):
    req = urllib.request.Request(URL, data=json.dumps({"coordinates": purls[i:i + 128]}).encode(),
                                 headers={"Content-Type": "application/json", "Authorization": auth})
    for entry in json.load(urllib.request.urlopen(req, timeout=120)):
        vulns = entry.get("vulnerabilities", [])
        ids = ", ".join(f"{v.get('cve') or v.get('id')}({v.get('cvssScore')})" for v in vulns)
        print(f"{entry['coordinates']}: {'clean' if not vulns else ids}")
