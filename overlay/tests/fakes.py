"""Small stand-ins for OSV, Sonatype Guide, PyPI and the npm registry.

They reproduce the response shapes of the real services, for the failure paths
a release build cannot provoke: a rejected token, a component left unanswered,
a fix published yesterday. Sonatype's real behaviour is pinned here: it echoes
coordinates normalized (`%40` decoded, names lower-cased, PyPI `_` kept).
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import tarfile
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class State:
    def __init__(self) -> None:
        self.sonatype: dict[str, list[dict]] = {}   # purl (as asked) → vulnerabilities
        self.sonatype_status = 200
        self.sonatype_drop: set[str] = set()        # purls left unanswered
        self.sonatype_no_field: set[str] = set()    # answered without `vulnerabilities`
        self.osv: dict[tuple[str, str], list[str]] = {}
        self.osv_short = False                      # one result fewer than asked
        self.pypi: dict[str, dict[str, str]] = {}   # name → version → upload time
        self.npm: dict[str, dict[str, str]] = {}    # name → version → publish time
        self.tarballs: dict[tuple[str, str], bytes] = {}


def tarball(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, text in files.items():
            data = text.encode()
            info = tarfile.TarInfo(f"package/{name}")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def serve(state: State, flag=None):
    """(server, base url). `flag(purl) -> [vulnerabilities] | None` overrides state.sonatype."""

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def reply(self, body, status=200, raw=False):
            data = body if raw else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path == "/api/v3/component-report":
                if state.sonatype_status != 200:
                    return self.reply({"message": "error"}, state.sonatype_status)
                out = []
                for p in body["coordinates"]:
                    if p in state.sonatype_drop:
                        continue
                    entry = {"coordinates": urllib.parse.unquote(p).lower(), "reference": "https://example.invalid"}
                    if p not in state.sonatype_no_field:
                        entry["vulnerabilities"] = (flag(p) if flag else None) or state.sonatype.get(p, [])
                    out.append(entry)
                return self.reply(out)
            if self.path == "/v1/querybatch":
                res = [{"vulns": [{"id": i} for i in state.osv.get((q["package"]["name"], q["version"]), [])]}
                       for q in body["queries"]]
                return self.reply({"results": res[:-1] if state.osv_short else res})
            if self.path == "/v1/query":
                ids = state.osv.get((body["package"]["name"], body["version"]), [])
                return self.reply({"vulns": [{"id": i} for i in ids]})
            self.reply({}, 404)

        def do_GET(self):
            parts = urllib.parse.unquote(self.path).strip("/").split("/")
            if parts[0] == "pypi":
                rel = state.pypi[parts[1]]
                return self.reply({"releases": {v: [{"upload_time_iso_8601": t, "yanked": False}] for v, t in rel.items()}})
            if parts[0] == "tarballs":
                return self.reply(state.tarballs[(parts[1], parts[2])], raw=True)
            name = parts[0]
            if name not in state.npm:
                return self.reply({}, 404)
            base = f"http://127.0.0.1:{self.server.server_port}"
            if len(parts) == 2:
                data = state.tarballs[(name, parts[1])]
                integ = "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode()
                return self.reply({"dist": {"tarball": f"{base}/tarballs/{name}/{parts[1]}", "integrity": integ}})
            self.reply({"versions": {v: {} for v in state.npm[name]}, "time": state.npm[name]})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_port}"
