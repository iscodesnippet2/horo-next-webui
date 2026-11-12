# horo-next-webui

An **unofficial** PyPI distribution of [Hermes WebUI](https://github.com/nesquena/hermes-webui), the browser interface for Hermes Agent. It is not affiliated with, endorsed by, or sponsored by the Hermes WebUI authors or Nous Research.

```bash
pip install horo-next horo-next-webui
hermes-webui
```

Install it into the same environment as [horo-next](https://pypi.org/project/horo-next/) (Hermes Agent). The command is `hermes-webui` and state lives in `~/.hermes/webui`, as upstream.

It listens on `127.0.0.1:8787` unless told otherwise: `hermes-webui --host 0.0.0.0 --port 9000` (or `HERMES_WEBUI_HOST` / `HERMES_WEBUI_PORT`). If the port is taken it stops, names a free one to use instead, and leaves the other service alone.

## What differs from upstream

Packaging and launch, plus one feature: an agent mind map across profiles. Everything else behaves as upstream.

- **Works offline.** Prism, xterm, Mermaid and PDF.js are served from the wheel instead of cdn.jsdelivr.net.
- **No known advisories in shipped code at release time**, in OSV or Sonatype's public data. Every front-end library is moved to the lowest clean release, and Mermaid is rebuilt from a lockfile so the DOMPurify it embeds is a fixed release too. Python dependency floors that admit a vulnerable version are raised. Each release notes exactly what changed.
- **The launcher never installs anything.** Without Hermes Agent it tells you to `pip install horo-next`, instead of running upstream's GitHub installer or creating a virtualenv inside site-packages.
- **`--host` and `--port` on the command line.** A port another service holds is refused with a free one to use instead, including on macOS where `--host 0.0.0.0` could otherwise start beside a listener on `127.0.0.1`.
- The server's working directory is the WebUI state directory, not site-packages.
- **No automatic outbound traffic.** The GitHub update check is off by default, and the CSP names no CDN, analytics or font host.
- **Agent mind map.** One panel shows the sessions a person is part of across every profile, from the last 7 days, and opens any of them as a full-screen workspace. It reads `state.db` read-only and calls no model.
- Python 3.11–3.13.

## Versions

`horo-next-webui X.Y.Z` is built from upstream `vX.Y.Z` or `exp-vX.Y.Z`. `X.Y.Z.N` is horo's Nth revision on the same upstream release, such as a fix to the mind map. `.postN` on either rebuilds the same code with raised dependencies.

## Offline installation

Each release attaches `requirements-X.Y.Z.txt`, the full resolved dependency set for Linux / CPython 3.12, and `requirements-X.Y.Z-hashes.txt`, the same set for `pip install --require-hashes`. On a machine with internet access:

```bash
pip download horo-next-webui==X.Y.Z -d wheelhouse
```

Then on the offline machine:

```bash
pip install --no-index --find-links wheelhouse horo-next-webui
```

## Developing offline

Each release also ships a source distribution: the tree the wheel was built from, upstream's tests, where it came from (`horo/SOURCE.json`: upstream tag and commit, overlay commit) and the patch against upstream. It builds without network access, into a wheel that installs the same files as the one here (each release checks this), so changes made on an air-gapped machine can be carried from one release to the next. `horo/DEVELOPING.md` inside it describes how.

```bash
pip download --no-deps --no-binary horo-next-webui horo-next-webui==X.Y.Z
```

## Security review

Each [GitHub release](https://github.com/iscodesnippet2/horo-next-webui/releases) attaches a CycloneDX SBOM, the vulnerability audit and the third-party notices. See the [security policy](https://github.com/iscodesnippet2/horo-next-webui/blob/horo/SECURITY.md) and the [reviewer's guide](https://github.com/iscodesnippet2/horo-next-webui/blob/horo/docs/security-review.md).

## License

MIT. Hermes WebUI is copyright its contributors; bundled front-end libraries carry their own licenses, in `THIRD_PARTY_LICENSES` inside the wheel. KaTeX's fonts are OFL-1.1.
