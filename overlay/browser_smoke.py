"""Drive the installed wheel's front end in Chromium and check it renders.

    python overlay/browser_smoke.py <wheel> <python-version>

verify_wheel.py proves every vendored file is served; it cannot prove the
libraries work. A self-hosted library can 404 nothing and still be broken:
PDF.js imported by a relative path from a blob: module does not resolve, and a
library moved to a new major can change behaviour. This runs upstream's own
render paths — renderMermaidBlocks, renderKatexBlocks, loadPdfInline, Prism's
autoloader, xterm — in a real browser, and fails on any request that leaves
the machine.

Needs Playwright (`uv run --with playwright`) and its Chromium
(`playwright install --with-deps chromium`).
"""
from __future__ import annotations

import os
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright

WHEEL, PYV = Path(sys.argv[1]).resolve(), sys.argv[2]

# A one-page PDF with a line of text. pdf.js recovers the xref table itself.
PDF = b"""%PDF-1.4
1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj
2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj
3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 100]/Contents 4 0 R/Resources<</Font<</F1 5 0 R>>>>>>endobj
4 0 obj<</Length 44>>stream
BT /F1 18 Tf 20 40 Td (horo smoke) Tj ET
endstream endobj
5 0 obj<</Type/Font/Subtype/Type1/BaseFont/Helvetica>>endobj
trailer<</Root 1 0 R>>
%%EOF
"""

PROBE = r"""async () => {
  const c = document.createElement('div');
  c.innerHTML = `
    <div class="mermaid-block">graph TD; A["start"] --> B["$$x^2$$"]</div>
    <div class="katex-block" data-katex="display">\\frac{a}{b}+\\sqrt{x^2}</div>
    <div class="pdf-preview-load" data-path="horo-smoke.pdf"></div>
    <pre><code class="language-python">def f(x):\n    return x</code></pre>`;
  document.body.appendChild(c);
  renderMermaidBlocks(c);
  renderKatexBlocks(c);
  loadPdfInline(c);
  Prism.highlightElement(c.querySelector('code'));
  const until = async (fn, ms = 30000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { try { if (fn()) return true; } catch (e) {} await new Promise(r => setTimeout(r, 200)); }
    return false;
  };
  const out = {};
  out.mermaid = await until(() => c.querySelector('.mermaid-block svg'));
  out.mermaidMath = !!c.querySelector('.mermaid-block svg .katex, .mermaid-block svg math');
  out.katex = await until(() => c.querySelector('.katex-block .katex-display .katex-html'));
  out.pdf = await until(() => { const cv = c.querySelector('canvas.pdf-preview-canvas'); return cv && cv.width > 0; });
  out.prism = await until(() => c.querySelectorAll('code .token').length > 2);
  out.xterm = typeof Terminal === 'function' && !!new Terminal();
  out.mermaidVersion = typeof mermaid !== 'undefined' && mermaid.version ? String(mermaid.version) : '';
  out.pdfjsVersion = window._pdfjsLib ? window._pdfjsLib.version : '';
  out.katexVersion = typeof katex !== 'undefined' ? katex.version : '';
  // Left in the body, it squeezes the page's layout for the checks after it.
  c.remove();
  return out;
}"""

# The mind map reads state.db directly. One session a person is in, one that is not,
# both still open: finished sessions start collapsed.
MINDMAP_DB = """
CREATE TABLE sessions (id TEXT PRIMARY KEY, source TEXT, title TEXT, parent_session_id TEXT, end_reason TEXT,
  ended_at REAL, started_at REAL, last_activity_at REAL, last_activity_description TEXT, message_count INTEGER);
CREATE TABLE messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
"""


