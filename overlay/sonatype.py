"""Sonatype's vulnerability data, through Sonatype Guide's OSS Index-compatible API.

Shared by audit_shipped.py (the release gate) and by security_bump.py and
vendor_assets.py, which use it to choose versions to raise to. The enterprise
mirror judges by Sonatype's data, which overlaps OSV but is not the same.

Needs SONATYPE_GUIDE_TOKEN (a Guide personal access token). Without one,
`available()` is False: the raising steps fall back to OSV and warn, and the
release gate refuses to pass.
"""
from __future__ import annotations

import base64
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

ENDPOINT = os.environ.get("SONATYPE_GUIDE_URL", "https://api.guide.sonatype.com/api/v3/component-report")
BATCH = 128  # OSS Index's per-request limit

# What a component Sonatype did not answer for carries: never clean.
NOT_RETURNED = {"id": "NOT-RETURNED", "cve": "NOT-RETURNED", "cvssScore": None,
                "title": "Sonatype returned no result for this component"}


def token() -> str:
    return os.environ.get("SONATYPE_GUIDE_TOKEN", "").strip()


def available() -> bool:
    return bool(token())


def _post(purls: list[str]) -> list[dict]:
    auth = base64.b64encode(f"horo-next-webui:{token()}".encode()).decode()  # user name is ignored for a PAT
    req = urllib.request.Request(ENDPOINT, data=json.dumps({"coordinates": purls}).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "Authorization": f"Basic {auth}"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit("[sonatype] 401 Unauthorized: SONATYPE_GUIDE_TOKEN was rejected")
            if e.code == 429 and attempt < 3:
                time.sleep(10 * (attempt + 1))
                continue
            sys.exit(f"[sonatype] HTTP {e.code} from {ENDPOINT}: {e.read()[:300]!r}")
    sys.exit("[sonatype] rate limited; gave up after retries")


def key(purl: str) -> str:
    """Compare purls as Sonatype may echo them: `%40` or `@`, any case, and
    PyPI names with `_`/`.` for `-`."""
    p = urllib.parse.unquote(purl).lower()
    if p.startswith("pkg:pypi/"):
        name, _, ver = p[len("pkg:pypi/"):].partition("@")
        p = "pkg:pypi/" + re.sub(r"[-_.]+", "-", name) + "@" + ver
    return p


def report(purls: list[str]) -> dict[str, list[dict]]:
    """{purl: [vulnerability, ...]} for every purl asked about; an empty list is
    clean. A purl Sonatype did not answer for, or answered without a
    vulnerabilities field, carries NOT_RETURNED, so callers fail closed."""
    purls = sorted(set(purls))
    asked = {key(p): p for p in purls}
    out: dict[str, list[dict]] = {}
    for i in range(0, len(purls), BATCH):
        for r in _post(purls[i:i + BATCH]):
            p = asked.get(key(r.get("coordinates", "")))
            if p and isinstance(r.get("vulnerabilities"), list):
                out[p] = r["vulnerabilities"]
    for p in purls:
        out.setdefault(p, [NOT_RETURNED])
    return out


def pypi_purl(name: str, version: str) -> str:
    return f"pkg:pypi/{name}@{version}"


def npm_purl(name: str, version: str) -> str:
    # purl spec: the scope's "@" is percent-encoded, its "/" is a separator.
    return f"pkg:npm/%40{name[1:]}@{version}" if name.startswith("@") else f"pkg:npm/{name}@{version}"
