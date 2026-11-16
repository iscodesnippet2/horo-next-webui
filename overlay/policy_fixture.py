"""Start an installed WebUI under an admin policy, for verify_wheel.py and browser_smoke.py.

Without a policy every API answers 503, so each check runs with one. A policy
must be a file the running user neither owns nor can write, which a test
cannot create without root. The server is started through LAUNCH, which makes
the permission check accept exactly one path, the fixture's, and nothing else:
any other path, and the fixture's content and its reload, go through the real
checks. A server started plainly (server.py) has no such exception, and the
checks use one to prove a policy the user wrote is refused.
"""
from __future__ import annotations

import json
import os
from pathlib import Path

# python -c LAUNCH <site-packages> <trusted policy path>
LAUNCH = r"""
import os, runpy, sys
site, trusted = sys.argv[1], os.path.realpath(sys.argv[2])
sys.path.insert(0, site)
import api.horo_policy as policy
real = policy._permission_problem
policy._permission_problem = lambda path, st: None if str(path) == trusted else real(path, st)
sys.argv = [os.path.join(site, "server.py")]
runpy.run_path(sys.argv[0], run_name="__main__")
"""


def isolated_env(base: dict, home: Path) -> dict:
    """Hermes state of a throwaway home only: nothing reads or writes a real ~/.hermes."""
    for d in ("state", "locks", "webui"):
        (home / d).mkdir(parents=True, exist_ok=True)
    env = {**base, "HOME": str(home), "HERMES_HOME": str(home / ".hermes"),
           "XDG_STATE_HOME": str(home / "state"), "HERMES_GATEWAY_LOCK_DIR": str(home / "locks"),
           "HERMES_WEBUI_STATE_DIR": str(home / "webui")}
    for k in ("PYTHONPATH", "HERMES_WEBUI_AGENT_DIR", "HERMES_WEBUI_PYTHON", "HERMES_WEBUI_SETTINGS_FILE"):
        env.pop(k, None)
    return env


def write_policy(path: Path, policy) -> None:
    """Replace the policy in one step, with a new mtime, as an admin's editor would."""
    text = policy if isinstance(policy, str) else json.dumps(policy)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    st = path.stat()
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))


def launch_command(py: Path, site: Path, policy: Path) -> list[str]:
    return [str(py), "-c", LAUNCH, str(site), str(policy)]