def seed_mindmap(hermes_home: Path) -> None:
    now = time.time()
    # A second profile, so a tab can be left believing it is on the wrong one. Without
    # the agent installed upstream lists only default, so the map shows none of it.
    for home, rows in ((hermes_home, (("smoke-human", "desktop"), ("smoke-cron", "cron"))),
                       (hermes_home / "profiles" / "smoke-second", (("smoke-second", "desktop"),))):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").touch()
        with sqlite3.connect(home / "state.db") as db:
            db.executescript(MINDMAP_DB)
            for sid, source in rows:
                db.execute("INSERT INTO sessions VALUES (?,?,?,NULL,NULL,NULL,?,?,NULL,1)", (sid, source, sid, now, now))
                db.execute("INSERT INTO messages (session_id, role, content, timestamp) VALUES (?,'user','smoke',?)",
                           (sid, now))


failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    venv, home = tmp / "venv", tmp / "home"
    home.mkdir()
    seed_mindmap(home / ".hermes")
    subprocess.run(["uv", "venv", "-q", "--python", PYV, str(venv)], check=True)
    subprocess.run(["uv", "pip", "install", "-q", "--python", str(venv / "bin" / "python"), str(WHEEL)], check=True)
    py = venv / "bin" / "python"
    site = Path(subprocess.check_output([str(py), "-c", "import api,pathlib;print(pathlib.Path(api.__file__).parent.parent)"],
                                        text=True).strip())
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]
    env = {**os.environ, "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
           "HERMES_WEBUI_HOST": "127.0.0.1", "HERMES_WEBUI_PORT": str(port),
           "HERMES_WEBUI_STATE_DIR": str(home / "webui")}
    log = tmp / "server.log"
    proc = subprocess.Popen([str(py), str(site / "server.py")], cwd=tmp, env=env,
                            stdout=log.open("w"), stderr=subprocess.STDOUT, start_new_session=True)
    base = f"http://127.0.0.1:{port}"
    try:
        for _ in range(120):
            try:
                if urllib.request.urlopen(base + "/health", timeout=2).status == 200:
                    break
            except OSError:
                time.sleep(0.5)
        print(f"[smoke] Python {PYV}, Chromium")
        external, errors = [], []
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            page = browser.new_page()

            def on_request(req):
                host = urlsplit(req.url).hostname
                if req.url.startswith(("http:", "https:")) and host not in ("127.0.0.1", "localhost"):
                    external.append(req.url)
            page.on("request", on_request)
            page.on("pageerror", lambda e: errors.append(str(e)))
            # The PDF comes from the workspace through /api/media; serve a fixture.
            page.route("**/api/media?path=horo-smoke.pdf*",
                       lambda route: route.fulfill(status=200, body=PDF, content_type="application/pdf"))
            # Requests leaving the machine are recorded above and then refused,
            # as they would be air-gapped.
            page.route(lambda url: urlsplit(url).hostname not in ("127.0.0.1", "localhost"),
                       lambda route: route.abort())
            page.goto(base + "/", wait_until="load")
            page.wait_for_function("typeof renderMermaidBlocks === 'function' && typeof Prism !== 'undefined'",
                                   timeout=30000)
            # A page load lands on the mind map; the render probe needs the chat view.
            try:
                page.wait_for_function("() => S._bootReady && _currentPanel === 'mindmap'", timeout=15000)
                landed = True
            except Exception:
                landed = False
            page.evaluate("switchPanel('chat')")
            out = page.evaluate(PROBE)
            page.evaluate("switchPanel('mindmap')")
            page.wait_for_selector(".mm-card", timeout=15000)
            map_requests: list[str] = []
            on_map_request = lambda req: map_requests.append(req.url)
            page.on("request", on_map_request)
            # Dispatched, not clicked: a fresh home sits behind the onboarding dialog.
            page.evaluate("document.querySelector('.mm-card').click()")
            page.wait_for_selector(".mm-detail", timeout=5000)
            mindmap = page.evaluate("""() => ({
              cards: [...document.querySelectorAll('.mm-card')].map(c => c.dataset.sid),
              chat: getComputedStyle(document.getElementById('mainChat')).display,
              open: document.querySelector('.mm-progress-link').title,
              nav: [...document.querySelectorAll('.rail .nav-tab[data-panel]')].slice(0, 2).map(b => b.dataset.panel),
            })""")
            page.remove_listener("request", on_map_request)
            refused = page.evaluate("""() => fetch('api/horo/overview', {headers: {'X-Horo-Profile': 'nobody'}})
              .then(r => r.status)""")
            page.evaluate("""() => fetch('api/horo/goal', {method: 'POST', headers: {'Content-Type': 'application/json'},
              body: JSON.stringify({session_id: mm.data.sessions[0].id, goal: '手寫目標'})})""")
            page.evaluate("loadMindmap()")
            page.wait_for_function("() => mm.data.sessions[0].goal === '手寫目標'", timeout=5000)
            fresh = page.evaluate("mm.data.sessions[0].imported")
            page.evaluate("_mmOpen(mm.data.sessions[0])")
            page.wait_for_function("() => document.body.classList.contains('mm-modal')"
                                   " && !document.body.classList.contains('mm-loading')", timeout=15000)
            modal = page.evaluate("""() => ({
              // Gaps to the map column's inside edges: left, right, bottom.
              full: (m => { const b = m.getBoundingClientRect(), c = document.getElementById('mainChat').getBoundingClientRect();
                return [c.left - b.left - m.clientLeft, b.left + m.clientLeft + m.clientWidth - c.right,
                        b.top + m.clientTop + m.clientHeight - c.bottom].map(Math.round); })(
                document.querySelector('main.main')),
              rail: !document.querySelector('.rail').inert && document.querySelector('.rail').getBoundingClientRect().width > 0,
              session: S.session.session_id === mm.modal, behind: document.getElementById('mainMindmap').inert,
              panel: _currentPanel, close: document.getElementById('mmModalClose').getAttribute('aria-label'),
              centred: ((h, b) => Math.abs(h.left + h.width / 2 - (b.left + b.width / 2)) < 2)(
                document.querySelector('.mm-modal-heading').getBoundingClientRect(),
                document.getElementById('mmModalBar').getBoundingClientRect()),
            })""")
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.body.classList.contains('mm-modal')", timeout=5000)
            kept = page.evaluate("mm.selected === mm.data.sessions[0].id")
            page.keyboard.press("Escape")
            page.wait_for_function("() => document.getElementById('mmDetail').hidden", timeout=5000)
            # The centre sits at a fixed place in the view (MM_ME_AT), at both ends of the scroll.
            level = page.evaluate("""async () => {
              const view = document.getElementById('mmMap');
              const at = async top => {
                view.scrollTop = top;
                await new Promise(r => requestAnimationFrame(() => requestAnimationFrame(r)));
                const me = document.getElementById('mmMe').getBoundingClientRect();
                const place = view.getBoundingClientRect().top + view.clientTop + view.clientHeight * MM_ME_AT;
                return Math.round(me.top + me.height / 2 - place);
              };
              const out = [await at(0), await at(1e6)];
              view.scrollTop = 0;
              return out;
            }""")
            # The centre's halves: left lengthens, right shortens, both stop at the ends;
            # the choice is stored and read back on the next load.
            steps = page.evaluate("""() => {
              const longer = document.getElementById('mmMeLonger'), shorter = document.getElementById('mmMeShorter');
              const out = [];
              longer.click(); out.push(mm.hours, longer.disabled);
              longer.click(); out.push(mm.hours);
              shorter.click(); shorter.click(); out.push(mm.hours);
              const stored = JSON.parse(localStorage.getItem(MM_STORE_KEY)).hours;
              mm.hours = 6; _mmLoadStore(); out.push(stored, mm.hours);
              ['mmMeShorter', 'mmMeShorter', 'mmMeShorter', 'mmMeShorter'].forEach(id => document.getElementById(id).click());
              out.push(mm.hours, shorter.disabled);
              document.querySelector('[data-mm-hours="72"]').click();
              return out;
            }""")
            # A profile whose only session is older than the range is left off the map.
            pruned = page.evaluate("""() => {
              const s = mm.data.sessions.find(s => s.id === 'smoke-human');
              const pills = () => document.querySelectorAll('.mm-profile-pill').length;
              // Selected cards always show; closing a workspace reselects its card late.
              _mmDeselect();
              const {last_activity_at: at, status} = s;
              const out = [pills()];
              // Idle too: a running session stays on the map however old.
              Object.assign(s, {last_activity_at: Date.now() / 1000 - 30 * 86400, status: 'idle'});
              _mmRender();
              // The centre stays, alone, so a wider range is one click away.
              out.push(pills(), !document.getElementById('mmEmpty').hidden,
                       document.getElementById('mmMe').getBoundingClientRect().width > 0,
                       document.getElementById('mmEdges').children.length);
              Object.assign(s, {last_activity_at: at, status});
              _mmRender();
              out.push(pills());
              return out;
            }""")
            page.evaluate("document.querySelector('.mm-card')"
                          ".dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
            page.wait_for_function("() => document.body.classList.contains('mm-modal')"
                                   " && !document.body.classList.contains('mm-loading')", timeout=15000)
            card_open = page.evaluate("S.session.session_id === document.querySelector('.mm-card').dataset.sid")
            page.keyboard.press("Escape")
            page.wait_for_function("() => !document.body.classList.contains('mm-modal')", timeout=5000)
            opened = "() => document.body.classList.contains('mm-modal') && !document.body.classList.contains('mm-loading')"
            new_chat = []
            for _ in range(2):
                page.evaluate("document.querySelector('.mm-profile-pill')"
                              ".dispatchEvent(new MouseEvent('dblclick', {bubbles: true}))")
                page.wait_for_function(opened, timeout=15000)
                new_chat.append(page.evaluate("""() => ({
                  id: S.session.session_id, modal: mm.modal === S.session.session_id,
                  profile: S.session.profile || 'default', empty: !S.messages.length,
                })"""))
                page.keyboard.press("Escape")
                page.wait_for_function("() => !document.body.classList.contains('mm-modal')", timeout=5000)
            # Every tab shares the profile cookie. Another tab switching it leaves this
            # one believing it is still on default, where upstream skips the switch.
            stale = page.evaluate("""async () => {
              const otherTab = name => api('/api/profile/switch', {method: 'POST', body: JSON.stringify({name})});
              const server = async () => (await api('/api/profile/active')).name;
              const out = {believed: S.activeProfile};
              await otherTab('smoke-second');
              out.moved = await server();
              await _mmOpen(mm.data.sessions.find(s => s.id === 'smoke-human'));
              out.open = mm.modal === 'smoke-human' && S.session.session_id === 'smoke-human';
              out.server = await server();
              // And while the workspace is open: coming back to this tab takes the profile back.
              await otherTab('smoke-second');
              window.dispatchEvent(new Event('focus'));
              for (let i = 0; i < 25 && await server() !== 'default'; i++) await new Promise(r => setTimeout(r, 200));
              out.held = await server();
              _mmCloseModal();
              return out;
            }""")
            # The card has been opened, so the WebUI holds a copy: the next open shows it
            # first and imports behind it, in the window chosen on the map.
            later = page.evaluate("""async () => {
              await loadMindmap();
              const s = mm.data.sessions.find(s => s.id === 'smoke-human');
              const tail = document.getElementById('mmTail');
              tail.value = '10';
              tail.dispatchEvent(new Event('change'));
              // Away first: reopening the session already loaded requests nothing.
              await loadSession(mm.drafts.default);
              const seen = [];
              const real = api;
              api = (path, opts) => {
                seen.push(`${path.startsWith('/api/session/import_cli') ? 'import' : /messages=1/.test(path) ? 'load' : 'other'}`
                          + `${document.body.classList.contains('mm-loading') ? '' : '+shown'}`
                          + `${/msg_limit=10&/.test(path) ? '+10' : ''}`);
                return real(path, opts);
              };
              try {
                await _mmOpen(s);
                for (let i = 0; i < 25 && !seen.some(x => x.startsWith('import')); i++) await new Promise(r => setTimeout(r, 200));
              } finally {
                api = real;
              }
              _mmCloseModal();
              return {imported: s.imported, seen: seen.filter(x => !x.startsWith('other')),
                      stored: localStorage.getItem('horo-initial-messages')};
            }""")
            browser.close()
        print(f"  libraries: mermaid {out['mermaidVersion'] or '?'}, pdf.js {out['pdfjsVersion'] or '?'}, "
              f"katex {out['katexVersion'] or '?'}")
        check(out["mermaid"], "Mermaid renders a diagram")
        check(out["mermaidMath"], "Mermaid renders math through its bundled KaTeX")
        check(out["katex"], "KaTeX renders display math")
        check(out["pdf"], "PDF.js renders a page to canvas")
        check(out["prism"], "Prism highlights Python via its autoloader")
        check(out["xterm"], "xterm constructs a terminal")
        check(mindmap["cards"] == ["smoke-human"], "mind map shows only the session a person is in", str(mindmap["cards"]))
        check(landed, "a page load opens on the mind map")
        check(mindmap["chat"] == "none", "mind map replaces the chat view")
        check(not map_requests, "selecting a card makes no request", ", ".join(map_requests[:3]))
        check(mindmap["nav"] == ["mindmap", "chat"], "mind map comes first in the navigation", str(mindmap["nav"]))
        check(mindmap["open"] == "開啟工作區", "the progress line opens the workspace", mindmap["open"])
        check(refused == 403, "an unknown profile in X-Horo-Profile is refused", str(refused))
        check(modal == {"full": [0, 0, 0], "rail": True, "session": True, "behind": True, "panel": "mindmap",
                       "close": "關閉工作區", "centred": True},
              "workspace covers the map, leaves the rail usable, title centred, with a close button", str(modal))
        check(kept, "Esc closes the workspace and keeps the card selected")
        check(all(abs(off) <= 2 for off in level), "the centre stays at its place in the view",
              f"offsets {level} px")
        check(steps == [168, True, 168, 48, 48, 48, 6, True], "the centre's halves step the range, stop at the ends, and it is kept", str(steps))
        check(pruned == [1, 0, True, True, 0, 1],
              "a profile with nothing in the time range is left off; the centre stays", str(pruned))
        check(card_open, "double-clicking a card opens its workspace")
        first = new_chat[0]
        check(first["id"] != "smoke-human" and first["modal"] and first["profile"] == "default" and first["empty"],
              "double-clicking a profile opens a new session in its workspace", str(first))
        check(new_chat[1]["id"] == first["id"], "an unused new session is reused, not duplicated", str(new_chat))
        check(stale == {"believed": "default", "moved": "smoke-second", "open": True, "server": "default", "held": "default"},
              "a card opens in its own profile after another tab switched profile, and keeps it", str(stale))
        check(fresh is False, "a session the WebUI never opened is imported before it opens", str(fresh))
        check(later == {"imported": True, "seen": ["load+10", "import+shown"], "stored": "10"},
              "an imported session opens before its import, in the window set on the map", str(later))
        check(not external, "no request leaves the machine", ", ".join(sorted(set(external))[:4]))
        check(not errors, "no uncaught page errors", " | ".join(errors[:3]))
    finally:
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGTERM)
            proc.wait(timeout=10)
    if failures:
        print(log.read_text()[-1500:])

if failures:
    sys.exit(f"[smoke] {len(failures)} check(s) failed")
print("[smoke] all checks passed")
