"""Turn an upstream Hermes WebUI release checkout into the horo-next-webui package.

    python overlay/horo_overlay.py <upstream-checkout> <expected-version>

horo-next-webui is an unofficial PyPI distribution of Hermes WebUI, the browser
front end for Hermes Agent (packaged as horo-next). Upstream ships packaging
metadata but publishes no maintained wheel, and its launcher assumes a source
checkout: from site-packages it would pipe a GitHub installer into bash and
create a virtualenv inside site-packages. This overlay changes packaging and
launch only, with one deliberate exception: the agent mind map (section 7).

Two kinds of step, deliberately treated differently (as in horo-next):

* Metadata steps are checked by POSTCONDITION on the resulting pyproject.toml.

* Code edits use EXACT anchors and fail if the anchor moved. Silently skipping
  one would ship a wheel that builds, uploads, and misbehaves.

Vulnerable front-end libraries are handled separately, by vendor_assets.py;
vulnerable Python floors by security_bump.py.
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import tomllib
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = Path(sys.argv[1]).resolve()
EXPECTED_VERSION = sys.argv[2]

DIST_NAME = "horo-next-webui"
REPO = "https://github.com/iscodesnippet2/horo-next-webui"
# Upstream CI and classifiers cover 3.11–3.13. 3.14 is not vouched for.
REQUIRES_PYTHON = ">=3.11,<3.14"
# The air-gapped machines run CPython 3.12.
REQUIRED_PYTHONS = ("3.12",)

failures: list[str] = []


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"[overlay] FAIL {msg}")


def ok(msg: str) -> None:
    print(f"[overlay] ok   {msg}")


def edit(path: str, old: str, new: str, count: int = 1) -> None:
    p = ROOT / path
    if not p.exists():
        return fail(f"{path}: file missing")
    text = p.read_text(encoding="utf-8")
    found = text.count(old)
    if found != count:
        return fail(f"{path}: expected {count} anchor(s), found {found}: {old.splitlines()[0][:70]!r}")
    p.write_text(text.replace(old, new), encoding="utf-8")
    ok(path)


def sub_once(path: str, pattern: str, repl: str, label: str) -> None:
    p = ROOT / path
    text = p.read_text(encoding="utf-8")
    new, n = re.subn(pattern, repl, text, count=1, flags=re.M | re.S)
    if n != 1:
        return fail(f"{path}: {label} anchor not found")
    p.write_text(new, encoding="utf-8")
    ok(f"{path}: {label}")


PP = ROOT / "pyproject.toml"

# ── 1. Distribution name and PyPI page ───────────────────────────────────
# hermes-webui on PyPI is someone else's upload (0.1.0, unmaintained). The
# command stays `hermes-webui` and state stays under ~/.hermes/webui: this IS
# Hermes WebUI, repackaged.
edit("pyproject.toml", 'name = "hermes-webui"', f'name = "{DIST_NAME}"')
sub_once("pyproject.toml", r'^description = ".*?"$',
         'description = "Unofficial PyPI distribution of Hermes WebUI"', "description")
shutil.copy(HERE.parent / "pypi" / "README.md", ROOT / "README-PYPI.md")
edit("pyproject.toml", 'readme = "README.md"', 'readme = "README-PYPI.md"')
sub_once("pyproject.toml", r'^requires-python = ".*?"$', f'requires-python = "{REQUIRES_PYTHON}"',
         "requires-python")

# Security review systems (Sonatype and others) follow Project-URL to the
# source, and read the maintainer to tell this repackaging from the original.
# PEP 639: an SPDX expression, with which a License classifier may not coexist.
edit("pyproject.toml", 'license = {text = "MIT"}', 'license = "MIT"\nlicense-files = ["LICENSE"]')
edit("pyproject.toml", 'authors = [{name = "Hermes WebUI contributors"}]',
     'authors = [{name = "Hermes WebUI contributors"}]\n'
     'maintainers = [{name = "horo-next-webui contributors"}]')
edit("pyproject.toml", '    "License :: OSI Approved :: MIT License",\n',
     '    "Operating System :: POSIX :: Linux",\n    "Operating System :: MacOS",\n')
edit("pyproject.toml",
     'Repository = "https://github.com/nesquena/hermes-webui"\n'
     'Issues = "https://github.com/nesquena/hermes-webui/issues"\n',
     f'Source = "{REPO}"\n'
     f'Issues = "{REPO}/issues"\n'
     f'Security = "{REPO}/blob/horo/SECURITY.md"\n'
     'Upstream = "https://github.com/nesquena/hermes-webui"\n')

# ── 2. Static version ────────────────────────────────────────────────────
# Upstream derives the version from git tags with setuptools-scm. Its tags are
# `vX.Y.Z` and `exp-vX.Y.Z`; the second does not parse, and a release here must
# be a pure function of (tag, overlay) anyway. The version is written statically
# and into api/_version.py, which the settings badge reads when there is no .git.
edit("pyproject.toml", 'dynamic = ["version", "dependencies"]',
     f'version = "{EXPECTED_VERSION}"\ndynamic = ["dependencies"]')
edit("pyproject.toml", 'requires = ["setuptools>=80", "setuptools-scm>=8"]', 'requires = ["setuptools>=80"]')
sub_once("pyproject.toml", r'^\[tool\.setuptools_scm\]\n[^\[]*', "", "drop [tool.setuptools_scm]")
(ROOT / "api" / "_version.py").write_text(f"__version__ = 'v{EXPECTED_VERSION}'\n", encoding="utf-8")
ok("api/_version.py")

# ── 3. Launcher: never install anything from the network ─────────────────
# Upstream's launcher, missing Hermes Agent, runs `curl <github>/install.sh |
# bash`. Installed from a wheel that is never right: the agent is the horo-next
# package, installed by the operator from the same index as this one.
edit("bootstrap.py",
     '''    info(f"Hermes Agent not found. Attempting install via {INSTALLER_URL}")
    subprocess.run(
        ["/bin/bash", "-lc", f"curl -fsSL {INSTALLER_URL} | bash"], check=True
    )''',
     '''    # horo-next-webui: never fetch and run an installer from the network.
    raise RuntimeError(
        "Hermes Agent was not found. Install it into this environment with "
        "`pip install horo-next`, or set HERMES_WEBUI_AGENT_DIR / HERMES_WEBUI_PYTHON."
    )''')

# The interpreter that installed this wheel is the one with its dependencies.
# Upstream prefers whatever `python3` is first on PATH, which from a venv
# install is often the system Python — the check then fails and falls through
# to the virtualenv below.
edit("bootstrap.py",
     '    return shutil.which("python3") or shutil.which("python") or sys.executable\n',
     '    # horo-next-webui: the interpreter this package is installed in.\n'
     '    return sys.executable\n')

# Upstream's last resort creates REPO_ROOT/.venv and pip-installs
# requirements.txt from the network. REPO_ROOT is site-packages here.
edit("bootstrap.py",
     '    venv_dir = REPO_ROOT / ".venv"\n',
     '''    # horo-next-webui: REPO_ROOT is site-packages; never create a venv or pip-install there.
    raise RuntimeError(
        f"{python_exe} cannot import both the WebUI dependencies and Hermes Agent. "
        "Install horo-next into the same environment (`pip install horo-next`), "
        "or set HERMES_WEBUI_PYTHON to an interpreter that has both."
    )
    venv_dir = REPO_ROOT / ".venv"
''')

# With horo-next installed alongside, the agent dir resolves to site-packages,
# and upstream runs the server there; its fallback relative writes would land
# in site-packages. The state dir is ours to write.
edit("bootstrap.py",
     'server_cwd = os.environ.get("HERMES_WEBUI_SERVER_CWD", "").strip() or str(agent_dir or REPO_ROOT)',
     'server_cwd = os.environ.get("HERMES_WEBUI_SERVER_CWD", "").strip() or str(state_dir)')

# ── 4. Every root module ships ───────────────────────────────────────────
# Upstream lists root modules by hand, and the list drifted: server.py imports
# managed_agent_startup, which is not in it, so a wheel built from upstream's
# config cannot start its own server. Derived from the tree instead.
root_modules = sorted(f.stem for f in ROOT.glob("*.py") if f.stem not in ("setup", "conftest"))
sub_once("pyproject.toml", r'^py-modules = \[.*?\]$',
         "py-modules = [" + ", ".join(f'"{m}"' for m in root_modules) + "]", "py-modules from tree")

# ── 5. .mjs is JavaScript ────────────────────────────────────────────────
# PDF.js is an ES module (.mjs), now served from the wheel instead of a CDN.
# Python's mimetypes table only knows .mjs on some platforms/versions; a
# browser refuses to import a module served with a non-JavaScript type.
edit("api/routes.py",
     '    "js": "application/javascript",\n    "html": "text/html",\n',
     '    "js": "application/javascript",\n    "mjs": "application/javascript",\n    "html": "text/html",\n')

# ── 6. No outbound traffic nobody asked for ──────────────────────────────
# The air-gapped target has no route out. Upstream checks GitHub for updates on
# every page load, and allows CDN, analytics and web-font hosts in its CSP. A
# wheel updates through pip, and nothing here loads from those hosts, so the
# check is off by default (the setting remains) and the hosts leave the policy.
# Other network features (OAuth, cloud TTS, the extension gallery) run only when
# a user invokes them, and are left as upstream's.
edit("api/config.py",
     '    "check_for_updates": True,  # check if webui/agent repos are behind upstream\n',
     '    "check_for_updates": False,  # horo-next-webui: wheels update through pip\n')
for old, new in (
    ("\"default-src 'self' https://*.cloudflareaccess.com; \"", "\"default-src 'self'; \""),
    ("\"script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://static.cloudflareinsights.com blob:; \"",
     "\"script-src 'self' 'unsafe-inline' blob:; \""),
    ("\"worker-src blob: 'self' https://cdn.jsdelivr.net; \"", "\"worker-src blob: 'self'; \""),
    ("\"style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com; \"",
     "\"style-src 'self' 'unsafe-inline'; \""),
    ("\"font-src 'self' data: https://fonts.gstatic.com; \"", "\"font-src 'self' data:; \""),
    ("\"manifest-src 'self' https://*.cloudflareaccess.com; \"", "\"manifest-src 'self'; \""),
    ('f"{_CSP_CONNECT_BASE} https://cdn.jsdelivr.net{extra_connect_src}"', 'f"{_CSP_CONNECT_BASE}{extra_connect_src}"'),
):
    edit("api/helpers.py", old, new)

# ── 7. Agent mind map ─────────────────────────────────
# The one product feature added here. Its code is new files; upstream's are
# touched only to hang them on: a route, a nav entry, a panel, two tags.
MINDMAP = HERE / "mindmap"
shutil.copy(MINDMAP / "horo_overview.py", ROOT / "api" / "horo_overview.py")
shutil.copy(MINDMAP / "mindmap.js", ROOT / "static" / "mindmap.js")
shutil.copy(MINDMAP / "mindmap.css", ROOT / "static" / "mindmap.css")
ok("mind map files")
edit("api/routes.py",
     '    if parsed.path == "/api/session":\n        return _handle_session_get(handler, parsed)\n',
     '    if parsed.path == "/api/horo/overview":\n'
     '        from api.horo_overview import build as _horo_overview\n'
     '        return j(handler, _horo_overview(_session_attention_summary,\n'
     '                                         parse_qs(parsed.query).get("since", [""])[0], handler=handler))\n\n'
     '    if parsed.path == "/api/session":\n        return _handle_session_get(handler, parsed)\n')
# A map action on another profile's session is scoped to that one request.
edit("api/routes.py", '    """Handle all GET routes. Returns True if handled, False for 404."""\n',
     '    """Handle all GET routes. Returns True if handled, False for 404."""\n'
     '    if handler.headers.get("X-Horo-Profile"):\n'
     '        from api.horo_overview import profile_override\n'
     '        refused = profile_override(handler)\n'
     '        if refused:\n'
     '            return bad(handler, refused, 403)\n')
edit("api/routes.py", '    """Handle all POST routes. Returns True if handled, False for 404."""\n',
     '    """Handle all POST routes. Returns True if handled, False for 404."""\n'
     '    if handler.headers.get("X-Horo-Profile"):\n'
     '        from api.horo_overview import profile_override\n'
     '        refused = profile_override(handler)\n'
     '        if refused:\n'
     '            arm_connection_close_if_body_pending(handler)\n'
     '            return bad(handler, refused, 403)\n')
edit("api/routes.py",
     '    if parsed.path == "/api/session/rename":\n        try:\n            require(body, "session_id", "title")\n',
     '    if parsed.path == "/api/horo/goal":\n'
     '        from api.horo_overview import set_goal\n'
     '        refused = set_goal(body)\n'
     '        return bad(handler, refused) if refused else j(handler, {"ok": True})\n'
     '    if parsed.path == "/api/session/rename":\n        try:\n            require(body, "session_id", "title")\n')
edit("api/routes.py",
     '        for row in get_cli_sessions(all_profiles=all_profiles):\n'
     '            if row.get("session_id") == session_id:\n'
     '                return row\n'
     '        for source in _CLI_LOOKUP_FALLBACK_SOURCES:\n',
     '        from api.horo_overview import cli_row\n'
     '        row = cli_row(get_cli_sessions, session_id, all_profiles)\n'
     '        if row:\n'
     '            return row\n'
     '        for source in _CLI_LOOKUP_FALLBACK_SOURCES:\n')
# Opening a session of another profile waited for the sidebar, which the map
# has just covered, to finish reloading: about 420 ms on real data.
edit("static/sessions.js",
     "    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);\n"
     "    if(typeof renderSessionList==='function') await renderSessionList();\n",
     "    if(typeof _setProfileSwitchListEmbargo==='function') _setProfileSwitchListEmbargo(false);\n"
     "    if(typeof renderSessionList==='function') void renderSessionList();\n")
# A page load opens on the mind map, over whatever session it restores, unless
# ?panel= names a panel or the mind map tab is hidden.
edit("static/boot.js",
     "    }catch(e){console.warn('[boot] panel intent launch failed', e);}\n  }\n"
     "  // Apply the honored intent over whatever the restore path below settled on.\n",
     "    }catch(e){console.warn('[boot] panel intent launch failed', e);}\n  }\n"
     "  if(!pendingPanelIntent&&!(panelIntent&&panelIntent.hasParam&&panelIntent.valid)\n"
     "     &&!(typeof _getHiddenTabs==='function'&&_getHiddenTabs().includes('mindmap'))) pendingPanelIntent='mindmap';\n"
     "  // Apply the honored intent over whatever the restore path below settled on.\n")
edit("static/panels.js", "'kanban','workspaces','profiles','insights','logs','plugin'];",
     "'kanban','mindmap','workspaces','profiles','insights','logs','plugin'];")
edit("static/panels.js", "  if (nextPanel === 'kanban') await loadKanban();\n",
     "  if (prevPanel === 'mindmap' && nextPanel !== 'mindmap') hideMindmap();\n"
     "  if (nextPanel === 'kanban') await loadKanban();\n"
     "  if (nextPanel === 'mindmap') await showMindmap();\n")
_MM_ICON = ('<svg width="{s}" height="{s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="{w}" '
            'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="3"/>'
            '<circle cx="4" cy="6" r="2"/><circle cx="20" cy="6" r="2"/><circle cx="4" cy="18" r="2"/>'
            '<circle cx="20" cy="18" r="2"/><path d="M9.5 10.5 5.7 7.1M14.5 10.5l3.8-3.4M9.5 13.5l-3.8 3.4M14.5 13.5l3.8 3.4"/></svg>')
_MM_ATTRS = ('data-panel="mindmap" onclick="switchPanel(\'mindmap\',{fromRailClick:true})" '
             'data-tooltip="心智圖" aria-label="心智圖"')
# First in the navigation, above Chat.
sub_once("static/index.html", r'(    <button class="rail-btn nav-tab active has-tooltip" data-panel="chat"[^\n]*\n)',
         lambda m: f'    <button class="rail-btn nav-tab has-tooltip" {_MM_ATTRS}>'
                   + _MM_ICON.format(s=20, w="1.5") + "</button>\n" + m.group(1), "rail entry")
sub_once("static/index.html", r'(      <button class="nav-tab active has-tooltip has-tooltip--bottom" data-panel="chat"[^\n]*\n)',
         lambda m: f'      <button class="nav-tab has-tooltip has-tooltip--bottom" {_MM_ATTRS} data-label="心智圖">'
                   + _MM_ICON.format(s=18, w="2") + "</button>\n" + m.group(1), "mobile nav entry")
edit("static/index.html", '    <div class="panel-view" id="panelKanban">\n',
     # Empty on purpose: the map needs the room, so the desktop sidebar is hidden
     # (mindmap.css). Upstream's mobile drawer still needs a panel to show.
     '    <div class="panel-view" id="panelMindmap"><div class="panel-head"><span>心智圖</span></div></div>\n'
     '    <div class="panel-view" id="panelKanban">\n')
edit("static/index.html", '    <div id="mainKanban" class="main-view">\n',
     '    <div id="mainMindmap" class="main-view"></div>\n    <div id="mainKanban" class="main-view">\n')
edit("static/index.html", '<link rel="stylesheet" href="static/style.css?v=__WEBUI_VERSION__">\n',
     '<link rel="stylesheet" href="static/style.css?v=__WEBUI_VERSION__">\n'
     '<link rel="stylesheet" href="static/mindmap.css?v=__WEBUI_VERSION__">\n')
edit("static/index.html", '<script src="static/panels.js?v=__WEBUI_VERSION__" defer></script>\n',
     '<script src="static/mindmap.js?v=__WEBUI_VERSION__" defer></script>\n'
     '<script src="static/panels.js?v=__WEBUI_VERSION__" defer></script>\n')

# ── Postconditions ───────────────────────────────────────────────────────
try:
    from packaging.requirements import Requirement
    from packaging.specifiers import SpecifierSet
except ImportError:
    sys.exit("[overlay] needs `packaging` installed")

meta = tomllib.loads(PP.read_text(encoding="utf-8"))
project = meta["project"]
if project.get("name") != DIST_NAME:
    fail(f"name is {project.get('name')!r}")
if "Unofficial" not in project.get("description", ""):
    fail(f"description does not say it is unofficial: {project.get('description')!r}")
if project.get("urls", {}).get("Source") != REPO:
    fail(f"Source URL is not this repository: {project.get('urls')}")
if project.get("license") != "MIT":
    fail(f"license is not the SPDX expression MIT: {project.get('license')!r}")
if any(c.startswith("License ::") for c in project.get("classifiers", [])):
    fail("a License classifier remains beside the SPDX expression")
if project.get("version") != EXPECTED_VERSION:
    fail(f"version {project.get('version')!r} != expected {EXPECTED_VERSION!r}")
if "tool" in meta and "setuptools_scm" in meta["tool"]:
    fail("[tool.setuptools_scm] still present")
if project.get("scripts", {}).get("hermes-webui") != "bootstrap:main":
    fail(f"console script changed upstream: {project.get('scripts')}")

reqs = [ln.strip() for ln in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.lstrip().startswith("#")]
url_left = [r for r in reqs if Requirement(r).url]
if url_left:
    fail(f"URL dependencies in requirements.txt: {url_left}")

spec = SpecifierSet(project["requires-python"])
supported = [f"3.{m}" for m in range(11, 20) if spec.contains(f"3.{m}")]
for required in REQUIRED_PYTHONS:
    if required not in supported:
        fail(f"Python {required} is not supported ({spec}) — the air-gapped target cannot install it")
print(f"[overlay] supported Python: {', '.join(supported)}")

declared = set(meta["tool"]["setuptools"].get("py-modules", []))
for f in ROOT.glob("*.py"):
    for mod in re.findall(r"^\s*(?:from|import) (\w+)", f.read_text(encoding="utf-8"), re.M):
        if (ROOT / f"{mod}.py").exists() and mod not in declared:
            fail(f"{f.name} imports root module {mod!r}, which the wheel would not ship")

boot = (ROOT / "bootstrap.py").read_text(encoding="utf-8")
if "curl -fsSL" in boot:
    fail("bootstrap.py still runs a network installer")

if failures:
    sys.exit(f"[overlay] {len(failures)} step(s) failed — not building")

(ROOT / "horo-build.json").write_text(
    json.dumps({"version": EXPECTED_VERSION, "pythons": supported}) + "\n", encoding="utf-8")
print("[overlay] done")
