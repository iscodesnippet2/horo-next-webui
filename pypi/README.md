# horo-next-webui

An **unofficial** PyPI distribution of [Hermes WebUI](https://github.com/nesquena/hermes-webui), the browser interface for Hermes Agent. It is not affiliated with, endorsed by, or sponsored by the Hermes WebUI authors or Nous Research.

```bash
pip install horo-next horo-next-webui
hermes-webui
```

Install it into the same environment as [horo-next](https://pypi.org/project/horo-next/) (Hermes Agent). The command is `hermes-webui` and state lives in `~/.hermes/webui`, as upstream.

## What differs from upstream

Packaging and launch only. Product behaviour is upstream's.

- **Works offline.** Prism, xterm, Mermaid and PDF.js are served from the wheel instead of cdn.jsdelivr.net.
- **No known advisories in shipped code.** Every front-end library is moved to the lowest release in its major version with no OSV advisory. Mermaid is rebuilt so the DOMPurify it embeds is a fixed release too. Python dependency floors that admit a vulnerable version are raised. Each release notes exactly what changed.
- **The launcher never installs anything.** Without Hermes Agent it tells you to `pip install horo-next`, instead of running upstream's GitHub installer or creating a virtualenv inside site-packages.
- The server's working directory is the WebUI state directory, not site-packages.
- Python 3.11–3.13.

## Versions

`horo-next-webui X.Y.Z` is built from upstream `vX.Y.Z` or `exp-vX.Y.Z`. A packaging-only fix on the same upstream release is published as `X.Y.Z.postN`.

## Offline installation

Each release attaches `requirements-X.Y.Z.txt`, the full resolved dependency set. On a machine with internet access:

```bash
pip download horo-next-webui==X.Y.Z -d wheelhouse
```

Then on the offline machine:

```bash
pip install --no-index --find-links wheelhouse horo-next-webui
```

## License

MIT. Hermes WebUI is copyright its contributors; bundled front-end libraries carry their own licenses.
