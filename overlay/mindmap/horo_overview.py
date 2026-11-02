"""Read model for the agent mind map: every profile's human sessions, in one call.

Opening a session through the chat view reloads its transcript, may import it
into a sidecar, and switches the global profile. The map cannot afford any of
that per click, so it reads this instead: read-only SQL on each profile's
state.db, plus the in-process state only this server knows (live streams,
pending approvals). Nothing here writes.

A profile that cannot be read is reported in ``errors``, never silently dropped:
an empty column must not look like an idle profile.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path
from urllib.parse import quote

from api.helpers import _redact_text
from api.models import all_sessions
from api.profiles import get_hermes_home_for_profile, list_profiles_api

# Sessions a person is part of. An allowlist: a source Hermes adds later stays
# off the map until someone decides it belongs here.
HUMAN_SOURCES = ("webui", "cli", "tui", "desktop", "gui", "local")
WINDOW_SECONDS = 72 * 3600
MAX_PER_PROFILE = 200
TEXT_LIMIT = 280
EVENTS = 3
# A session outside this server has no live-stream record. Hermes stamps
# last_activity_at as it works, so a recent stamp on an open session means running.
EXTERNAL_RUNNING_SECONDS = 120
INTERRUPTED = {"startup_orphan_reap", "ws_orphan_reap"}

_SESSIONS_SQL = """
SELECT id, source, title, parent_session_id, end_reason, ended_at,
       COALESCE(last_activity_at, ended_at, started_at) AS activity,
       last_activity_description
FROM sessions
WHERE COALESCE(last_activity_at, ended_at, started_at) >= ?
  AND source IN ({sources})
  AND message_count > 0
  AND COALESCE(end_reason, '') != 'compression'
ORDER BY activity DESC
LIMIT ?
""".format(sources=",".join("?" * len(HUMAN_SOURCES)))
_FIRST_USER_SQL = """
SELECT content FROM messages
WHERE session_id = ? AND role = 'user' AND content IS NOT NULL AND content != ''
ORDER BY id LIMIT 1
"""
_RECENT_SQL = """
SELECT role, content, timestamp FROM messages
WHERE session_id = ? AND role IN ('user', 'assistant') AND content IS NOT NULL AND content != ''
ORDER BY id DESC LIMIT ?
"""
_PARENT_SQL = "SELECT end_reason FROM sessions WHERE id = ?"


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
    return "done"


def _next_step(status: str, attention: dict | None, end_reason: str | None) -> str:
    if status == "waiting":
        kind = (attention or {}).get("kind")
        return "等待你批准指令" if kind == "approval" else "等待你回答問題"
    if status == "failed":
        return f"工作中斷（{end_reason}）"
    return "繼續執行" if status == "running" else "—"


def _db_uri(home: Path) -> str:
    return f"file:{quote(str(home / 'state.db'))}?mode=ro"


def _read_profile(name: str, home: Path, cutoff: float, now: float, live: dict, attention_of) -> tuple[list, list]:
    if not (home / "state.db").exists():
        return [], []
    sessions, links = [], []
    conn = sqlite3.connect(_db_uri(home), uri=True, timeout=1.0)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(_SESSIONS_SQL, (cutoff, *HUMAN_SOURCES, MAX_PER_PROFILE)).fetchall()
        for row in rows:
            sid = row["id"]
            state = live.get(sid, {})
            if state.get("archived"):
                continue
            first = conn.execute(_FIRST_USER_SQL, (sid,)).fetchone()
            recent = conn.execute(_RECENT_SQL, (sid, EVENTS)).fetchall()
            attention = attention_of(sid)
            status = _status(row, now, state, attention)
            goal = _text(first["content"]) if first else ""
            last_assistant = next((m["content"] for m in recent if m["role"] == "assistant"), "")
            sessions.append({
                "id": sid,
                "profile": name,
                "source": row["source"],
                "title": _text(state.get("title") or row["title"], 120) or goal[:80] or sid,
                "status": status,
                "last_activity_at": row["activity"],
                "goal": goal or "—",
                "progress": _text(row["last_activity_description"]) or _text(last_assistant) or "—",
                "next": _next_step(status, attention, row["end_reason"]),
                "events": [{"role": m["role"], "text": _text(m["content"], 160), "at": m["timestamp"]}
                           for m in reversed(recent)],
                "pinned": bool(state.get("pinned")),
                "read_only": row["source"] != "webui",
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
            state[sid] = {key: row.get(key) for key in ("is_streaming", "pinned", "archived", "title")}
    return state


def build(attention_of, since: str = "") -> dict:
    """Return the overview, or just ``{"unchanged": true}`` if ``since`` still matches."""
    now = time.time()
    cutoff = now - WINDOW_SECONDS
    live = _live_state()
    profiles, sessions, links, errors = [], [], [], []
    for entry in list_profiles_api():
        name = entry.get("name") or "default"
        profiles.append(name)
        try:
            found, forks = _read_profile(name, get_hermes_home_for_profile(name), cutoff, now, live, attention_of)
        except sqlite3.Error as exc:
            errors.append({"profile": name, "error": str(exc)})
            continue
        sessions += found
        links += forks
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
