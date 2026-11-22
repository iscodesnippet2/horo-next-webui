"""The admin policy (overlay/policy/horo_policy.py), against real files and a stand-in settings store.

    python -m unittest discover -s overlay/tests -p 'test_*.py'

A test cannot make a file the running user does not own. The ownership and
write checks are real, and each test chooses what they see: by default the
policy file belongs to someone else and nobody here can write it or its
folder (`trusted`); the permission tests turn one of those back to the truth.
"""
from __future__ import annotations

import io
import json
import os
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "policy"))
import horo_policy as hp  # noqa: E402

REAL_GETEUID, REAL_ACCESS = os.geteuid, os.access

DEFAULTS = {"hidden_tabs": [], "tab_order": [], "theme": "dark", "check_for_updates": False,
            "hide_composer_yolo": False, "hide_composer_toolsets": False, "send_key": "enter",
            "pinned_sessions_limit": 3, "tts_rate": 1.0, "provider_cost_budget": None}
SCHEMA = dict(allowed=set(DEFAULTS) - {"password_hash"} | {"language"}, defaults=DEFAULTS,
              bools={"check_for_updates", "hide_composer_yolo", "hide_composer_toolsets"},
              enums={"send_key": {"enter", "ctrl+enter"}}, ints={"pinned_sessions_limit": (1, 99)},
              floats={"tts_rate": (0.5, 2.0)})


class Store:
    """upstream load_settings/save_settings over a settings.json, with upstream's tab cleanup."""

    def __init__(self, path: Path):
        self.path = path

    def raw(self) -> dict:
        return json.loads(self.path.read_text()) if self.path.exists() else {}

    def load(self) -> dict:
        return {**DEFAULTS, **self.raw()}

    def save(self, body: dict) -> dict:
        current = self.load()
        for k, v in body.items():
            if k == "hidden_tabs":
                v = [t for t in v if t not in ("chat", "settings")]
            if k in SCHEMA["allowed"]:
                current[k] = v
        self.path.write_text(json.dumps(current))
        return current


class PolicyCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "share"
        self.dir.mkdir()
        self.file = self.dir / "webui-policy.json"
        env = unittest.mock.patch.dict(os.environ, {hp.ENV: str(self.file)})
        env.start()
        self.addCleanup(env.stop)
        quiet = unittest.mock.patch("sys.stderr", new_callable=io.StringIO)  # what the server would log
        quiet.start()
        self.addCleanup(quiet.stop)
        hp.configure(**SCHEMA)
        self.store = Store(Path(tmp.name) / "settings.json")
        self.load = hp.wrap_load(self.store.load)
        self.save = hp.wrap_save(self.store.save, self.store.load)
        self.owner_is_me = False
        self.writable: set[Path] = set()
        def access(path, mode, *a, **kw):
            p = Path(path).resolve()
            if mode == os.W_OK and p in (self.file.resolve(), self.dir.resolve()):
                return p in self.writable
            return REAL_ACCESS(path, mode, *a, **kw)

        for target, value in (("os.geteuid", lambda: os.stat(self.dir).st_uid if self.owner_is_me else -2),
                              ("os.access", access)):
            patcher = unittest.mock.patch(target, side_effect=value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def write(self, policy) -> None:
        text = policy if isinstance(policy, str) else json.dumps(policy)
        self.file.write_text(text)
        # A rewrite within one mtime tick must still be read: force a new stamp.
        st = self.file.stat()
        os.utime(self.file, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))

    def assert_refused(self, reason: str):
        policy, error, path = hp.current()
        self.assertIsNone(policy)
        self.assertIn(reason, error)
        status, body = hp.refusal("/api/settings")
        self.assertEqual(status, 503)
        self.assertEqual(body["horo_policy_error"], {"path": path, "reason": error})


