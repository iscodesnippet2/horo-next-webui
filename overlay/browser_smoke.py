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
  return out;
}"""

failures: list[str] = []


def check(cond: bool, label: str, detail: str = "") -> None:
    print(f"  {'ok  ' if cond else 'FAIL'} {label}{(' — ' + detail) if detail else ''}")
    if not cond:
        failures.append(label)


with tempfile.TemporaryDirectory() as tmp:
    tmp = Path(tmp)
    venv, home = tmp / "venv", tmp / "home"
    home.mkdir()
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
            out = page.evaluate(PROBE)
            browser.close()
        print(f"  libraries: mermaid {out['mermaidVersion'] or '?'}, pdf.js {out['pdfjsVersion'] or '?'}, "
              f"katex {out['katexVersion'] or '?'}")
        check(out["mermaid"], "Mermaid renders a diagram")
        check(out["mermaidMath"], "Mermaid renders math through its bundled KaTeX")
        check(out["katex"], "KaTeX renders display math")
        check(out["pdf"], "PDF.js renders a page to canvas")
        check(out["prism"], "Prism highlights Python via its autoloader")
        check(out["xterm"], "xterm constructs a terminal")
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
