"""The sdist check must fail closed: each way an sdist can stop being the
wheel's source, or carry third-party code its audit never saw, holds it.

    python -m unittest discover -s overlay/tests -p 'test_*.py'

Built from small synthetic files; the real sdist and its offline rebuild are
checked by every release run.
"""
from __future__ import annotations

import io
import json
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import source_dist  # noqa: E402

V = "1.2.3.4"
TOP = f"horo_next_webui-{V}"
INFO = f"{TOP}.dist-info"
SHA = "0123456789abcdef0123456789abcdef01234567"

WHEEL = {
    "server.py": b"print('hi')\n",
    "static/app.js": b"console.log(1)\n",
    "static/vendor/katex/0.18.2/katex.min.js": b"/* katex */\n",
    f"{INFO}/METADATA": f"Metadata-Version: 2.4\nName: horo-next-webui\nVersion: {V}\n".encode(),
    f"{INFO}/licenses/LICENSE": b"MIT\n",
    f"{INFO}/licenses/THIRD_PARTY_LICENSES": b"notices\n",
}


def sdist_files() -> dict[str, bytes]:
    files = {n: d for n, d in WHEEL.items() if not n.startswith(INFO)}
    files.update({
        "PKG-INFO": f"Metadata-Version: 2.4\nName: horo-next-webui\nVersion: {V}\n".encode(),
        "pyproject.toml": b"[project]\n",
        "LICENSE": b"MIT\n",
        "THIRD_PARTY_LICENSES": b"notices\n",
        "tests/test_server.py": b"def test(): pass\n",
        "docs/guide.md": b"# guide\n",
        "horo/SOURCE.json": json.dumps({
            "version": V,
            "upstream": {"tag": "v1.2.3", "commit": SHA},
            "overlay": {"commit": SHA},
        }).encode(),
        "horo/overlay.patch": b"diff --git a/server.py b/server.py\n",
        "horo/overlay/horo_overlay.py": b"# overlay\n",
        "horo/overlay/mindmap/mindmap.js": WHEEL["static/app.js"],
        "horo/DEVELOPING.md": b"# offline\n",
    })
    return files


class SourceDist(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)

    def check(self, change=None, wheel_change=None, name=f"{TOP}.tar.gz") -> list[str]:
        files = sdist_files()
        if change:
            change(files)
        sdist = self.tmp / name
        with tarfile.open(sdist, "w:gz") as t:
            for n, d in files.items():
                info = tarfile.TarInfo(n if n.startswith("/") or n.startswith("..") else f"{TOP}/{n}")
                info.size = len(d)
                t.addfile(info, io.BytesIO(d))
        wheel_files = dict(WHEEL)
        if wheel_change:
            wheel_change(wheel_files)
        wheel = self.tmp / f"{TOP}-py3-none-any.whl"
        with zipfile.ZipFile(wheel, "w") as z:
            for n, d in wheel_files.items():
                z.writestr(n, d)
        with redirect_stdout(io.StringIO()):
            return source_dist.check_contents(sdist, wheel, V)

    def assertHeld(self, label: str, **kw):
        failures = self.check(**kw)
        self.assertTrue(any(label in f for f in failures), f"expected {label!r} in {failures}")

    def test_clean_sdist_passes(self):
        self.assertEqual(self.check(), [])

    def test_installed_file_missing(self):
        self.assertHeld("every file the wheel installs", change=lambda f: f.pop("server.py"))

    def test_installed_file_differs(self):
        self.assertHeld("same bytes", change=lambda f: f.update({"server.py": b"print('other')\n"}))

    def test_vendored_library_the_wheel_does_not_ship(self):
        def add(f):
            f["static/vendor/katex/0.16.22/katex.min.js"] = b"/* old katex */\n"
        self.assertHeld("static/ is exactly the wheel's", change=add)

    def test_script_in_tests(self):
        self.assertHeld("no script", change=lambda f: f.update({"tests/helper.min.js": b"/* lib */\n"}))

    def test_script_matching_the_wheel_is_allowed_only_byte_for_byte(self):
        self.assertHeld("no script", change=lambda f: f.update({"horo/overlay/mindmap/mindmap.js": b"changed\n"}))

    def test_lockfile(self):
        self.assertHeld("lockfile", change=lambda f: f.update({"horo/overlay/mermaid/package-lock.json": b"{}"}))

    def test_upstream_lockfile_at_the_top(self):
        self.assertHeld("what only the sdist has", change=lambda f: f.update({"uv.lock": b""}))

    def test_stray_directory(self):
        self.assertHeld("what only the sdist has", change=lambda f: f.update({"node_modules/x/index.txt": b""}))

    def test_page_loading_remote_script(self):
        page = b'<script src="https://cdn.example/lib.js"></script>'
        self.assertHeld("remote", change=lambda f: f.update({"tests/page.html": page}))

    def test_protocol_relative_url(self):
        page = b"<link href='//cdn.example/x.css'>"
        self.assertHeld("remote", change=lambda f: f.update({"tests/page.html": page}))

    def test_no_tests(self):
        self.assertHeld("upstream's tests", change=lambda f: f.pop("tests/test_server.py"))

    def test_notices_differ(self):
        self.assertHeld("THIRD_PARTY_LICENSES", change=lambda f: f.update({"THIRD_PARTY_LICENSES": b"other\n"}))

    def test_wrong_version(self):
        def pkg(f):
            f["PKG-INFO"] = b"Metadata-Version: 2.4\nName: horo-next-webui\nVersion: 1.2.3.5\n"
        self.assertHeld("metadata name this version", change=pkg)

    def test_wrong_file_name(self):
        self.assertHeld("named for this version", name="horo_next_webui-9.9.9.tar.gz")

    def test_source_json_missing(self):
        self.assertHeld("SOURCE.json is readable", change=lambda f: f.pop("horo/SOURCE.json"))

    def test_source_json_without_overlay_commit(self):
        def edit(f):
            s = json.loads(f["horo/SOURCE.json"])
            s["overlay"]["commit"] = "main"
            f["horo/SOURCE.json"] = json.dumps(s).encode()
        self.assertHeld("overlay commit", change=edit)

    def test_source_json_for_another_version(self):
        def edit(f):
            s = json.loads(f["horo/SOURCE.json"])
            s["version"] = "1.2.3"
            f["horo/SOURCE.json"] = json.dumps(s).encode()
        self.assertHeld("SOURCE.json names this version", change=edit)

    def test_empty_patch(self):
        self.assertHeld("is a patch", change=lambda f: f.update({"horo/overlay.patch": b""}))

    def test_path_outside_the_top_directory(self):
        self.assertHeld("one top directory", change=lambda f: f.update({"../evil.py": b""}))

    def test_empty_wheel_does_not_pass(self):
        def empty(w):
            for n in [n for n in w if not n.startswith(INFO)]:
                w.pop(n)
        self.assertHeld("every file the wheel installs", wheel_change=empty)


if __name__ == "__main__":
    unittest.main()