class Loading(PolicyCase):
    def test_minimal_policy_applies(self):
        self.write({"version": 1})
        self.assertEqual(hp.current()[0], {"settings": {}, "hide_tabs": [], "block_features": [],
                                           "allow_commands": None, "deny_commands": []})
        self.assertIsNone(hp.refusal("/api/settings"))

    def test_environment_path_is_read_on_every_call(self):
        self.write({"version": 1})
        self.assertIsNotNone(hp.current()[0])
        with unittest.mock.patch.dict(os.environ, {hp.ENV: str(self.dir / "elsewhere.json")}):
            self.assert_refused("no policy file")
        self.assertIsNotNone(hp.current()[0])

    def test_default_path_without_environment(self):
        self.write({"version": 1})
        with unittest.mock.patch.dict(os.environ, clear=False) as env, \
                unittest.mock.patch.object(hp, "DEFAULT_PATH", str(self.file)):
            env.pop(hp.ENV)
            self.assertEqual(hp.current()[2], str(self.file))
            self.assertIsNotNone(hp.current()[0])

    def test_no_environment_and_no_default_file(self):
        with unittest.mock.patch.dict(os.environ, clear=False) as env, \
                unittest.mock.patch.object(hp, "DEFAULT_PATH", str(self.dir / "absent.json")):
            env.pop(hp.ENV)
            self.assert_refused("no policy file")

    def test_missing_unreadable_and_broken(self):
        self.assert_refused("no policy file")
        self.write("{not json")
        self.assert_refused("cannot be read as JSON")
        self.write(b"\xff\xfe".decode("latin-1"))
        self.assert_refused("cannot be read as JSON")
        self.write({"version": 1})
        with unittest.mock.patch("builtins.open", side_effect=PermissionError(13, "Permission denied")):
            # Changed on disk, so it is read again: the read fails.
            self.write({"version": 1, "hide_tabs": []})
            self.assert_refused("cannot be read as JSON")
        self.file.unlink()
        self.file.mkdir()
        self.assert_refused("not a regular file")

    def test_invalid_content(self):
        for policy, reason in (
            ([], "JSON object"),
            ({}, "version must be 1"),
            ({"version": 2}, "version must be 1"),
            ({"version": True}, "version must be 1"),
            ({"version": 1, "extra": 1}, "unknown key(s): extra"),
            ({"version": 1, "settings": []}, "settings must be an object"),
            ({"version": 1, "settings": {"no_such": True}}, "not a WebUI setting"),
            ({"version": 1, "settings": {"hidden_tabs": ["kanban"]}}, "cannot be set by policy"),
            ({"version": 1, "settings": {"tab_order": []}}, "cannot be set by policy"),
            ({"version": 1, "settings": {"password_hash": "x"}}, "cannot be set by policy"),
            ({"version": 1, "settings": {"hide_composer_yolo": "yes"}}, "must be true or false"),
            ({"version": 1, "settings": {"send_key": "space"}}, "must be one of"),
            ({"version": 1, "settings": {"pinned_sessions_limit": 0}}, "integer from 1 to 99"),
            ({"version": 1, "settings": {"pinned_sessions_limit": True}}, "integer from 1 to 99"),
            ({"version": 1, "settings": {"tts_rate": 9}}, "number from 0.5 to 2.0"),
            ({"version": 1, "settings": {"theme": 3}}, "must be a str"),
            ({"version": 1, "hide_tabs": "kanban"}, "list of names"),
            ({"version": 1, "hide_tabs": ["chat"]}, "chat can never be hidden"),
            ({"version": 1, "hide_tabs": ["skills"]}, "skills can never be hidden"),
            ({"version": 1, "hide_tabs": ["nope"]}, "unknown name 'nope'"),
            ({"version": 1, "block_features": ["skills"]}, "unknown name 'skills'"),
            ({"version": 1, "block_features": ["workspaces"]}, "unknown name 'workspaces'"),
            ({"version": 1, "block_features": [1]}, "list of names"),
        ):
            with self.subTest(policy=policy):
                self.write(policy)
                self.assert_refused(reason)

    def test_never_falls_back_to_the_last_good_policy(self):
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}})
        self.assertTrue(self.load()["hide_composer_yolo"])
        self.write("{")
        self.assert_refused("cannot be read as JSON")
        self.file.unlink()
        self.assert_refused("no policy file")
        self.write({"version": 1})
        self.assertIsNone(hp.refusal("/api/settings"))

    def test_unconfigured_schema_refuses(self):
        self.write({"version": 1})
        with unittest.mock.patch.object(hp, "_schema", None):
            hp._cache.update(key=None)
            self.assert_refused("schema is not configured")

    def test_reparsed_only_when_changed(self):
        self.write({"version": 1})
        hp.current()
        with unittest.mock.patch("builtins.open", side_effect=AssertionError("read again")):
            self.assertIsNotNone(hp.current()[0])

    def test_block_feature_hides_its_tab(self):
        self.write({"version": 1, "hide_tabs": ["logs", "logs"], "block_features": ["kanban", "terminal"]})
        self.assertEqual(hp.current()[0]["hide_tabs"], ["logs", "kanban"])


