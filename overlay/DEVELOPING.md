# Developing horo-next-webui offline

This sdist is the tree the wheel of the same version was built from: Hermes
WebUI at the upstream commit in `SOURCE.json`, with the horo overlay applied
and the front-end libraries already self-hosted under `static/vendor`. It
builds without network access, and the wheel it builds installs the same files
as the one on PyPI (the release checks this).

| Path | What it is |
|---|---|
| `horo/SOURCE.json` | Version, upstream tag and commit, overlay commit |
| `horo/overlay.patch` | Every change against upstream except `static/vendor` (see `horo-vendor.json`) |
| `horo/overlay/` | The overlay scripts that made this tree, for reference; they need the network |
| `tests/` | Upstream's tests |

## Get it from the mirror

`airgap/probe.py --develop` checks the mirror serves it, and setuptools.

```bash
pip download --no-deps --no-binary horo-next-webui horo-next-webui==VERSION
tar xzf horo_next_webui-VERSION.tar.gz
```

## Keep local changes across releases

New upstream releases reach the air-gapped machine only as new horo-next-webui
releases. Keep each release's sdist on a branch of its own and your changes on
another, and merge:

```bash
cd horo-webui-dev                       # once: git init, then the first import below
git switch vendor
git rm -rq . && tar xzf ../horo_next_webui-VERSION.tar.gz --strip-components=1
git add -A && git commit -m "horo-next-webui VERSION"
git switch main && git merge vendor     # resolve conflicts with your changes here
```

## Build and test

Building needs setuptools 80 or later from the mirror, nothing else:

```bash
python -m pip wheel --no-build-isolation --no-deps --no-index -w dist .
python -m pip install -r requirements-dev.txt   # pytest and friends, from the mirror
python -m pytest tests
```

Give a local build its own version so it is never mistaken for a release: edit
`version` in `pyproject.toml` to e.g. `VERSION+local1`.

Some upstream tests fail against the overlay by design, and a few fail on
upstream too. Run the untouched sdist once and keep the list of failures as
your baseline; only a failure outside it is yours. The ones by design assert:

- the front-end library versions upstream pins (KaTeX 0.16.22, js-yaml 4.1.0,
  xterm 5.3.0, Prism's SRI hashes) or CDN hosts in the CSP;
- upstream's launcher: its `bootstrap:main` entry point, its installer, its
  choice of Python, its working directory and port messages;
- source text the overlay changed, such as `const _INITIAL_MSG_LIMIT` or the
  boot opening a panel other than the chat;
- that every CLI session lookup scans the list: the overlay keeps it for up to
  five seconds, so a test that swaps the scan after another test filled it can
  fail in a full run and pass on its own;
- files the sdist leaves out: `docs/ui-ux/*.html` (it loads KaTeX 0.16.9 from
  a CDN) and upstream's other CI workflows.

## What cannot be done offline

- Upgrading a front-end library (KaTeX, Mermaid, PDF.js, …) needs npm; wait
  for a release.
- A local wheel has not been through the release's OSV and Sonatype audit or
  the 14-day cooldown. Only your mirror's own scanning applies to what it
  depends on.
