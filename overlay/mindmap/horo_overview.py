"""Read model for the agent mind map: every profile's human sessions, in one call.

Opening a session through the chat view reloads its transcript, may import it
into a sidecar, and switches the global profile. The map cannot afford any of
that per click, so it reads this instead: read-only SQL on each profile's
state.db, plus the in-process state only this server knows (live streams,
pending approvals). Reading writes nothing; goals a person edits live in this
server's own state directory, so state.db stays Hermes's alone.

The map acts on sessions of other profiles without switching the active one:
``X-Horo-Profile`` scopes a single request, see ``profile_override``.

A profile that cannot be read is reported in ``errors``, never silently dropped:
an empty column must not look like an idle profile.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import sqlite3
import threading
import time
from pathlib import Path
from urllib.parse import quote

from api.auth import _trusted_auth_bound_profile
from api.config import STATE_DIR
from api.helpers import _redact_text
from api.models import all_sessions
from api.profiles import (
    _is_isolated_profile_mode,
    get_active_profile_name,
    get_hermes_home_for_profile,
    list_profiles_api,
    set_request_profile,
)

logger = logging.getLogger(__name__)

# Sessions a person is part of. An allowlist: a source Hermes adds later stays
# off the map until someone decides it belongs here.
HUMAN_SOURCES = ("webui", "cli", "tui", "desktop", "gui", "local")
WINDOW_SECONDS = 7 * 24 * 3600
MAX_PER_PROFILE = 200
TEXT_LIMIT = 280
GOAL_LIMIT = 120
MAX_GOALS = 5000
GOALS_FILE = "horo_goals.json"
USER_MESSAGES_SCANNED = 40
USER_MESSAGE_PREFIX = 4000
EVENTS = 3
# A session outside this server has no live-stream record. Hermes stamps
# last_activity_at as it works, so a recent stamp on an open session means running.
EXTERNAL_RUNNING_SECONDS = 120
INTERRUPTED = {"startup_orphan_reap", "ws_orphan_reap"}
PROFILE_HEADER = "X-Horo-Profile"
CLI_INDEX_SECONDS = 5.0
# Messages stored as role 'user' that no person typed. A format Hermes adds later
# only shows up as progress text until it is listed here.
_MACHINE_MESSAGE = re.compile(
    r"\s*(\[(context compaction|system:|important:|note:|out-of-band|continuing toward)"
    r"|you've reached the maximum|↻ continuing toward)", re.I)
_ATTACHED_CONTEXT = "--- Attached Context ---"
_FILE_REFERENCE = re.compile(r"@(file|folder|url|diff|staged):\S+")
_ACKNOWLEDGEMENTS = {"要", "好", "好的", "對", "是", "嗯", "可以", "繼續", "同意", "謝謝", "收到", "不用", "不要",
                     "ok", "okay", "yes", "y", "no", "go", "go on", "continue", "thanks", "hi", "hello"}
LIVE_FIELDS = ("is_streaming", "pinned", "archived", "title", "active_stream_id", "profile", "updated_at",
               "message_count", "parent_session_id", "raw_source", "is_cli_session")

_SESSIONS_SQL = """
SELECT id, source, title, parent_session_id, end_reason, ended_at,
       COALESCE(last_activity_at, ended_at, started_at) AS activity
FROM sessions
WHERE (COALESCE(last_activity_at, ended_at, started_at) >= ? OR id IN ({{pinned}}))
  AND source IN ({sources})
  AND message_count > 0
  AND COALESCE(end_reason, '') != 'compression'
