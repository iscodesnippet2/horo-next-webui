"""Admin policy: settings and tabs an administrator locks, feature APIs it blocks.

Installed as api/horo_policy.py by horo_overlay.py (section 8). The policy is a
JSON file the person running the WebUI cannot write:

    {"version": 1,
     "settings": {"hide_composer_yolo": true},
     "hide_tabs": ["kanban", "settings"],
     "block_features": ["kanban", "terminal"]}

It is read from $HORO_WEBUI_POLICY, else /etc/horo/webui-policy.json. A policy
is mandatory: with none, or an unreadable, invalid or user-writable one, every
request outside the page shell, /health, /login and /api/auth/* is refused with
503. Nothing falls back to a policy read earlier: a shared folder that went
away must not leave the WebUI running on what it used to say.

This is interface governance, not a security boundary: the WebUI runs as the
user, who can run another copy without it (docs/admin-policy-design.md, §2).

Nothing here imports the WebUI at module level; api/config.py calls
configure() with its settings schema when it wraps load_settings/save_settings.
"""
from __future__ import annotations

import copy
import json
import math
import os
import posixpath
import stat
import sys
import threading
from pathlib import Path

ENV = "HORO_WEBUI_POLICY"
DEFAULT_PATH = "/etc/horo/webui-policy.json"
MAX_BYTES = 1024 * 1024

TOP_KEYS = {"version", "settings", "hide_tabs", "block_features"}
# Tabs the policy may hide. chat and skills never: the chat is the product, and
# the skills tab is the only way to see what the agent can do.
HIDEABLE_TABS = {"mindmap", "memory", "tasks", "kanban", "workspaces", "profiles", "insights", "logs",
                 "todos", "settings"}
NEVER_HIDDEN = {"chat", "skills"}
# Settings the policy may not force: tab lists are what hide_tabs is for, and
# the password is the user's.
UNFORCEABLE = {"hidden_tabs", "tab_order", "password_hash", "_set_password", "_clear_password"}
# Feature → (path prefixes, tab). Only features the chat, sessions, workspaces
# and the mind map do not use. A new upstream feature is not covered until it
# is added here.
FEATURES = {
    "kanban": (("/api/kanban",), "kanban"),
    "tasks": (("/api/crons",), "tasks"),
    "memory": (("/api/memory",), "memory"),
    "insights": (("/api/insights",), "insights"),
    "logs": (("/api/logs",), "logs"),
    "terminal": (("/api/terminal",), None),
    "mcp": (("/api/mcp",), None),
    "extensions": (("/api/extensions", "/api/plugins", "/api/dashboard",
                    "/extensions", "/plugins", "/dashboard-plugins"), None),
}
# Served while the policy is unusable: the page shell and its assets, so the
# page can say why nothing works, and what login needs.
OPEN_EXACT = {"/", "/index.html", "/sessions", "/health", "/login", "/favicon.ico", "/sw.js",
              "/manifest.json", "/manifest.webmanifest"}
OPEN_PREFIXES = ("/static", "/session", "/api/auth")

_schema: dict | None = None
_lock = threading.Lock()
_cache: dict = {"key": None, "policy": None, "error": None}
_reported: list = [None]


class PolicyUnavailable(RuntimeError):
    """No usable policy; the request or save must not go ahead."""


def configure(*, allowed, defaults, bools=(), enums=None, ints=None, floats=None) -> None:
    """The WebUI's settings schema, from api/config.py; forced values are checked against it."""
    global _schema
    _schema = {"allowed": set(allowed), "defaults": dict(defaults), "bools": set(bools),
               "enums": dict(enums or {}), "ints": dict(ints or {}), "floats": dict(floats or {})}
    with _lock:
        _cache.update(key=None, policy=None, error=None)


def policy_path() -> str:
    """Resolved on every call: the environment decides, never a cached answer."""
    return os.environ.get(ENV, "").strip() or DEFAULT_PATH


def _permission_problem(path: Path, st: os.stat_result) -> str | None:
    """Why the running user could change this policy, or None.

    Root ownership is not required: on a network share root is often squashed.
    What matters is that the user neither owns the file nor can write it or the
    folder it is in (or they could edit it, or replace it with their own).
    """
    if st.st_uid == os.geteuid():
        return f"the policy file is owned by the user running the WebUI (uid {st.st_uid})"
    if os.access(path, os.W_OK):
        return "the user running the WebUI can write the policy file"
    if os.access(path.parent, os.W_OK):
        return f"the user running the WebUI can write the folder {path.parent}"
    return None


