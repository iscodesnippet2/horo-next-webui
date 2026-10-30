# Security policy

horo-next-webui is an unofficial PyPI distribution of
[Hermes WebUI](https://github.com/nesquena/hermes-webui). It repackages upstream
releases. It changes packaging, the launcher and two outbound defaults; it does
not change what the product does.

## Reporting a vulnerability

- **In Hermes WebUI's own code**: report it to
  [upstream](https://github.com/nesquena/hermes-webui/security). A fix reaches
  horo-next-webui with the next upstream release built here.
- **In horo-next-webui's packaging** (the overlay, the wheel's contents, the
  release workflow, a dependency version chosen here): open a private advisory
  at <https://github.com/iscodesnippet2/horo-next-webui/security/advisories/new>.

## Supported versions

Only the latest release. A dependency fix on the same upstream release is
published as `X.Y.Z.postN`.

## What every release is checked for

A release is built and published only by the `release` workflow, from an
upstream tag, with no step done by hand. It stops before publishing if any of
these fails:

- **Known vulnerabilities: none allowed.** Every shipped component — the Python
  dependency set for Linux / CPython 3.12, every vendored front-end library,
  and every package esbuild put in the Mermaid bundle — is checked against
  [OSV](https://osv.dev) and against Sonatype's data (the Sonatype Guide /
  OSS Index API). A missing or rejected token, an HTTP error, or a component
  either database did not answer for holds the release.
- **Versions are raised to clean ones.** Front-end libraries and Python floors
  move to the lowest release clean in both databases. Mermaid is built from a
  committed lockfile with `npm ci --ignore-scripts`; a vulnerable package in it
  is raised with `npm update`, and the fix must appear in the lock.
- **14-day cooldown, no exceptions.** A just-published release is where a
  compromised maintainer account shows up first. Nothing younger ships, and a
  fix inside the cooldown holds the release until it ages out.
- **The wheel is installed and exercised** in clean environments on every
  supported Python (3.11–3.13; 3.12 is required), including front-end rendering
  in Chromium with outside requests refused.
- **The release files are checked against each other**, and that check is
  tested: real release files are corrupted one way at a time, and each
  corruption must be caught by the check responsible for it.

A clean audit holds on the day it runs. The daily `rescan` workflow audits the
latest release again and opens an issue on a new advisory.

## What every release publishes

Attached to each [GitHub release](https://github.com/iscodesnippet2/horo-next-webui/releases):

| File | Contents |
|---|---|
| `horo-next-webui-X.Y.Z.cdx.json` | CycloneDX 1.5 SBOM: every shipped component, the wheel's SHA-256, and the upstream release it was built from |
| `audit-X.Y.Z.json` | The vulnerability audit: when, against which database, every package checked, every finding |
| `THIRD_PARTY_LICENSES.txt` | License texts of every npm package in the front end, and the OFL text for KaTeX's fonts |
| `requirements-X.Y.Z.txt` | The resolved dependency set for Linux / CPython 3.12 |
| `requirements-X.Y.Z-hashes.txt` | The same set with hashes, for `pip install --require-hashes` |

The same SBOM is inside the wheel at `.dist-info/sboms/` (PEP 770), and the
notices at `.dist-info/licenses/`.

PyPI uploads use [Trusted Publishing](https://docs.pypi.org/trusted-publishers/):
no upload token exists, and PyPI verifies that each file came from this
repository's `release` workflow.

For a security reviewer: [docs/security-review.md](docs/security-review.md).
