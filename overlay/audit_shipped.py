"""Audit everything a release ships against OSV and Sonatype.

    python overlay/audit_shipped.py <sbom.cdx.json> [--notes FILE] [--report FILE] [--json FILE]

The SBOM (sbom.py) is the list of what ships: every Python package the wheel
resolves to on Linux / CPython 3.12, every vendored front-end library, and every
package esbuild put in a rebuilt bundle.

The enterprise mirror is guarded by Sonatype, whose data overlaps OSV but is
not the same: Sonatype publishes its own findings (sonatype-YYYY-NNNN) that no
CVE or OSV entry carries. A release is held unless BOTH report nothing.

Sonatype is queried through its public API (Sonatype Guide, formerly OSS
Index) with a personal access token in SONATYPE_GUIDE_TOKEN. No token, a
rejected token, or a component left unanswered fails the audit: an unchecked
release is not a clean one. The public API serves a subset of what Sonatype's
commercial products know, so a clean result narrows the gap with the mirror; it
cannot close it.

Run daily against the latest release by .github/workflows/rescan.yml.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import sonatype  # noqa: E402

OSV = os.environ.get("HORO_OSV_URL", "https://api.osv.dev")
TARGET = {"os": "Linux", "python": "3.12", "implementation": "CPython"}


def osv_package(purl: str) -> dict:
    kind, _, rest = purl[len("pkg:"):].partition("/")
    name, _, version = rest.rpartition("@")
    return {"package": {"name": urllib.parse.unquote(name), "ecosystem": {"pypi": "PyPI", "npm": "npm"}[kind]},
            "version": version}


def post(url: str, payload: dict) -> dict:
    req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)


def check_osv(purls: list[str]) -> tuple[str, list[dict]]:
    """(error or "", findings). Anything but one answer per package is an error."""
    findings = []
    try:
        for i in range(0, len(purls), 500):
            chunk = purls[i:i + 500]
            res = post(f"{OSV}/v1/querybatch", {"queries": [osv_package(p) for p in chunk]})
            if len(res.get("results", [])) != len(chunk):
                return f"OSV answered for {len(res.get('results', []))} of {len(chunk)} packages", findings
            for purl, r in zip(chunk, res["results"]):
                findings += [{"purl": purl, "id": x["id"]} for x in r.get("vulns", []) if not x.get("withdrawn")]
    except OSError as exc:
        return f"OSV request failed: {exc}", findings
    return "", findings


def check_sonatype(purls: list[str]) -> tuple[str, list[dict]]:
    if not sonatype.available():
        return "SONATYPE_GUIDE_TOKEN is not set — Sonatype not checked", []
    try:
        results = sonatype.report(purls)
    except SystemExit as exc:  # a rejected token, an HTTP error
        return str(exc), []
    except OSError as exc:
        return f"Sonatype request failed: {exc}", []
    return "", [{"purl": p, "id": v.get("cve") or v.get("id"), "cvss": v.get("cvssScore"),
                 "title": v.get("title", ""), "reference": v.get("reference")}
                for p, vulns in results.items() for v in vulns]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("sbom", type=Path)
    ap.add_argument("--notes", type=Path)
    ap.add_argument("--report", type=Path)
    ap.add_argument("--json", type=Path)
    a = ap.parse_args()

    bom = json.loads(a.sbom.read_text(encoding="utf-8"))
    purls = sorted({c["purl"] for c in bom.get("components", [])})
    if not purls:
        sys.exit("[audit] nothing to audit — refusing to report a clean result")

    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
    audits, problems = [], []
    for label, check in (("OSV", check_osv), ("Sonatype (Sonatype Guide, OSS Index-compatible API)", check_sonatype)):
        error, findings = check(purls)
        audits.append({"database": label, "checked_at": now, "target": TARGET, "packages": purls,
                       "advisories": findings, "error": error,
                       "result": "pass" if not error and not findings else "fail"})
        if error:
            problems.append(error)

    flagged = {f["purl"] for x in audits for f in x["advisories"]}
    py = sum(p.startswith("pkg:pypi/") for p in purls)
    lines = [f"Shipped components: {py} Python (Linux / 3.12), {len(purls) - py} npm — "
             f"OSV and Sonatype: {len(flagged)} with findings"]
    for x in audits:
        for f in sorted(x["advisories"], key=lambda f: (f["purl"], f["id"])):
            lines.append(f"  - {f['purl']}: {x['database'].split()[0]} {f['id']}"
                         + (f" (CVSS {f['cvss']}) {f['title'][:90]}" if "cvss" in f else ""))
    lines += [f"  NOT CHECKED: {p}" for p in problems]
    print("\n".join(lines))
    for path in (a.notes, a.report):
        if path:
            with path.open("a", encoding="utf-8") as f:
                f.write("\n" + "\n".join(lines) + "\n")
    if a.json:
        a.json.write_text(json.dumps({"source": a.sbom.name, "audits": audits}, indent=1) + "\n", encoding="utf-8")

    if flagged or problems:
        sys.exit("[audit] release held: findings, or a database not checked")
    print("[audit] clean")
    return 0


if __name__ == "__main__":
    sys.exit(main())
