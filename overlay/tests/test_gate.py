"""The release gate must fail closed. Runs offline against tests/fakes.py.

    python -m unittest discover -s overlay/tests -p 'test_*.py'

Sonatype's real behaviour is exercised by every CI release run; these cover
the failures a healthy run never reaches.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
OVERLAY = HERE.parent
sys.path.insert(0, str(HERE))
from fakes import State, serve, tarball  # noqa: E402

PURLS = ["pkg:pypi/typing_extensions@4.0.0", "pkg:pypi/pyyaml@6.0.3", "pkg:npm/%40xterm/xterm@5.5.0", "pkg:npm/DOMPurify@3.4.17"]
VULN = {"id": "sonatype-2026-1", "cve": "CVE-2026-1", "cvssScore": 9.1, "title": "bad", "reference": "https://x.invalid"}


def age(days: int) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class Base(unittest.TestCase):
    def setUp(self):
        self.state = State()
        self.srv, self.base = serve(self.state)
        self.addCleanup(self.srv.server_close)
        self.addCleanup(self.srv.shutdown)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.env = {**os.environ, "HORO_OSV_URL": self.base, "HORO_PYPI_URL": self.base, "HORO_NPM_REGISTRY": self.base,
                    "SONATYPE_GUIDE_URL": f"{self.base}/api/v3/component-report", "SONATYPE_GUIDE_TOKEN": "fake"}

    def run_py(self, script: str, *args, env=None):
        return subprocess.run([sys.executable, str(OVERLAY / script), *map(str, args)], env=env or self.env,
                              capture_output=True, text=True, timeout=120)


class Audit(Base):
    def audit(self, env=None):
        bom = self.tmp / "b.cdx.json"
        bom.write_text(json.dumps({"components": [{"purl": p} for p in PURLS]}))
        out = self.tmp / "audit.json"
        r = self.run_py("audit_shipped.py", bom, "--json", out, env=env)
        return r, json.loads(out.read_text()) if out.exists() else {}

    def assertHeld(self, r):
        self.assertNotEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertNotIn("[audit] clean", r.stdout)

    def test_clean(self):
        r, rep = self.audit()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual({a["result"] for a in rep["audits"]}, {"pass"})
        self.assertEqual(len(rep["audits"]), 2)

    def test_normalized_echo_matches(self):
        """`%40` and case differ in Sonatype's echo; a finding still lands on the component."""
        self.state.sonatype["pkg:npm/%40xterm/xterm@5.5.0"] = [VULN]
        r, rep = self.audit()
        self.assertHeld(r)
        self.assertIn("pkg:npm/%40xterm/xterm@5.5.0", r.stdout)

    def test_pypi_name_normalized(self):
        self.state.sonatype["pkg:pypi/typing_extensions@4.0.0"] = [VULN]
        self.assertHeld(self.audit()[0])

    def test_sonatype_finding(self):
        self.state.sonatype["pkg:pypi/pyyaml@6.0.3"] = [VULN]
        r, rep = self.audit()
        self.assertHeld(r)
        self.assertEqual(rep["audits"][1]["result"], "fail")

    def test_osv_finding(self):
        self.state.osv[("pyyaml", "6.0.3")] = ["GHSA-x"]
        self.assertHeld(self.audit()[0])

    def test_scoped_name_reaches_osv(self):
        self.state.osv[("@xterm/xterm", "5.5.0")] = ["GHSA-x"]
        self.assertHeld(self.audit()[0])

    def test_no_token(self):
        env = {k: v for k, v in self.env.items() if k != "SONATYPE_GUIDE_TOKEN"}
        r, rep = self.audit(env)
        self.assertHeld(r)
        self.assertIn("not set", r.stdout)
        self.assertEqual(rep["audits"][1]["result"], "fail")

    def test_rejected_token(self):
        self.state.sonatype_status = 401
        r, _ = self.audit()
        self.assertHeld(r)
        self.assertIn("rejected", r.stdout)

    def test_server_error(self):
        self.state.sonatype_status = 500
        self.assertHeld(self.audit()[0])

    def test_unanswered_component(self):
        self.state.sonatype_drop.add("pkg:pypi/pyyaml@6.0.3")
        r, _ = self.audit()
        self.assertHeld(r)
        self.assertIn("NOT-RETURNED", r.stdout)

    def test_answer_without_the_field(self):
        self.state.sonatype_no_field.add("pkg:pypi/pyyaml@6.0.3")
        self.assertHeld(self.audit()[0])

    def test_osv_short_answer(self):
        self.state.osv_short = True
        r, _ = self.audit()
        self.assertHeld(r)
        self.assertIn("OSV answered for", r.stdout)

    def test_unreachable(self):
        env = {**self.env, "SONATYPE_GUIDE_URL": "http://127.0.0.1:9/x"}
        self.assertHeld(self.audit(env)[0])

    def test_empty_sbom(self):
        bom = self.tmp / "e.json"
        bom.write_text('{"components": []}')
        self.assertNotEqual(self.run_py("audit_shipped.py", bom).returncode, 0)


