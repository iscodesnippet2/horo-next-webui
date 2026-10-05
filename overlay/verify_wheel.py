"""Install a built horo-next-webui wheel into a clean venv and check it works.

    python overlay/verify_wheel.py <wheel> <python-version> <expected-version>

Every check guards a failure a successful build and upload would not reveal:
front-end libraries still fetched from a CDN (blank diagrams, no highlighting
offline), a vendored path that 404s from the wheel, .mjs served with a type
the browser refuses to import, the version badge reading "unknown", and the
launcher piping a GitHub installer into bash or creating a venv inside
site-packages when Hermes Agent is absent.

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
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes"), "VIRTUAL_ENV": str(venv), "NO_COLOR": "1",
           "PATH": f"{bindir}{os.pathsep}/usr/bin{os.pathsep}/bin"}
    for k in ("PYTHONPATH", "HERMES_WEBUI_AGENT_DIR", "HERMES_WEBUI_PYTHON"):
        env.pop(k, None)

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
    check(">=50" in crypto, "cryptography floor raised", crypto)

    # ── Launcher without Hermes Agent: refuse, never install ─────────────
    r = run(str(bindir / "hermes-webui"), "--no-browser")
    out = r.stdout + r.stderr
    check(r.returncode != 0 and "pip install horo-next" in out,
          "`hermes-webui` without the agent points to pip", out.strip().splitlines()[-1] if out.strip() else "")
    check("curl" not in out and "install.sh" not in out, "launcher does not run a network installer")
    check(not (site / ".venv").exists(), "launcher creates no venv inside site-packages")

    # ── Server: serves the UI and every front-end library from the wheel ─
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    senv = {**env, "HERMES_WEBUI_HOST": "127.0.0.1", "HERMES_WEBUI_PORT": str(port),
            "HERMES_WEBUI_STATE_DIR": str(home / "webui")}
    log = tmp / "server.log"
    proc = subprocess.Popen([str(py), str(site / "server.py")], cwd=cwd, env=senv,
                            stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"

    def get(path: str) -> tuple[int, str, bytes]:
        try:
            with urllib.request.urlopen(base + path, timeout=10) as resp:
                return resp.status, resp.headers.get("Content-Type", ""), resp.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Content-Type", ""), b""
        except OSError:
            return 0, "", b""

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
        mermaid = next((r for r in refs if "/mermaid/" in r), "")
        body = get("/" + mermaid)[2] if mermaid else b""
        dp = re.search(rb"@license DOMPurify (\d+\.\d+\.\d+)", body)
        check(bool(dp), "Mermaid bundle embeds DOMPurify", dp[1].decode() if dp else "not found")
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(proc.pid, signal.SIGKILL)
    if failures:
        print(log.read_text()[-1500:])

if failures:
    sys.exit(f"[verify] Python {PYV}: {len(failures)} check(s) failed")
print(f"[verify] Python {PYV}: all checks passed")