class Permissions(PolicyCase):
    def setUp(self):
        super().setUp()
        self.write({"version": 1})

    def test_owned_by_the_user(self):
        self.owner_is_me = True
        self.assert_refused("owned by the user running the WebUI")

    def test_file_writable(self):
        self.writable.add(self.file.resolve())
        self.assert_refused("can write the policy file")

    def test_folder_writable(self):
        self.writable.add(self.dir.resolve())
        self.assert_refused("can write the folder")

    def test_checked_on_every_call_not_cached(self):
        self.assertIsNotNone(hp.current()[0])
        self.writable.add(self.dir.resolve())
        self.assert_refused("can write the folder")
        self.writable.clear()
        self.assertIsNotNone(hp.current()[0])

    def test_symlink_is_judged_by_its_target(self):
        link = self.dir.parent / "link.json"
        link.symlink_to(self.file)
        with unittest.mock.patch.dict(os.environ, {hp.ENV: str(link)}):
            self.assertIsNotNone(hp.current()[0])
            self.writable.add(self.file.resolve())
            self.assert_refused("can write the policy file")

    def test_real_check_refuses_a_file_the_user_wrote(self):
        # No stand-ins: this process owns the file it just wrote.
        with unittest.mock.patch("os.geteuid", REAL_GETEUID), unittest.mock.patch("os.access", REAL_ACCESS):
            self.assertEqual(self.file.stat().st_uid, os.geteuid())
            self.assert_refused("owned by the user running the WebUI")


