"""Install a built horo-next-webui wheel into a clean venv and check it works.

    python overlay/verify_wheel.py <wheel> <python-version> <expected-version>

Every check guards a failure a successful build and upload would not reveal:
front-end libraries still fetched from a CDN (blank diagrams, no highlighting
offline), a vendored path that 404s from the wheel, .mjs served with a type
the browser refuses to import, the version badge reading "unknown", and the
launcher piping a GitHub installer into bash or creating a venv inside
site-packages when Hermes Agent is absent. And the admin policy: without a
usable one every API answers 503, with one its values are forced and kept out
of settings.json, and blocked features answer 403.

Runs outside the source tree with a throwaway HERMES_HOME and a PATH without
`hermes`, so nothing resolves to a checkout or touches a real ~/.hermes.
"""
from __future__ import annotations

import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

from policy_fixture import isolated_env, launch_command, write_policy

WHEEL, PYV, EXPECTED = Path(sys.argv[1]).resolve(), sys.argv[2], sys.argv[3]
DIST = "horo-next-webui"
TOP_LEVEL = {"api", "static", "bootstrap", "server"}
# Top-level names horo-next (Hermes Agent) installs; both go in one venv.
AGENT_TOP_LEVEL = {"agent", "tools", "hermes_cli", "gateway", "tui_gateway", "cron", "acp_adapter",
                   "plugins", "providers", "hermes_platform", "run_agent", "cli", "model_tools",
                   "toolsets", "utils", "hermes_constants", "hermes_bootstrap", "mcp_serve"}

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    venv, home, cwd = tmp / "venv", tmp / "home", tmp / "elsewhere"
    home.mkdir(); cwd.mkdir()
    bindir = venv / "bin"
    # HOME too: upstream also looks for an agent checkout under ~/.hermes and ~/.
    env = {**isolated_env(os.environ, home), "VIRTUAL_ENV": str(venv), "NO_COLOR": "1",
           "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"}

    def run(*cmd, **kw):
        return subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True, timeout=600, **kw)

    print(f"[verify] Python {PYV}")
    r = subprocess.run(["uv", "venv", "--python", PYV, str(venv)], capture_output=True, text=True)
    if r.returncode:
        sys.exit(f"[verify] cannot create a {PYV} venv:\n{r.stderr[-800:]}")
    r = subprocess.run(["uv", "pip", "install", "--python", str(bindir / "python"), str(WHEEL)],
                       capture_output=True, text=True)
    check(r.returncode == 0, "wheel installs", r.stderr[-400:] if r.returncode else "")
    if r.returncode:
        sys.exit(1)
    py = bindir / "python"

    probe = r'''
import json, importlib.metadata as md
from pathlib import Path
out = {}
dist = md.distribution("horo-next-webui")
out["name"], out["version"] = dist.metadata["Name"], dist.version
out["requires"] = dist.requires or []
files = [str(f) for f in (dist.files or [])]
out["license"] = any("licenses/LICENSE" in f for f in files)
out["top"] = sorted({f.split("/")[0].removesuffix(".py") for f in files
                     if not f.split("/")[0].endswith((".dist-info", ".data")) and not f.startswith("..")})
import api.updates as u
out["badge"] = u._detect_webui_version()
import api
out["site"] = str(Path(api.__file__).resolve().parent.parent)
print("JSON" + json.dumps(out))
'''
    r = run(str(py), "-c", probe)
    data = next((json.loads(ln[4:]) for ln in r.stdout.splitlines() if ln.startswith("JSON")), {})
    check(bool(data), "package imports", r.stderr[-600:] if not data else "")
    if not data:
        sys.exit(f"[verify] Python {PYV}: cannot import the package")
    site = Path(data["site"])
    check(data["name"] == DIST, "distribution name", data["name"])
    check(data["version"] == EXPECTED, "distribution version", data["version"])
    check(data["badge"] == f"v{EXPECTED}", "settings badge version", data["badge"])
    check(data["license"], "MIT license shipped in dist-info")
    check(TOP_LEVEL <= set(data["top"]), "server, launcher and assets ship", str(data["top"]))
    check(not set(data["top"]) & AGENT_TOP_LEVEL, "no top-level module collides with horo-next",
          str(sorted(set(data["top"]) & AGENT_TOP_LEVEL)))
    crypto = next((q for q in data["requires"] if q.lower().startswith("cryptography")), "")
    floor = re.search(r">=\s*(\d+)", crypto)
    check(bool(floor) and int(floor[1]) >= 50, "cryptography floor raised", crypto)

    # ── Launcher without Hermes Agent: refuse, never install ─────────────
    r = run(str(bindir / "hermes-webui"), "--no-browser")
    out = r.stdout + r.stderr
    check(r.returncode != 0 and "pip install horo-next" in out,
          "`hermes-webui` without the agent points to pip", out.strip().splitlines()[-1] if out.strip() else "")
    check("curl" not in out and "install.sh" not in out, "launcher does not run a network installer")
    check(not (site / ".venv").exists(), "launcher creates no venv inside site-packages")

    # ── Launcher: --host/--port, and a busy port refused with advice ─────
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0)); busy.listen()
        taken = busy.getsockname()[1]
        for host in ("127.0.0.1", "0.0.0.0"):
            r = run(str(bindir / "hermes-webui"), "--host", host, "--port", str(taken), "--no-browser")
            out = r.stdout + r.stderr
            check(r.returncode != 0 and f"Port {taken} on {host} is already in use" in out
                  and "hermes-webui --port " in out and "Traceback" not in out,
                  f"a busy port on --host {host} is refused with a port to use instead",
                  out.strip().splitlines()[-1] if out.strip() else "")
    r = run(str(bindir / "hermes-webui"), "--port", "70000")
    check(r.returncode != 0 and "port must be 1-65535" in r.stderr, "an out-of-range --port is refused",
          r.stderr.strip().splitlines()[-1] if r.stderr.strip() else "")

    # ── Server: serves the UI and every front-end library from the wheel ─
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    # The admin policy every check below runs under (policy_fixture.py).
    policy_file = tmp / "admin" / "webui-policy.json"
    policy_file.parent.mkdir()
    POLICY = {"version": 1, "settings": {"hide_composer_yolo": True}, "hide_tabs": ["settings"],
              "block_features": ["kanban", "logs"]}
    write_policy(policy_file, POLICY)
    senv = {**env, "HERMES_WEBUI_HOST": "127.0.0.1", "HERMES_WEBUI_PORT": str(port),
            "HORO_WEBUI_POLICY": str(policy_file)}
    log = tmp / "server.log"
    proc = subprocess.Popen(launch_command(py, site, policy_file), cwd=cwd, env=senv,
                            stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"

    def request(path: str, body=None, at: str = "") -> tuple[int, str, bytes]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request((at or base) + path, data=data,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), e.read()
        except OSError:
            return 0, "", b""

    def get(path: str) -> tuple[int, str, bytes]:
        return request(path)

    def as_json(raw: bytes) -> dict:
        try:
            return json.loads(raw or b"{}")
        except ValueError:
            return {}

    try:
        for _ in range(120):
            if proc.poll() is not None:
                break
            if get("/health")[0] == 200:
                break
            time.sleep(0.5)
        status, _, page = get("/")
        check(status == 200 and b"<title>" in page, "server serves the UI",
              f"HTTP {status}" if status != 200 else "")
        with urllib.request.urlopen(base + "/", timeout=10) as resp:
            csp = resp.headers.get("Content-Security-Policy", "")
        check(csp and not re.search(r"jsdelivr|cloudflare|googleapis|gstatic", csp),
              "CSP allows no CDN, analytics or font host", csp[:120])
        texts = {"/": page.decode("utf-8", "replace")}
        for js in ("/static/ui.js", "/static/boot.js", "/static/terminal.js", "/static/sw.js"):
            texts[js] = get(js)[2].decode("utf-8", "replace")
        cdn = sorted({m for t in texts.values()
                      for m in re.findall(r"https://cdn\.jsdelivr\.net/npm/[^\s'\"`)]+", t)})
        check(not cdn, "no front-end library loaded from a CDN", str(cdn[:3]))
        refs = sorted({m for t in texts.values()
                       for m in re.findall(r"static/vendor/[\w@./-]+\.(?:m?js|css)", t)})
        bad = []
        for ref in refs:
            status, ctype, body = get("/" + ref.split("?")[0])
            want = "text/css" if ref.endswith(".css") else "javascript"
            if status != 200 or want not in ctype or not body:
                bad.append(f"{ref} → {status} {ctype}")
        check(refs and not bad, f"all {len(refs)} vendored assets served with the right type", "; ".join(bad[:4]))
        prism = re.search(r"static/vendor/prismjs/[\d.]+/", " ".join(refs))
        if prism:
            status, ctype, _ = get(f"/{prism[0]}components/prism-python.min.js")
            check(status == 200 and "javascript" in ctype, "Prism autoloader finds language components",
                  f"{status} {ctype}")
        else:
            check(False, "Prism is vendored")
        settings = json.loads(get("/api/settings")[2] or b"{}")
        check(settings.get("check_for_updates") is False, "update check is off by default",
              str(settings.get("check_for_updates")))
        mermaid = next((r for r in refs if "/mermaid/" in r), "")
        body = get("/" + mermaid)[2] if mermaid else b""
        dp = re.search(rb"@license DOMPurify (\d+\.\d+\.\d+)", body)
        check(bool(dp), "Mermaid bundle embeds DOMPurify", dp[1].decode() if dp else "not found")

        # ── Admin policy: forced, kept out of settings.json, blocking ────
        view = settings.get("horo_policy", {})
        check(settings.get("hide_composer_yolo") is True and {"settings", "kanban", "logs"} <= set(settings.get("hidden_tabs", [])),
              "/api/settings returns the policy's values and tabs",
              f"{settings.get('hide_composer_yolo')} {settings.get('hidden_tabs')}")
        check(view == {"path": str(policy_file), "settings": ["hide_composer_yolo"],
                       "hide_tabs": ["settings", "kanban", "logs"], "block_features": ["kanban", "logs"]},
              "/api/settings lists what the policy locks", str(view))
        status, _, raw = request("/api/settings", {"hide_composer_yolo": False, "hidden_tabs": ["kanban", "memory"],
                                                   "send_key": "ctrl+enter"})
        saved = as_json(raw)
        on_disk = as_json((home / "webui" / "settings.json").read_bytes()) if (home / "webui" / "settings.json").exists() else {}
        check(status == 200 and saved.get("hide_composer_yolo") is True and saved.get("send_key") == "ctrl+enter",
              "a locked setting cannot be changed; others can", f"HTTP {status} {str(saved)[:160]}")
        check(on_disk.get("send_key") == "ctrl+enter" and on_disk.get("hide_composer_yolo") is not True
              and on_disk.get("hidden_tabs") == ["memory"] and "horo_policy" not in on_disk,
              "no policy value is written to settings.json",
              str({k: on_disk.get(k) for k in ("hide_composer_yolo", "hidden_tabs", "horo_policy")}))
        blocked = {p: request(p)[0] for p in ("/api/kanban/board", "/api/logs", "/API/Logs")}
        check(set(blocked.values()) == {403}, "a blocked feature's API answers 403", str(blocked))
        status, _, raw = request("/api/session/new", {})
        sid = (as_json(raw).get("session") or {}).get("session_id", "")
        free = {"/api/sessions": request("/api/sessions")[0], "/api/session/new": status,
                "/api/session": request(f"/api/session?session_id={sid}")[0],
                "/api/memoryx": request("/api/memoryx")[0],
                "/api/chat/start": request("/api/chat/start", {"session_id": sid, "message": "hi"})[0]}
        check(free["/api/sessions"] == 200 and free["/api/session/new"] == 200 and free["/api/session"] == 200
              and not {403, 503} & set(free.values()),
              "sessions, chat and look-alike paths are not blocked", str(free))
        write_policy(policy_file, "{ broken")
        status, _, raw = request("/api/settings")
        err = as_json(raw).get("horo_policy_error", {})
        check(status == 503 and err.get("path") == str(policy_file) and "JSON" in err.get("reason", ""),
              "a broken policy answers 503 with its path and reason", f"HTTP {status} {err}")
        check(request("/api/sessions")[0] == 503 and request("/health")[0] == 200 and request("/")[0] == 200,
              "without a usable policy: APIs 503, /health and the page still served")
        write_policy(policy_file, POLICY)
        check(request("/api/settings")[0] == 200, "a fixed policy applies without a restart")
        policy_file.unlink()
        check(request("/api/settings")[0] == 503, "a removed policy is not remembered: 503")
        write_policy(policy_file, POLICY)
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    if failures:
        print(log.read_text()[-1500:])

    # ── Admin policy, no exception: as an installed server starts ────────
    # The user's own file is refused; so is no file at all.
    own = tmp / "own" / "webui-policy.json"
    own.parent.mkdir()
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port2 = s.getsockname()[1]
    plain = subprocess.Popen([str(py), str(site / "server.py")], cwd=cwd,
                             env={**senv, "HERMES_WEBUI_PORT": str(port2), "HORO_WEBUI_POLICY": str(own)},
                             stdout=(tmp / "plain.log").open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    base2 = f"http://127.0.0.1:{port2}"
    try:
        for _ in range(120):
            if plain.poll() is not None or request("/health", at=base2)[0] == 200:
                break
            time.sleep(0.5)
        status, _, raw = request("/api/settings", at=base2)
        reason = as_json(raw).get("horo_policy_error", {}).get("reason", "")
        check(status == 503 and "no policy file" in reason and request("/health", at=base2)[0] == 200,
              "no policy: 503 with the reason, /health still answers", f"HTTP {status} {reason}")
        write_policy(own, {"version": 1})
        status, _, raw = request("/api/settings", at=base2)
        reason = as_json(raw).get("horo_policy_error", {}).get("reason", "")
        check(status == 503 and "owned by the user running the WebUI" in reason,
              "a policy the user owns is refused", f"HTTP {status} {reason}")
        status, _, raw = request("/api/auth/status", at=base2)
        check(status not in (0, 403, 503), "login stays reachable without a policy", f"HTTP {status}")
    finally:
        if plain.poll() is None:
            os.killpg(plain.pid, signal.SIGTERM)
            try:
                plain.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(plain.pid, signal.SIGKILL)

if failures:
    sys.exit(f"[verify] Python {PYV}: {len(failures)} check(s) failed")
print(f"[verify] Python {PYV}: all checks passed")
