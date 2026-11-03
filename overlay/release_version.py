"""Check a release version against the upstream tag it is built from.

    python overlay/release_version.py <version> <upstream-tag>

X.Y.Z is upstream vX.Y.Z (or exp-vX.Y.Z) as it is. X.Y.Z.N is horo's Nth
revision on the same upstream release: a change to the overlay, such as the
mind map. .postN on either rebuilds the same code with raised dependencies.

Anything else is refused, not normalised: the version also names files and is
passed to shell commands, and PEP 440 would rewrite "01" or "post01" into a
wheel name the workflow does not expect.
"""
from __future__ import annotations

import re
import sys

_NUMBER = r"(?:0|[1-9]\d*)"
_VERSION = re.compile(rf"({_NUMBER}\.{_NUMBER}\.{_NUMBER})(?:\.([1-9]\d*))?(?:\.post({_NUMBER}))?")


def upstream_base(version: str) -> str:
    """The upstream X.Y.Z a release version is built from; ValueError if malformed."""
    match = _VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"version {version!r} is not X.Y.Z or X.Y.Z.N (N from 1), optionally followed by .postN")
    return match.group(1)


def check(version: str, tag: str) -> str | None:
    """Why ``version`` cannot be built from ``tag``, or None if it can."""
    try:
        base = upstream_base(version)
    except ValueError as exc:
        return str(exc)
    if tag not in (f"v{base}", f"exp-v{base}"):
        return f"version {version} is built from upstream v{base} or exp-v{base}, not {tag!r}"
    return None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip().splitlines()[2].strip(), file=sys.stderr)
        return 2
    problem = check(*argv)
    if problem:
        print(problem, file=sys.stderr)
        return 1
    print(f"version {argv[0]} matches upstream {argv[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