def _check_value(key: str, value) -> str | None:
    s = _schema
    if key in s["bools"]:
        return None if isinstance(value, bool) else "must be true or false"
    if key in s["enums"]:
        return None if value in s["enums"][key] else f"must be one of {sorted(s['enums'][key])}"
    if key in s["ints"]:
        lo, hi = s["ints"][key]
        ok = isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi
        return None if ok else f"must be an integer from {lo} to {hi}"
    if key in s["floats"]:
        lo, hi = s["floats"][key]
        ok = (isinstance(value, (int, float)) and not isinstance(value, bool)
              and math.isfinite(value) and lo <= value <= hi)
        return None if ok else f"must be a number from {lo} to {hi}"
    default = s["defaults"].get(key)
    if default is None:
        ok = value is None or isinstance(value, (str, int, float)) and not isinstance(value, bool)
        return None if ok else "must be a string, a number or null"
    want = type(default)
    if want is bool:
        return None if isinstance(value, bool) else "must be true or false"
    if isinstance(value, bool):
        return f"must be a {want.__name__}"
    if want is float and isinstance(value, int):
        return None
    return None if isinstance(value, want) else f"must be a {want.__name__}"


def validate(data) -> dict:
    """The policy as {"settings", "hide_tabs", "block_features"}; ValueError says what is wrong."""
    if _schema is None:
        raise ValueError("the WebUI settings schema is not configured")
    if not isinstance(data, dict):
        raise ValueError("the policy must be a JSON object")
    unknown = sorted(set(data) - TOP_KEYS)
    if unknown:
        raise ValueError(f"unknown key(s): {', '.join(unknown)}")
    version = data.get("version")
    if version != 1 or isinstance(version, bool):
        raise ValueError(f"version must be 1, got {version!r}")

    settings = data.get("settings", {})
    if not isinstance(settings, dict):
        raise ValueError("settings must be an object")
    for key, value in settings.items():
        if key in UNFORCEABLE:
            raise ValueError(f"settings.{key} cannot be set by policy")
        if key not in _schema["allowed"]:
            raise ValueError(f"settings.{key} is not a WebUI setting")
        problem = _check_value(key, value)
        if problem:
            raise ValueError(f"settings.{key} {problem}")

    def names(field: str, allowed: set, never: set = frozenset()) -> list:
        value = data.get(field, [])
        if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
            raise ValueError(f"{field} must be a list of names")
        out = []
        for name in value:
            if name in never:
                raise ValueError(f"{field}: {name} can never be hidden")
            if name not in allowed:
                raise ValueError(f"{field}: unknown name {name!r}")
            if name not in out:
                out.append(name)
        return out

    hide_tabs = names("hide_tabs", HIDEABLE_TABS, NEVER_HIDDEN)
    block = names("block_features", set(FEATURES))
    for feature in block:
        tab = FEATURES[feature][1]
        if tab and tab not in hide_tabs:
            hide_tabs.append(tab)
    return {"settings": copy.deepcopy(settings), "hide_tabs": hide_tabs, "block_features": block}


def _report(path: str, error: str | None) -> None:
    if _reported[0] == (path, error):
        return
    _reported[0] = (path, error)
    if error:
        print(f"[horo-policy] REFUSING: policy {path}: {error}", file=sys.stderr, flush=True)
    else:
        print(f"[horo-policy] applying policy {path}", file=sys.stderr, flush=True)