class Settings(PolicyCase):
    def test_forced_values_and_tab_union_on_load(self):
        self.store.path.write_text(json.dumps({"hidden_tabs": ["logs"], "hide_composer_yolo": False}))
        self.write({"version": 1, "settings": {"hide_composer_yolo": True, "check_for_updates": False},
                    "hide_tabs": ["kanban", "logs", "settings"]})
        s = self.load()
        self.assertTrue(s["hide_composer_yolo"])
        self.assertEqual(s["hidden_tabs"], ["logs", "kanban", "settings"])

    def test_locked_keys_are_not_written_and_policy_never_reaches_the_file(self):
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}, "hide_tabs": ["kanban", "settings"]})
        out = self.save({"hide_composer_yolo": False, "theme": "light",
                         "hidden_tabs": ["kanban", "settings", "memory"]})
        self.assertTrue(out["hide_composer_yolo"])
        self.assertEqual(out["theme"], "light")
        self.assertEqual(out["hidden_tabs"], ["memory", "kanban", "settings"])
        raw = self.store.raw()
        self.assertIs(raw["hide_composer_yolo"], False)  # the user's value, not the policy's
        self.assertEqual(raw["hidden_tabs"], ["memory"])
        self.assertNotIn("horo_policy", raw)
        # Saving what was loaded writes back no policy value either.
        self.save(self.load())
        self.assertEqual(self.store.raw()["hidden_tabs"], ["memory"])
        self.assertFalse(self.store.raw()["hide_composer_yolo"])

    def test_a_tab_the_user_hid_themselves_stays_theirs(self):
        self.store.path.write_text(json.dumps({"hidden_tabs": ["kanban"]}))
        self.write({"version": 1, "hide_tabs": ["kanban"]})
        self.save({"hidden_tabs": ["kanban"]})
        self.assertEqual(self.store.raw()["hidden_tabs"], ["kanban"])
        self.write({"version": 1})
        self.assertEqual(self.load()["hidden_tabs"], ["kanban"])

    def test_user_choices_return_when_the_policy_changes(self):
        self.store.path.write_text(json.dumps({"hide_composer_yolo": False}))
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}})
        self.assertTrue(self.load()["hide_composer_yolo"])
        self.write({"version": 1})
        self.assertFalse(self.load()["hide_composer_yolo"])

    def test_policy_hides_settings_but_the_user_cannot(self):
        self.write({"version": 1})
        self.save({"hidden_tabs": ["settings", "logs"]})
        self.assertEqual(self.load()["hidden_tabs"], ["logs"])
        self.write({"version": 1, "hide_tabs": ["settings"]})
        self.assertEqual(self.load()["hidden_tabs"], ["logs", "settings"])

    def test_without_a_policy(self):
        self.store.path.write_text(json.dumps({"theme": "light"}))
        # Reads go on (the password hash is needed for /login); every API is refused.
        self.assertEqual(self.load()["theme"], "light")
        with self.assertRaises(hp.PolicyUnavailable):
            self.save({"theme": "dark"})
        self.assertEqual(self.store.raw()["theme"], "light")
        # The password is not lockable: a legacy hash may still be upgraded at login.
        self.save({"_set_password": "x"})

    def test_client_view(self):
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}, "block_features": ["logs"]})
        self.assertEqual(hp.client_view(), {"path": str(self.file), "settings": ["hide_composer_yolo"],
                                            "hide_tabs": ["logs"], "block_features": ["logs"],
                                            "allow_commands": None, "deny_commands": ["yolo"]})
        self.file.unlink()
        self.assertEqual(hp.client_view()["path"], str(self.file))
        self.assertIn("no policy file", hp.client_view()["error"])


class Refusal(PolicyCase):
    def test_blocked_prefixes_and_their_edges(self):
        self.write({"version": 1, "block_features": list(hp.FEATURES)})
        for path in ("/api/kanban", "/api/kanban/board", "/api/crons", "/api/crons/run", "/api/memory/write",
                     "/api/insights", "/api/logs", "/api/terminal/start", "/api/mcp/servers", "/api/extensions/status",
                     "/api/plugins", "/api/dashboard/config", "/extensions/x.js", "/plugins/p/a.js",
                     "/dashboard-plugins/x", "/API/Kanban/board", "/api/session/../kanban/board", "//api/logs"):
            with self.subTest(path=path):
                status, body = hp.refusal(path)
                self.assertEqual(status, 403)
                self.assertIn("horo_policy_blocked", body)
        for path in ("/api/memoryx", "/api/kanbanboard", "/api/logsearch", "/api/session", "/api/sessions",
                     "/api/chat/start", "/api/settings", "/api/workspaces", "/api/file", "/api/skills",
                     "/api/profile/active", "/api/horo/overview", "/", "/static/ui.js"):
            with self.subTest(path=path):
                self.assertIsNone(hp.refusal(path))

    def test_without_a_policy_only_the_shell_login_and_health_are_served(self):
        for path in ("/", "/index.html", "/sessions", "/session/abc", "/static/ui.js", "/health", "/login",
                     "/api/auth/login", "/api/auth/status", "/favicon.ico", "/sw.js", "/manifest.json"):
            with self.subTest(path=path):
                self.assertIsNone(hp.refusal(path))
        for path in ("/api/settings", "/api/session", "/api/chat/start", "/api/authx", "/share/abc",
                     "/plugins/x.js", "/static/../api/settings", "/healthz", "/api/horo/overview"):
            with self.subTest(path=path):
                self.assertEqual(hp.refusal(path)[0], 503)


