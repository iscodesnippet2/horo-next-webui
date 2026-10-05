"""Raise Python dependency floors that admit a version with a known advisory.

    python overlay/security_bump.py <upstream-checkout> [--notes FILE]

Upstream declares its dependencies in requirements.txt as floors (`>=X`), read
into the wheel's metadata. A floor that admits a vulnerable version lets a
mirror lagging behind PyPI — or an offline wheelhouse built from an old list —
install it. Each floor is raised to the lowest release that has no advisory,
so the guarantee is explicit and the change as small as it can be.

Every PyPI release between the old floor and the new one is not checked
individually: pip, given `>=new`, never picks them.

Transitive dependencies are not given floors here (that would break installs
behind mirrors that have not synced a fix yet); the release audit checks what
actually resolves, and holds the release on any advisory.

Exits non-zero, holding the release, when a floor has no advisory-free
successor.
"""
from __future__ import annotations

import json
import re
import sys
import urllib.request
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import InvalidVersion, Version

ROOT = Path(sys.argv[1]).resolve()
NOTES = Path(sys.argv[sys.argv.index("--notes") + 1]) if "--notes" in sys.argv else None
REQ = ROOT / "requirements.txt"


def osv(name: str, version: str) -> list[str]:
    body = json.dumps({"package": {"name": name, "ecosystem": "PyPI"}, "version": version}).encode()
    req = urllib.request.Request("https://api.osv.dev/v1/query", data=body,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return [v["id"] for v in json.load(r).get("vulns", []) if not v.get("withdrawn")]


def releases(name: str) -> list[Version]:
    """Final, non-yanked releases on PyPI, ascending."""
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/json", timeout=60) as r:
        data = json.load(r)
    out = []
    for ver, files in data.get("releases", {}).items():
        try:
            v = Version(ver)
        except InvalidVersion:
            continue
        if v.is_prerelease or v.is_devrelease or not files or all(f.get("yanked") for f in files):
            continue
        out.append(v)
    return sorted(out)


lines = REQ.read_text(encoding="utf-8").splitlines(keepends=True)
notes: list[str] = []
held: list[str] = []
for i, raw in enumerate(lines):
    body = raw.split("#", 1)[0].strip()
    if not body:
        continue
    req = Requirement(body)
    floors = [s for s in req.specifier if s.operator == ">="]
    if not floors:
        held.append(f"{req.name}: no `>=` floor to raise ({req.specifier or 'unbounded'})")
        continue
    floor = floors[0].version
    ids = osv(req.name, floor)
    if not ids:
        print(f"[bump] ok    {req.name}>={floor}")
        continue
    candidates = [v for v in releases(req.name) if v >= Version(floor) and req.specifier.contains(v)]
    fixed = next((v for v in candidates if not osv(req.name, str(v))), None)
    if fixed is None:
        held.append(f"{req.name}>={floor}: {len(ids)} advisories and no advisory-free release")
        continue
    new = re.sub(rf">=\s*{re.escape(floor)}\b", f">={fixed}", raw, count=1)
    lines[i] = new
    msg = f"{req.name} floor raised {floor} → {fixed} ({len(ids)} advisories at {floor}: {', '.join(ids[:6])}{'…' if len(ids) > 6 else ''})"
    print(f"[bump] raise {msg}")
    notes.append(f"- {msg}")

REQ.write_text("".join(lines), encoding="utf-8")

if NOTES:
    with NOTES.open("a", encoding="utf-8") as f:
        f.write("## Python dependency floors\n\n")
        f.write("\n".join(notes) + "\n" if notes else "No floor needed raising.\n")
if held:
    for h in held:
        print(f"[bump] HOLD  {h}")
    sys.exit(f"[bump] {len(held)} dependency floor(s) cannot be made advisory-free — release held")
