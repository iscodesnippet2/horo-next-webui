"""The mind map's read model, against a real SQLite file and stubbed WebUI modules.

    python -m unittest discover -s overlay/tests -p 'test_*.py'
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT NOT NULL, title TEXT, parent_session_id TEXT,
  end_reason TEXT, ended_at REAL, started_at REAL NOT NULL, last_activity_at REAL,
  last_activity_description TEXT, message_count INTEGER DEFAULT 0);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, role TEXT NOT NULL,
  content TEXT, timestamp REAL NOT NULL);
"""
stubs = {"profiles": [], "homes": {}, "live": []}


def _install_stubs() -> None:
    api = types.ModuleType("api")
    helpers = types.ModuleType("api.helpers")
    helpers._redact_text = lambda text: text.replace("sk-secret", "[REDACTED]")
    models = types.ModuleType("api.models")
    models.all_sessions = lambda **_kw: stubs["live"]
    profiles = types.ModuleType("api.profiles")
    profiles.list_profiles_api = lambda: [{"name": n} for n in stubs["profiles"]]
    profiles.get_hermes_home_for_profile = lambda name: stubs["homes"][name]
    sys.modules.update({"api": api, "api.helpers": helpers, "api.models": models, "api.profiles": profiles})


_install_stubs()
sys.path.insert(0, str(HERE.parent / "mindmap"))
import horo_overview as overview  # noqa: E402


def no_attention(_sid):
    return None


class Overview(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.now = time.time()
        stubs.update(profiles=["default"], homes={"default": Path(self.tmp.name)}, live=[])
        self.conn = sqlite3.connect(Path(self.tmp.name) / "state.db")
        self.addCleanup(self.conn.close)
        self.conn.executescript(SCHEMA)

    def add(self, sid, source="desktop", ago=3600, ended=True, end_reason=None, parent=None, text="做事"):
        activity = self.now - ago
        self.conn.execute("INSERT INTO sessions VALUES (?,?,?,?,?,?,?,?,?,?)",
                          (sid, source, sid, parent, end_reason, activity if ended else None, activity, activity, None, 2))
        self.conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                          (sid, "user", text, activity))
        self.conn.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,?,?,?)",
                          (sid, "assistant", "回覆", activity))
        self.conn.commit()

    def build(self, attention=no_attention):
        return overview.build(attention)

    def ids(self):
        return {s["id"] for s in self.build()["sessions"]}

    def test_only_human_sources(self):
        for source in overview.HUMAN_SOURCES:
            self.add(f"h-{source}", source=source)
        for source in ("cron", "kanban", "oneshot", "webhook", "subagent", "a2a", "tool", "bot_room", "new_source"):
            self.add(f"m-{source}", source=source)
        self.assertEqual(self.ids(), {f"h-{s}" for s in overview.HUMAN_SOURCES})

    def test_window_and_compression(self):
        self.add("recent")
        self.add("old", ago=73 * 3600)
        self.add("compressed", end_reason="compression")
        self.assertEqual(self.ids(), {"recent"})

    def test_status(self):
        self.add("idle")
        self.add("open", ended=False, ago=30)
        self.add("reaped", end_reason="ws_orphan_reap")
        self.add("web", source="webui", ended=False, ago=30)
        self.add("streaming", source="webui")
        self.add("asking")
        stubs["live"] = [{"session_id": "streaming", "is_streaming": True}]
        pending = {"asking": {"kind": "approval", "count": 1}}
        got = {s["id"]: s["status"] for s in self.build(pending.get)["sessions"]}
        self.assertEqual(got, {"idle": "done", "open": "running", "reaped": "failed", "web": "done",
                               "streaming": "running", "asking": "waiting"})

    def test_attention_outranks_streaming(self):
        self.add("both", source="webui")
        stubs["live"] = [{"session_id": "both", "is_streaming": True}]
        got = self.build({"both": {"kind": "clarify"}}.get)["sessions"][0]
        self.assertEqual((got["status"], got["next"]), ("waiting", "等待你回答問題"))

    def test_text_is_redacted(self):
        self.add("leak", text="金鑰 sk-secret")
        got = self.build()["sessions"][0]
        self.assertNotIn("sk-secret", str(got))
        self.assertIn("[REDACTED]", got["goal"])

    def test_archived_hidden_and_pin_kept(self):
        self.add("gone")
        self.add("kept")
        stubs["live"] = [{"session_id": "gone", "archived": True}, {"session_id": "kept", "pinned": True}]
        sessions = self.build()["sessions"]
        self.assertEqual([(s["id"], s["pinned"]) for s in sessions], [("kept", True)])

    def test_links(self):
        self.add("root")
        self.add("fork", parent="root")
        self.add("ghost", parent="not-on-map")
        self.assertEqual(self.build()["links"], [{"from": "root", "to": "fork", "type": "fork"}])

    def test_compression_child_is_not_a_fork(self):
        self.add("before", end_reason="compression")
        self.add("after", parent="before")
        got = self.build()
        self.assertEqual({s["id"] for s in got["sessions"]}, {"after"})
        self.assertEqual(got["links"], [])

    def test_unchanged_version(self):
        self.add("one")
        version = self.build()["version"]
        self.assertEqual(overview.build(no_attention, version), {"unchanged": True, "version": version})
        self.add("two")
        self.assertIn("sessions", overview.build(no_attention, version))

    def test_unreadable_profile_is_reported_not_dropped(self):
        broken = Path(self.tmp.name) / "broken"
        broken.mkdir()
        (broken / "state.db").write_text("not a database")
        self.add("fine")
        stubs["profiles"] = ["default", "broken"]
        stubs["homes"]["broken"] = broken
        got = self.build()
        self.assertEqual([e["profile"] for e in got["errors"]], ["broken"])
        self.assertEqual({s["id"] for s in got["sessions"]}, {"fine"})

    def test_profile_without_database_is_empty(self):
        stubs["profiles"] = ["default", "new"]
        stubs["homes"]["new"] = Path(self.tmp.name) / "missing"
        self.assertEqual(self.build()["errors"], [])

    def test_never_writes(self):
        self.add("one")
        self.conn.close()
        path = Path(self.tmp.name) / "state.db"
        before = path.read_bytes()
        self.build()
        self.assertEqual(path.read_bytes(), before)


if __name__ == "__main__":
    unittest.main()
