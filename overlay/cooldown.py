"""The 14-day cooldown on third-party releases.

    python overlay/cooldown.py        prints the cutoff, for `uv --exclude-newer`

A just-published release is where a compromised maintainer account shows up
first, so nothing younger than this ships. There are no exceptions: when the
only fix for an advisory is still inside the cooldown, the release is held
until it ages out.
"""
from __future__ import annotations

import datetime as dt

DAYS = 14


def cutoff() -> dt.datetime:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=DAYS)).replace(microsecond=0)


def stamp() -> str:
    return cutoff().strftime("%Y-%m-%dT%H:%M:%SZ")


def inside(published: str) -> bool:
    return dt.datetime.fromisoformat(published.replace("Z", "+00:00")) > cutoff()


if __name__ == "__main__":
    print(stamp())