def current() -> tuple[dict | None, str | None, str]:
    """(policy, None, path) when usable, else (None, reason, path). Rechecked on every call.

    The file is parsed again only when it changed; ownership and write access
    are checked every time, since a chmod does not change the mtime.
    """
    path = policy_path()
    try:
        real = Path(path).resolve(strict=True)
        st = os.stat(real)
        if not stat.S_ISREG(st.st_mode):
            raise ValueError("not a regular file")
        problem = _permission_problem(real, st)
        if problem:
            raise ValueError(problem)
        key = (str(real), st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns)
        with _lock:
            if _cache["key"] == key:
                policy, error = _cache["policy"], _cache["error"]
            else:
                policy, error = None, None
                try:
                    if st.st_size > MAX_BYTES:
                        raise ValueError(f"larger than {MAX_BYTES} bytes")
                    with open(real, "rb") as f:
                        raw = f.read(MAX_BYTES + 1)
                    policy = validate(json.loads(raw.decode("utf-8")))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    error = f"cannot be read as JSON: {exc}"
                except ValueError as exc:
                    error = str(exc)
                _cache.update(key=key, policy=policy, error=error)
    except FileNotFoundError:
        policy, error = None, "no policy file (a policy is required; see HORO_WEBUI_POLICY)"
    except OSError as exc:
        policy, error = None, f"cannot be read: {exc.strerror or exc}"
    except ValueError as exc:
        policy, error = None, str(exc)
    _report(path, error)
    return policy, error, path


def _apply(settings: dict, policy: dict) -> dict:
    settings.update(copy.deepcopy(policy["settings"]))
    hidden = [t for t in settings.get("hidden_tabs") or [] if isinstance(t, str)]
    settings["hidden_tabs"] = hidden + [t for t in policy["hide_tabs"] if t not in hidden]
    return settings


def wrap_load(upstream_load):
    """load_settings with the policy's values over the user's.

    Without a usable policy the user's values are returned unchanged: the
    password hash must still be readable for /login. Every request that could
    act on a setting is refused with 503 by refusal() in that state.
    """
    def load_settings() -> dict:
        settings = upstream_load()
        policy, _error, _path = current()
        return _apply(settings, policy) if policy else settings
    load_settings.__doc__ = upstream_load.__doc__
    return load_settings


def wrap_save(upstream_save, upstream_load):
    """save_settings that never writes a policy value into the user's settings.json.

    Locked keys are dropped from what is saved, and tabs only the policy hides
    are dropped from hidden_tabs; a tab the user had hidden themselves stays.
    """
    def save_settings(settings: dict) -> dict:
        policy, error, path = current()
        body = dict(settings)
        if not policy:
            # Which keys a broken policy locks is unknown: only the password,
            # which no policy can lock, may be saved (a legacy hash upgrades on login).
            if set(body) - {"_set_password", "_clear_password"}:
                raise PolicyUnavailable(f"admin policy {path}: {error}")
            return upstream_save(body)
        for key in policy["settings"]:
            body.pop(key, None)
        if isinstance(body.get("hidden_tabs"), list):
            own = set(upstream_load().get("hidden_tabs") or [])
            body["hidden_tabs"] = [t for t in body["hidden_tabs"]
                                   if t not in policy["hide_tabs"] or t in own]
        return _apply(upstream_save(body), policy)
    save_settings.__doc__ = upstream_save.__doc__
    return save_settings


def client_view() -> dict:
    """What the page needs to show locks: the path and the locked names, never the values."""
    policy, error, path = current()
    if not policy:
        return {"path": path, "error": error}
    return {"path": path, "settings": sorted(policy["settings"]), "hide_tabs": list(policy["hide_tabs"]),
            "block_features": list(policy["block_features"])}


def _under(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix + "/")


def _forms(path: str) -> set:
    """The path as routed and as normalised: a block must hold for either spelling."""
    folded = path.casefold()
    normal = posixpath.normpath("/" + folded.lstrip("/"))
    return {folded, normal}


def refusal(path: str):
    """(status, body) when the request must not reach a handler, else None.

    Called by server.py after authentication, before dispatch, for every method.
    """
    forms = _forms(path)
    policy, error, policy_file = current()
    if not policy:
        if all(p in OPEN_EXACT or any(_under(p, pre) for pre in OPEN_PREFIXES) for p in forms):
            return None
        return 503, {"error": f"The WebUI is unavailable: the admin policy {policy_file} cannot be used "
                              f"({error}). Contact your administrator.",
                     "horo_policy_error": {"path": policy_file, "reason": error}}
    for feature in policy["block_features"]:
        if any(_under(p, pre) for p in forms for pre in FEATURES[feature][0]):
            return 403, {"error": f"Blocked by the admin policy ({feature}).", "horo_policy_blocked": feature}
    return None