class Floors(Base):
    def bump(self):
        (self.tmp / "requirements.txt").write_text("demo>=1.0\n")
        return self.run_py("security_bump.py", self.tmp), (self.tmp / "requirements.txt").read_text()

    def test_raises_past_sonatype_only_finding(self):
        self.state.osv[("demo", "1.0")] = ["GHSA-a"]
        self.state.pypi["demo"] = {"1.0": age(100), "1.1": age(50), "1.2": age(40)}
        self.state.sonatype["pkg:pypi/demo@1.1"] = [VULN]
        r, req = self.bump()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(">=1.2", req)

    def test_only_fix_in_cooldown_holds(self):
        self.state.osv[("demo", "1.0")] = ["GHSA-a"]
        self.state.pypi["demo"] = {"1.0": age(100), "1.1": age(3)}
        r, req = self.bump()
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cooldown", r.stdout)
        self.assertIn(">=1.0", req)

    def test_aged_fix_passes(self):
        self.state.osv[("demo", "1.0")] = ["GHSA-a"]
        self.state.pypi["demo"] = {"1.0": age(100), "1.1": age(15)}
        r, req = self.bump()
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(">=1.1", req)


class FrontEnd(Base):
    def vendor(self, lib: str, path: str):
        root = self.tmp / "up"
        (root / "static" / "vendor").mkdir(parents=True)
        (root / "static" / "index.html").write_text(f'<script src="https://cdn.jsdelivr.net/npm/{lib}@1.0.0/{path}"></script>')
        return self.run_py("vendor_assets.py", root), root

    def test_only_fix_in_cooldown_holds(self):
        self.state.osv[("demo", "1.0.0")] = ["GHSA-a"]
        self.state.npm["demo"] = {"1.0.0": age(100), "1.0.1": age(3)}
        r, _ = self.vendor("demo", "a.js")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("inside the 14-day cooldown", r.stdout)

    def test_unanswered_candidate_is_not_chosen(self):
        self.state.osv[("demo", "1.0.0")] = ["GHSA-a"]
        self.state.npm["demo"] = {"1.0.0": age(100), "1.0.1": age(50)}
        self.state.sonatype_drop.add("pkg:npm/demo@1.0.1")
        r, _ = self.vendor("demo", "a.js")
        self.assertIn("demo@1.0.0", r.stdout)
        self.assertIn("no clean semver-compatible release", r.stdout)

    def test_fix_not_locked_holds(self):
        """`npm update` that leaves the vulnerable version in the lock must stop the build."""
        name = "mermaid"
        lock = json.loads((OVERLAY / "mermaid" / "package-lock.json").read_text())["packages"]
        ver = lock["node_modules/mermaid"]["version"]
        self.state.npm[name] = {ver: age(100)}
        self.state.tarballs[(name, ver)] = tarball({"dist/mermaid.min.js": "//"})
        self.state.osv[("dompurify", lock["node_modules/dompurify"]["version"])] = ["GHSA-b"]
        root = self.tmp / "up"
        (root / "static" / "vendor").mkdir(parents=True)
        (root / "static" / "index.html").write_text(
            f'<script src="https://cdn.jsdelivr.net/npm/mermaid@{ver}/dist/mermaid.min.js"></script>')
        noop = self.tmp / "npm"
        noop.write_text("#!/bin/sh\nexit 0\n")
        noop.chmod(0o755)
        r = self.run_py("vendor_assets.py", root, env={**self.env, "HORO_NPM": str(noop)})
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("still locked after `npm update`", r.stdout)


if __name__ == "__main__":
    unittest.main()