class Commands(PolicyCase):
    def test_absent_key_allows_every_command(self):
        self.write({"version": 1})
        policy = hp.current()[0]
        for name in sorted(hp.COMMANDS) + ["agents", "some-plugin"]:
            self.assertTrue(hp.command_allowed(policy, name), name)
        self.assertIsNone(hp.command_refusal("/api/commands/exec", {"command": "/reload-mcp"}))
        self.assertIsNone(hp.command_refusal("/api/session/yolo", {"session_id": "s"}))
        self.assertIsNone(hp.refusal("/api/btw"))

    def test_validation(self):
        for allow, reason in (("help", "list of names"), ([1], "list of names"), (["Help"], "unknown name"),
                              (["nope"], "unknown name"), (["compact"], "write 'compress'"),
                              (["use"], "write 'skills'"), (["reload_mcp"], "write 'reload-mcp'"),
                              (["/model"], "unknown name"), (None, "list of names")):
            with self.subTest(allow=allow):
                self.write({"version": 1, "allow_commands": allow})
                self.assert_refused(reason)
        self.write({"version": 1, "allow_commands": []})
        self.assertEqual(hp.current()[0]["allow_commands"], [])
        self.write({"version": 1, "allow_commands": ["model", "help", "model"]})
        self.assertEqual(hp.current()[0]["allow_commands"], ["model", "help"])

    def test_allowlist(self):
        self.write({"version": 1, "allow_commands": ["model", "skills"]})
        policy = hp.current()[0]
        for name in ("model", "skills", "use", "help", "HELP"):
            self.assertTrue(hp.command_allowed(policy, name), name)
        # Not listed, and not known to this policy at all (a later upstream's command).
        for name in ("yolo", "compress", "compact", "plugins", "reload-mcp", "agents", "brand-new"):
            self.assertFalse(hp.command_allowed(policy, name), name)
        self.write({"version": 1, "allow_commands": []})
        policy = hp.current()[0]
        self.assertTrue(hp.command_allowed(policy, "help"))
        self.assertFalse(hp.command_allowed(policy, "new"))

    def test_dedicated_endpoints_are_refused(self):
        self.write({"version": 1, "allow_commands": ["new"]})
        for path, name in (("/api/commands/moa/resolve", "moa"), ("/api/btw", "btw"),
                           ("/api/background", "background"), ("/api/goal", "goal"),
                           ("/api/session/compress", "compress"), ("/api/session/compress/start", "compress"),
                           ("/api/session/retry", "retry"), ("/api/commands/bundles/resolve", "skills"),
                           ("/API/Goal", "goal"), ("/api/session/../btw", "btw")):
            with self.subTest(path=path):
                status, body = hp.refusal(path)
                self.assertEqual((status, body["horo_policy_blocked_command"]), (403, name))
        # Shared with the page: never refused for a command's sake.
        for path in ("/api/background/status", "/api/session/compress/status", "/api/commands",
                     "/api/commands/bundles", "/api/skills", "/api/skills/content", "/api/session/yolo",
                     "/api/session/branch", "/api/session/undo", "/api/chat/steer", "/api/session/update",
                     "/api/chat/start", "/api/approval/respond", "/api/commands/exec"):
            with self.subTest(path=path):
                self.assertIsNone(hp.refusal(path))
        self.write({"version": 1, "allow_commands": list(hp.COMMAND_ENDPOINTS)})
        for path in ("/api/btw", "/api/goal", "/api/commands/bundles/resolve"):
            self.assertIsNone(hp.refusal(path))

    def test_exec(self):
        self.write({"version": 1, "allow_commands": ["reload-skills"]})
        for command, name in (("/reload-mcp", "reload-mcp"), ("/reload_mcp now", "reload-mcp"),
                              ("CREDITS", "credits"), ("/codex_runtime on", "codex-runtime"),
                              ("/my-plugin x", "plugins"), ("/help", "plugins"), ("", "plugins")):
            with self.subTest(command=command):
                status, body = hp.command_refusal("/api/commands/exec", {"command": command})
                self.assertEqual((status, body["horo_policy_blocked_command"]), (403, name))
        for command in ("/reload-skills", "/reload_skills", " reload-skills "):
            self.assertIsNone(hp.command_refusal("/api/commands/exec", {"command": command}))
        self.assertEqual(hp.command_refusal("/api/commands/exec", ["/reload-skills"])[0], 403)
        self.write({"version": 1, "allow_commands": ["plugins"]})
        self.assertIsNone(hp.command_refusal("/api/commands/exec", {"command": "/my-plugin"}))
        self.assertEqual(hp.command_refusal("/api/commands/exec", {"command": "/credits"})[0], 403)

    def test_yolo_may_be_turned_off_never_on(self):
        self.write({"version": 1, "allow_commands": ["new"]})
        for path, body in (("/api/session/yolo", {"session_id": "s"}),
                           ("/api/session/yolo", {"session_id": "s", "enabled": True}),
                           ("/api/session/yolo", {"session_id": "s", "enabled": "false"}),
                           ("/api/approval/respond", {"session_id": "s", "choice": "once", "yolo": True})):
            with self.subTest(body=body):
                self.assertEqual(hp.command_refusal(path, body)[0], 403)
        for path, body in (("/api/session/yolo", {"session_id": "s", "enabled": False}),
                           ("/api/approval/respond", {"session_id": "s", "choice": "once"}),
                           ("/api/approval/respond", {"session_id": "s", "choice": "once", "yolo": "yes"})):
            with self.subTest(body=body):
                self.assertIsNone(hp.command_refusal(path, body))

    def test_hidden_yolo_chip_blocks_yolo(self):
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}})
        policy = hp.current()[0]
        self.assertEqual(policy["deny_commands"], ["yolo"])
        self.assertFalse(hp.command_allowed(policy, "yolo"))
        self.assertTrue(hp.command_allowed(policy, "model"))
        self.assertEqual(hp.command_refusal("/api/session/yolo", {"session_id": "s"})[0], 403)
        # Even when listed: the hidden chip wins.
        self.write({"version": 1, "settings": {"hide_composer_yolo": True}, "allow_commands": ["yolo"]})
        self.assertFalse(hp.command_allowed(hp.current()[0], "yolo"))
        # The user's own choice to hide the chip blocks nothing.
        self.write({"version": 1, "settings": {"hide_composer_yolo": False}})
        self.assertTrue(hp.command_allowed(hp.current()[0], "yolo"))

    def test_moa_on_chat_start(self):
        self.write({"version": 1, "allow_commands": ["new"]})
        self.assertEqual(hp.command_refusal("/api/chat/start", {"message": "x", "moa_config": True})[0], 403)
        self.assertIsNone(hp.command_refusal("/api/chat/start", {"message": "/moa x"}))
        self.assertIsNone(hp.command_refusal("/api/chat/start", {"message": "x", "moa_config": None}))

    def test_without_a_policy(self):
        self.assertEqual(hp.command_refusal("/api/session/yolo", {"session_id": "s", "enabled": False})[0], 503)

    def test_client_view(self):
        self.write({"version": 1, "allow_commands": ["skills", "model"]})
        view = hp.client_view()
        self.assertEqual((view["allow_commands"], view["deny_commands"]), (["model", "skills"], []))


if __name__ == "__main__":
    unittest.main()
