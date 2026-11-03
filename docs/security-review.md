# Reviewing horo-next-webui

For whoever evaluates horo-next-webui for use inside an organisation — written
with Sonatype (Lifecycle / IQ Server, Nexus Firewall) in mind, but nothing here
is specific to one product.

## What it is

An unofficial PyPI repackaging of [Hermes WebUI](https://github.com/nesquena/hermes-webui)
(MIT), the browser front end for Hermes Agent. Upstream's own wheel cannot start
its server. horo-next-webui rebuilds each upstream tag as a wheel that installs
with pip and works offline, and adds one feature, the agent mind map
([`overlay/mindmap/`](../overlay/mindmap/)). Every change is in
[`overlay/horo_overlay.py`](../overlay/horo_overlay.py) and
[`overlay/vendor_assets.py`](../overlay/vendor_assets.py), with the reason for
each.

- `horo-next-webui X.Y.Z` = upstream `vX.Y.Z` or `exp-vX.Y.Z`; `X.Y.Z.N` is
  horo's Nth revision on it, `.postN` a rebuild with raised dependencies.
- Same command (`hermes-webui`) and state directory (`~/.hermes/webui`) as upstream.
- Runs beside [horo-next](https://github.com/iscodesnippet2/horo-next) (Hermes
  Agent). Python 3.11–3.13.

## Why it may look unfamiliar to a scanner

| What the scanner sees | What it is | Where it is described |
|---|---|---|
| A new, low-popularity PyPI project | A repackaging of an existing project under a new name | SBOM `metadata.component.pedigree` names the upstream tag |
| Minified JavaScript under `static/vendor/` with no `package.json` | Front-end libraries, served from the wheel instead of a CDN | Each is an SBOM component with `horo:ships=static/vendor` |
| `mermaid.min.js` that matches no published Mermaid file | Mermaid rebuilt here so its embedded dependencies are fixed releases | Its contents, from esbuild's metafile, are components with `horo:ships=bundled in mermaid@…`; the lockfile is in [`overlay/mermaid/`](../overlay/mermaid/) |

The rebuilt bundle is the one file hash-based identification should not be
expected to recognise. This has not been tested against a Sonatype commercial
product; the SBOM exists so that identification does not depend on it.

## How to evaluate it

1. **Import the SBOM**, not the wheel alone: `horo-next-webui-X.Y.Z.cdx.json`
   from the GitHub release (CycloneDX 1.5, validated against the schema at
   build time). Components are identified by purl. The copy inside the wheel
   (`.dist-info/sboms/horo-next-webui.cdx.json`) is identical except that it
   cannot carry the wheel's own hash.
2. **Check the wheel is the one the workflow built.** Its SHA-256 is in the
   release SBOM (`metadata.component.hashes`).
3. **Compare with our audit.** `audit-X.Y.Z.json` lists every package checked
   and every finding, against OSV and Sonatype's data through the Sonatype
   Guide (formerly OSS Index) API. That API serves a subset of Sonatype's
   commercial data, so your result can differ. Your policies may also differ
   (license, age, Release Integrity); a finding of yours absent from ours is
   worth reporting (see [SECURITY.md](../SECURITY.md)).

## With Sonatype specifically

- **Nexus Repository Firewall** evaluates each requested component by its
  coordinates: `horo-next-webui` itself, and each dependency as pip requests it.
  It sees the wheel as one package and cannot name the npm packages inside it.
- **Lifecycle (IQ Server)** evaluates an application. Import the release SBOM
  with the Third-Party Analysis REST API (`source=cyclonedx`). This is the step
  that makes the front end visible.

Sonatype documentation:
[Third-Party Analysis REST API](https://help.sonatype.com/iqserver/automating/rest-apis/third-party-scan-rest-api---v2) ·
[Firewall Quarantine](https://help.sonatype.com/en/firewall-quarantine.html) ·
[Release Integrity](https://help.sonatype.com/en/release-integrity.html) ·
[Configure PyPI with Nexus](https://help.sonatype.com/en/configure-pypi-with-nexus.html)

## Scope of the SBOM

- **Python** (`pkg:pypi/…`): the resolved set for Linux / CPython 3.12, each
  with every release file and its SHA-256. The wheel declares floors, not pins;
  `requirements-X.Y.Z-hashes.txt` is the pinned set.
- **npm, vendored** (`horo:ships=static/vendor`): each library the wheel serves,
  with the hash of the verified npm tarball.
- **npm, bundled** (`horo:ships=bundled in mermaid@…`): read from esbuild's
  metafile, so it lists what is inside the bundle, not the locked tree. Packages
  locked but tree-shaken away are not components, though the build still checks
  them.
- **Not included**: upstream's tests, ESLint and desktop tooling, and esbuild,
  which runs at build time only.

Hermes Agent is a separate package, with its own SBOM and audit.

## Network behaviour

Off by default here: the automatic update check against GitHub. The CSP names no
CDN, analytics or font host. Nothing loads from outside at page load; this is
tested in Chromium with outside requests refused.

Still present, and run only when a user invokes them: OAuth sign-in to hosted
model providers, cloud TTS and transcription, the extension gallery, and
requests to whichever model endpoint is configured.

## Licenses

horo-next-webui and Hermes WebUI: MIT. Each component's license is in the SBOM,
and the npm packages' license texts are in `THIRD_PARTY_LICENSES`. Two entries
differ from npm's metadata, on purpose:

- **KaTeX** is `MIT AND OFL-1.1`: npm declares MIT, but the fonts' own name
  tables declare the SIL Open Font License 1.1, whose text is included.
- **khroma** declares no license on npm; its tarball carries the MIT text.