ORDER BY activity DESC
LIMIT ?
""".format(sources=",".join("?" * len(HUMAN_SOURCES)))
_USER_MESSAGES_SQL = """
SELECT substr(content, 1, {prefix}) FROM messages
WHERE session_id = ? AND role = 'user' AND content IS NOT NULL AND content != ''
ORDER BY id DESC LIMIT {limit}
""".format(prefix=USER_MESSAGE_PREFIX, limit=USER_MESSAGES_SCANNED)
_RECENT_SQL = """
SELECT role, content, timestamp FROM messages
WHERE session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != ''
ORDER BY id DESC LIMIT ?
"""
_PARENT_SQL = "SELECT end_reason FROM sessions WHERE id = ?"


_goals: dict | None = None
_goals_lock = threading.Lock()
_cli_index: dict = {}
_cli_refreshing: set = set()
_cli_index_lock = threading.Lock()


def _build_cli_index(key: tuple, fetch_rows) -> dict:
    index = {row.get("session_id"): row for row in fetch_rows(all_profiles=key[1])}
    with _cli_index_lock:
        _cli_index[key] = (time.monotonic(), index)
    return index


def _refresh_cli_index(key: tuple, fetch_rows) -> None:
    set_request_profile(key[0])
    try:
        _build_cli_index(key, fetch_rows)
    finally:
        with _cli_index_lock:
            _cli_refreshing.discard(key)


def cli_row(fetch_rows, session_id: str, all_profiles: bool) -> dict | None:
    """One session's row from upstream's list, without rebuilding the list per lookup.

    Upstream scans every session of the profile to find one, and opening a
    session looks it up three times in a row: up to a second in all on 3,900
    sessions. So only the first lookup of a profile waits; later ones answer from
    the index and refresh it behind the scenes once it is CLI_INDEX_SECONDS old.
    A session too new to be listed still reaches upstream's direct lookup, which
    follows this call.
    """
    key = (get_active_profile_name(), all_profiles)
    with _cli_index_lock:
        at, index = _cli_index.get(key, (0.0, None))
        refresh = (index is not None and time.monotonic() - at > CLI_INDEX_SECONDS
                   and key not in _cli_refreshing)
        if refresh:
            _cli_refreshing.add(key)
    if index is None:
        index = _build_cli_index(key, fetch_rows)
    elif refresh:
        threading.Thread(target=_refresh_cli_index, args=(key, fetch_rows), daemon=True).start()
    row = index.get(session_id)
    return dict(row) if row else None


def _text(raw: object, limit: int = TEXT_LIMIT) -> str:
    """One readable, redacted line from a message body."""
    value = raw if isinstance(raw, str) else ""
    if value.lstrip().startswith(("[{", '{"')):
        try:
            parts = json.loads(value)
        except ValueError:
            parts = None
        if isinstance(parts, list):
            value = " ".join(p.get("text", "") for p in parts if isinstance(p, dict))
    line = " ".join(_redact_text(value).split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def _prompt_text(raw: object) -> str:
    """What a person typed in this message, or "" if nobody did."""
    if not isinstance(raw, str) or _MACHINE_MESSAGE.match(raw):
        return ""
    return _text(_FILE_REFERENCE.sub("", raw.split(_ATTACHED_CONTEXT)[0]))


def _substantive(prompt: str) -> bool:
    return prompt.lower().strip(" .,!?。，！？～~") not in _ACKNOWLEDGEMENTS


def _progress(rows: list) -> str:
    """The latest prompt with something to say; a bare "要" says nothing about the work."""
    prompts = [prompt for prompt in (_prompt_text(row[0]) for row in rows) if prompt]
    return next((prompt for prompt in prompts if _substantive(prompt)), prompts[0] if prompts else "—")


def _goals_path() -> Path:
    return Path(STATE_DIR) / GOALS_FILE


def _load_goals() -> dict:
    global _goals
    if _goals is None:
        path = _goals_path()
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            _goals = loaded if isinstance(loaded, dict) else {}
        except FileNotFoundError:
            _goals = {}
        except (OSError, ValueError) as exc:
            # People wrote these: keep the file for recovery rather than overwrite it.
            logger.warning("Setting aside unreadable %s: %s", GOALS_FILE, exc)
            os.replace(path, path.with_suffix(".bad"))
            _goals = {}
    return _goals


def _save_goals(goals: dict) -> None:
    path = _goals_path()
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(goals, handle, ensure_ascii=False)
    os.replace(temporary, path)


def _goal_of(name: str, sid: str) -> str:
    with _goals_lock:
        return _load_goals().get(f"{name}/{sid}", "")


def set_goal(body: dict) -> str | None:
    """Store a person's goal for a session of the request's profile; return why not, if refused.

    An empty goal goes back to the session's own title. Kept here for every
    source alike: upstream's rename refuses read-only sessions, most of the map.
    """
    sid, goal = body.get("session_id"), body.get("goal")
    if not isinstance(sid, str) or not sid or not isinstance(goal, str):
        return "session_id and goal are required"
    key = f"{get_active_profile_name()}/{sid}"
    text = _text(goal, GOAL_LIMIT)
    with _goals_lock:
        goals = _load_goals()
        if text and key not in goals and len(goals) >= MAX_GOALS:
            return "Too many goals"
        if text:
            goals[key] = text
        else:
            goals.pop(key, None)
        _save_goals(goals)
    return None


def _status(row: sqlite3.Row, now: float, live: dict, attention: dict | None) -> str:
    if attention:
        return "waiting"
    if live.get("is_streaming"):
        return "running"
    if row["end_reason"] in INTERRUPTED:
        return "failed"
    external = row["source"] != "webui"
    if external and row["ended_at"] is None and now - row["activity"] < EXTERNAL_RUNNING_SECONDS:
        return "running"
    return "idle"


def _blocker(status: str, attention: dict | None, end_reason: str | None) -> str:
    if status == "waiting":
        kind = (attention or {}).get("kind")
        return "等待你批准指令" if kind == "approval" else "等待你回答問題"
    return f"工作中斷（{end_reason}）" if status == "failed" else ""


def _db_uri(home: Path) -> str:
    return f"file:{quote(str(home / 'state.db'))}?mode=ro"


def _read_profile(name: str, home: Path, cutoff: float, now: float, live: dict, attention_of) -> tuple[list, list]:
    if not (home / "state.db").exists():
        return [], []
    sessions, links = [], []
    conn = sqlite3.connect(_db_uri(home), uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    try:
        # Pinned sessions stay on the map however old they are.
        pinned = [sid for sid, state in live.items() if state.get("pinned")]
        sql = _SESSIONS_SQL.format(pinned=",".join("?" * len(pinned)))
        rows = conn.execute(sql, (cutoff, *pinned, *HUMAN_SOURCES, MAX_PER_PROFILE)).fetchall()
        for row in rows:
            sid = row["id"]
            state = live.get(sid, {})
            if state.get("archived"):
                continue
            progress = _progress(conn.execute(_USER_MESSAGES_SQL, (sid,)).fetchall())
            recent = conn.execute(_RECENT_SQL, (sid, EVENTS)).fetchall()
            attention = attention_of(sid)
            status = _status(row, now, state, attention)
            title = _text(state.get("title") or row["title"], GOAL_LIMIT) or progress[:80] or sid
            sessions.append({
                "id": sid,
                "profile": name,
                "source": row["source"],
                "title": title,
                "status": status,
                "last_activity_at": row["activity"],
                "goal": _goal_of(name, sid) or title,
                "progress": progress,
                "blocker": _blocker(status, attention, row["end_reason"]),
                "events": [{"role": m["role"], "text": _text(m["content"], 160), "at": m["timestamp"]}
                           for m in reversed(recent)],
                "pinned": bool(state.get("pinned")),
                "read_only": row["source"] != "webui",
                # The WebUI holds a copy: opening needs no import first.
                "imported": sid in live,
                "stream_id": state.get("active_stream_id") or None,
            })
            parent = row["parent_session_id"]
            if parent:
                up = conn.execute(_PARENT_SQL, (parent,)).fetchone()
                # A compression child is the same conversation continued, not a fork.
                if up is not None and up["end_reason"] != "compression":
                    links.append({"from": parent, "to": sid, "type": "fork"})
    finally:
        conn.close()
    return sessions, links


def _live_state() -> dict:
    """What only this process knows, by session id."""
    state = {}
    for row in all_sessions(include_lineage_metadata=False, sidebar_metadata_only=True):
        sid = row.get("session_id")
        if sid:
            state[sid] = {key: row.get(key) for key in LIVE_FIELDS}
    return state


def _sidecar_session(sid: str, state: dict, now: float, attention_of) -> dict:
    """A WebUI session with no state.db row yet: a fresh fork, before its first turn."""
    attention = attention_of(sid)
    status = "waiting" if attention else "running" if state.get("is_streaming") else "idle"
    title = _text(state.get("title"), GOAL_LIMIT) or sid
    name = state.get("profile") or "default"
    return {
        "id": sid, "profile": name, "source": "webui", "title": title,
        "status": status, "last_activity_at": state.get("updated_at") or now, "goal": _goal_of(name, sid) or title,
        "progress": "—", "blocker": _blocker(status, attention, None), "events": [],
        "pinned": bool(state.get("pinned")),
        "read_only": False, "imported": True, "stream_id": state.get("active_stream_id") or None,
    }


def _sidecar_only(known: set, profiles: list, cutoff: float, now: float, live: dict, attention_of) -> tuple[list, list]:
    sessions, links = [], []
    for sid, state in live.items():
        if sid in known or state.get("archived") or state.get("is_cli_session") or not state.get("message_count"):
            continue
        if (state.get("profile") or "default") not in profiles:
            continue
        if (state.get("raw_source") or "webui") != "webui":
            continue
        if (state.get("updated_at") or 0) < cutoff and not state.get("pinned"):
            continue
        sessions.append(_sidecar_session(sid, state, now, attention_of))
        if state.get("parent_session_id"):
            links.append({"from": state["parent_session_id"], "to": sid, "type": "fork"})
    return sessions, links


def profile_override(handler) -> str | None:
    """Scope this request to the ``X-Horo-Profile`` profile; return why not, if refused.

    Runs in the route handlers, so after authentication. It grants nothing a
    client cannot already do with /api/profile/switch, and refuses what that
    route refuses: a profile bound to the login, or isolated single-profile mode.
    """
    name = handler.headers.get(PROFILE_HEADER, "").strip()
    if name == get_active_profile_name():
        return None
    if _is_isolated_profile_mode():
        return "Profiles are isolated on this server"
    bound = _trusted_auth_bound_profile(handler)
    if bound and name != bound:
        return "Profile is bound to the current session"
    if name not in {entry.get("name") for entry in list_profiles_api()}:
        return "Unknown profile"
    set_request_profile(name)
    return None


def readable_profiles(handler, names: list) -> list:
    """The profiles this request may read.

    A login bound to a profile, or a server isolated to one, sees only that one:
    the map shows prompts, so listing every profile would hand them to anyone
    signed in. A bound profile that does not exist leaves nothing.
    """
    bound = _trusted_auth_bound_profile(handler)
    if bound:
        return [name for name in names if name == bound]
    if _is_isolated_profile_mode():
        active = get_active_profile_name()
        return [name for name in names if name == active]
    return names


def build(attention_of, since: str = "", *, handler) -> dict:
    """Return the overview, or just ``{"unchanged": true}`` if ``since`` still matches."""
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    live = _live_state()
    profiles, sessions, links, errors = [], [], [], []
    names = [entry.get("name") or "default" for entry in list_profiles_api()]
    for name in readable_profiles(handler, names):
        profiles.append(name)
        try:
            found, forks = _read_profile(name, get_hermes_home_for_profile(name), cutoff, now, live, attention_of)
        except sqlite3.Error as exc:
            errors.append({"profile": name, "error": str(exc)})
            continue
        sessions += found
        links += forks
    extra, extra_links = _sidecar_only({s["id"] for s in sessions}, profiles, cutoff, now, live, attention_of)
    sessions += extra
    links += extra_links
    visible = {s["id"] for s in sessions}
    payload = {
        "window_hours": WINDOW_SECONDS // 3600,
        "profiles": profiles,
        "sessions": sessions,
        "links": [link for link in links if link["from"] in visible],
        "errors": errors,
    }
    version = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()[:16]
    if since and since == version:
        return {"unchanged": True, "version": version}
    return {**payload, "version": version}
