"""The release version rule: X.Y.Z from upstream, .N for horo revisions, .postN for rebuilds.

    python -m unittest discover -s overlay/tests -p 'test_*.py'
"""
from __future__ import annotations

import subprocess
import sys
import unittest
from pathlib import Path

from packaging.version import Version

SCRIPT = Path(__file__).resolve().parent.parent / "release_version.py"
sys.path.insert(0, str(SCRIPT.parent))
from release_version import check, upstream_base  # noqa: E402


class ReleaseVersion(unittest.TestCase):
    def test_accepted_versions_and_their_upstream(self):
        for version in ("0.52.544", "0.52.544.1", "0.52.544.12", "0.52.544.post1", "0.52.544.1.post2",
                        "0.52.544.post0", "1.0.0"):
            self.assertIsNone(check(version, f"v{upstream_base(version)}"), version)
        self.assertIsNone(check("0.52.416.1", "exp-v0.52.416"))

    def test_malformed_versions_are_refused(self):
        for version in ("0.52.544.0", "0.52.544.01", "0.52.544.1.1", "0.52", "v0.52.544", "0.52.544+horo1",
                        "0.52.544.post", "0.52.544.post01", "0.52.544-1", "00.52.544", "0.52.544.1a",
                        "0.52.544; rm -rf /", "0.52.544\n", ""):
            self.assertIsNotNone(check(version, "v0.52.544"), repr(version))

    def test_version_must_match_the_tag(self):
        self.assertIn("not 'v0.52.545'", check("0.52.544.1", "v0.52.545"))
        self.assertIsNotNone(check("0.52.544", "0.52.544"))
        self.assertIsNotNone(check("0.52.544", "v0.52.544.1"))

    def test_order_is_what_pip_sees(self):
        order = ["0.52.544", "0.52.544.post1", "0.52.544.1", "0.52.544.1.post1", "0.52.544.2", "0.52.545"]
        self.assertEqual(sorted(order, key=Version), order)

    def test_accepted_versions_are_already_normal(self):
        # The wheel is named after the normalised version; the workflow uses the input.
        for version in ("0.52.544", "0.52.544.1", "0.52.544.1.post2"):
            self.assertEqual(str(Version(version)), version)

    def test_command_line_exit_status(self):
        ok = subprocess.run([sys.executable, str(SCRIPT), "0.52.544.1", "v0.52.544"], capture_output=True)
        bad = subprocess.run([sys.executable, str(SCRIPT), "0.52.544.0", "v0.52.544"], capture_output=True)
        usage = subprocess.run([sys.executable, str(SCRIPT), "0.52.544"], capture_output=True)
        self.assertEqual((ok.returncode, bad.returncode, usage.returncode), (0, 1, 2))


if __name__ == "__main__":
    unittest.main()
