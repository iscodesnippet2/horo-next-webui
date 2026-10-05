# horo-next-webui

An unofficial PyPI distribution of [Hermes WebUI](https://github.com/nesquena/hermes-webui).
Not affiliated with, endorsed by, or sponsored by its authors or Nous Research.

Upstream's packaging metadata builds a wheel that cannot start its server
(`managed_agent_startup` is not packaged), loads four front-end libraries from
a CDN, and ships some with known advisories. This repository rebuilds each
upstream release as a wheel that installs with `pip`, works offline, and
carries no known advisory.

```bash
pip install horo-next horo-next-webui
hermes-webui
```

Companion to [horo-next](https://github.com/iscodesnippet2/horo-next) (Hermes Agent),
and built the same way.

## How this repository is laid out

This is a fork of `nesquena/hermes-webui`, but **upstream code is never
committed here**. The default branch, `horo`, holds only the tooling:

| Path | What it does |
|---|---|
| `overlay/horo_overlay.py` | Packaging and launcher changes applied to an upstream checkout |
| `overlay/security_bump.py` | Raises Python dependency floors that admit a vulnerable version |
| `overlay/vendor_assets.py` | Self-hosts every front-end library at its lowest advisory-free release in the same major; rebuilds Mermaid so its embedded DOMPurify is fixed too |
| `overlay/verify_wheel.py` | Installs the wheel into a clean venv, starts the server, fetches every vendored asset |
| `overlay/audit.py` | Checks the resolved Linux / Python 3.12 dependency set against OSV |
| `airgap/probe.py` | Run on an air-gapped machine: which dependencies the mirror serves |
| `pypi/README.md` | The project page shown on PyPI |
| `.github/workflows/release.yml` | Tag → overlay → floors → front end → wheel → audit → verify → TestPyPI → PyPI |

`master` mirrors upstream and is not used by the release workflow.

## Principles

**Packaging and launch only.** Product behaviour is upstream's. The launcher
is changed because, installed from a wheel, upstream's would pipe a GitHub
installer into bash or create a virtualenv inside site-packages.

**Fail closed.** Code edits use exact anchors and stop the build if upstream
moved them. A library with no advisory-free release in its major version, a
CDN URL left in the front end, or an advisory in the resolved Python set holds
the release.

**Smallest clean change.** A vulnerable library moves to the lowest release
that has no advisory, within its major version, so no API breaks. Python floors
are raised the same way; transitive Python dependencies get no floor (pip
already picks the newest the index has), but the resolved set is audited.

**Verify by installing, not by building.** Upstream's own wheel builds and
uploads cleanly and cannot start. Every release is installed into a clean venv
on each supported Python and its server is started and fetched.

**A clean audit is a snapshot.** OSV is checked on the day of the build.
Advisories published later, or held only in other databases, are not covered.

## What it does not cover

- `package.json` dev tooling (ESLint) — never shipped.
- Libraries a bundle embeds, other than Mermaid's tree (rebuilt and audited
  package by package): KaTeX, PDF.js, Prism, xterm, js-yaml and
  streaming-markdown ship as single files with no runtime dependencies.
- The CSP still lists cdn.jsdelivr.net. Nothing loads from it; the header is
  upstream's.

## Releasing

Run the **release** workflow with the upstream tag and version, e.g.
`exp-v0.52.416` / `0.52.416`. With `publish` off it builds and verifies only.

One-time setup (repository settings and PyPI):

1. Set `horo` as the default branch.
2. Create environments `testpypi` and `pypi`; give `pypi` a required reviewer.
3. On PyPI and TestPyPI, add a pending Trusted Publisher: project
   `horo-next-webui`, owner `iscodesnippet2`, repository `horo-next-webui`,
   workflow `release.yml`, environment `pypi` / `testpypi` respectively.

## Running locally

```bash
git clone --depth 1 --branch exp-v0.52.416 https://github.com/nesquena/hermes-webui.git upstream
python overlay/horo_overlay.py upstream 0.52.416
python overlay/security_bump.py upstream --notes release-notes.md
HORO_NPM="npx -y npm@11.21.0" python overlay/vendor_assets.py upstream --notes release-notes.md
(cd upstream && uv build --wheel --out-dir ../dist)
python overlay/verify_wheel.py dist/horo_next_webui-0.52.416-py3-none-any.whl 3.12 0.52.416
```

Requires `packaging`, `uv`, and Node 22 with npm.
