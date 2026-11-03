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
| `overlay/horo_overlay.py` | Packaging, metadata, launcher and outbound-traffic changes applied to an upstream checkout; hangs the mind map on upstream's panels |
| `overlay/mindmap/` | The agent mind map: `/api/horo/overview` read model, panel script and styles |
| `overlay/security_bump.py` | Raises Python dependency floors that admit a vulnerable version |
| `overlay/vendor_assets.py` | Self-hosts every front-end library at its lowest advisory-free release; rebuilds Mermaid from a lockfile |
| `overlay/mermaid/` | The Mermaid build: entry point, `package.json`, committed `package-lock.json` |
| `overlay/cooldown.py` | The 14-day cooldown on third-party releases |
| `overlay/sonatype.py` | Sonatype Guide client, shared by every step that consults it |
| `overlay/sbom.py` | CycloneDX 1.5 SBOM and third-party notices; embeds the SBOM in the wheel (PEP 770) |
| `overlay/audit_shipped.py` | Checks every SBOM component against OSV **and Sonatype**; needs the `SONATYPE_GUIDE_TOKEN` secret |
| `overlay/verify_release.py` | Checks the wheel, SBOM, notices, hashes and audit against each other |
| `overlay/verify_wheel.py` | Installs the wheel into a clean venv, starts the server, fetches every vendored asset |
| `overlay/browser_smoke.py` | Runs upstream's Mermaid, KaTeX, PDF.js, Prism and xterm render paths in Chromium from the installed wheel, offline |
| `overlay/tests/` | Gate tests against fake services, and mutation tests on real release files |
| `overlay/sonatype_lookup.py` | Lists Sonatype's findings for every version of a package (via the `sonatype-lookup` workflow), to pick a replacement |
| `.github/workflows/rescan.yml` | Re-audits the latest release daily; opens an issue on a new advisory |
| `airgap/probe.py` | Run on an air-gapped machine: which dependencies the mirror serves |
| `pypi/README.md` | The project page shown on PyPI |
| `.github/workflows/release.yml` | Tag → overlay → floors → front end → SBOM → wheel → audit → verify → TestPyPI → PyPI |

Security policy: [SECURITY.md](SECURITY.md). For a reviewer: [docs/security-review.md](docs/security-review.md).

`master` mirrors upstream and is not used by the release workflow.

## Principles

**Packaging and launch, plus one feature.** Product behaviour is upstream's,
with one exception: the agent mind map, new files under `overlay/mindmap/` hung
on a handful of exact anchors. The launcher
is changed because, installed from a wheel, upstream's would pipe a GitHub
installer into bash or create a virtualenv inside site-packages. The one
runtime default changed is the automatic GitHub update check, now off; the CSP
no longer names CDN, analytics or font hosts.

**Fail closed.** Code edits use exact anchors and stop the build if upstream
moved them. A library with no advisory-free release in its allowed line, a
fix still inside the cooldown, a CDN URL left in the front end, an advisory in
the resolved set, or a component a database did not answer for holds the
release.

**Smallest clean change.** A vulnerable library moves to the lowest release
that has no advisory, within its semver line, so no API breaks; KaTeX, Mermaid
and PDF.js cross one by an explicit allowlist (`ALLOW_BREAKING`). Python floors
are raised the same way; transitive Python dependencies get no floor, but the
resolved set is audited.

**Fourteen days, no exceptions.** Nothing younger than 14 days ships. When the
only fix for an advisory is inside the cooldown, the release waits for it.

**Verify by installing, not by building.** Upstream's own wheel builds and
uploads cleanly and cannot start. Every release is installed into a clean venv
on each supported Python and its server is started and fetched.

**Zero known advisories, in OSV and Sonatype.** The enterprise mirror is
guarded by Sonatype, so every shipped component is checked against both, and
any finding — or a Sonatype check that could not run — holds the release.
Sonatype's public API (Sonatype Guide) serves a subset of its commercial data;
a clean result narrows the gap with the mirror, it cannot close it.

**A clean audit is a snapshot.** It holds on the day of the build. The daily
rescan checks the latest release again and opens an issue on a new advisory;
the fix is the same code rebuilt as `.postN` on the version it fixes.

## Agent mind map

A panel showing, across every profile, the sessions a person is part of (the
source allowlist in `overlay/mindmap/horo_overview.py`; cron and kanban are out)
from the last 7 days: which run, which wait for you, what changed since you
looked. Selecting a card uses only the overview, so it loads no transcript and
leaves the active profile alone. The detail card edits the goal, and its progress
line opens the workspace; requests for another profile go through `X-Horo-Profile`, a
per-request override checked after authentication that refuses what
`/api/profile/switch` refuses. Ctrl/Cmd-click, Enter or the progress line
lays upstream's chat view over the map as a full-screen workspace, which does
switch the active profile; the close button or Esc comes back without repainting
the map, and keeps the card selected. With a card selected, the arrow keys move
the selection, Enter opens the workspace and Esc lets go.

A card's goal is the session's title, which a person can edit; edits live in
`horo_goals.json` in the WebUI state directory, so `state.db` stays read-only and
read-only sessions work too. Progress is the last prompt with substance: messages
Hermes stores as `user` and bare acknowledgements are skipped. No model is involved.

## What it does not cover

- `package.json` dev tooling (ESLint) — never shipped.
- Libraries a bundle embeds, other than Mermaid's tree (rebuilt and audited
  package by package): KaTeX, PDF.js, Prism, xterm, js-yaml and
  streaming-markdown ship as upstream publishes them.
- Network features a user has to invoke (OAuth sign-in, cloud TTS and
  transcription, the extension gallery, hosted model providers) remain. Offline
  they fail and do nothing else.

## Releasing

Run the **release** workflow with the upstream tag and version, e.g.
`exp-v0.52.416` / `0.52.416`. With `publish` off it builds and verifies only.

**Versions** keep upstream's, so each release names what it was built from
(`overlay/release_version.py` holds a release to this):

| Version | Built from |
|---|---|
| `X.Y.Z` | upstream `vX.Y.Z` or `exp-vX.Y.Z`, with the overlay as it was |
| `X.Y.Z.N` (N from 1) | the same upstream release, with horo's Nth overlay revision (a feature or fix of ours) |
| either, plus `.postN` | the same code, rebuilt with raised dependencies |

pip orders them `0.52.544 < 0.52.544.post1 < 0.52.544.1 < 0.52.544.2 < 0.52.545`.
A new upstream release starts again at `X.Y.Z`, carrying every overlay change.

One-time setup (repository settings and PyPI):

1. Set `horo` as the default branch.
2. Create environments `testpypi` and `pypi`; give `pypi` a required reviewer.
3. Add a Sonatype Guide personal access token as the `SONATYPE_GUIDE_TOKEN` secret.
4. On PyPI and TestPyPI, add a pending Trusted Publisher: project
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
python -m unittest discover -s overlay/tests -p 'test_*.py'
```

Requires `packaging`, `uv`, and Node 22 with npm. Without the Sonatype token,
`overlay/tests/sonatype_replay.py` serves Sonatype's known answers (point
`SONATYPE_GUIDE_URL` at it) so versions match CI; it proves the pipeline, not
the data. To change the Mermaid build, edit `overlay/mermaid/package.json` and
regenerate the lock with `npm install --package-lock-only --ignore-scripts
--before=<14 days ago>`.
