"""Serve Sonatype's known answers, for a local build without the token.

    python overlay/tests/sonatype_replay.py          prints the URL, serves until killed

Flags what the real API flagged in CI (Mermaid below 11.16.1, PDF.js below
6.2.108) and nothing else, so a local build picks the versions CI picks. It
proves the pipeline, not the data: only a CI run asks the real Sonatype.
"""
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fakes import State, serve  # noqa: E402

FIRST_CLEAN = {"mermaid": (11, 16, 1), "pdfjs-dist": (6, 2, 108)}


def flag(purl: str):
    m = re.fullmatch(r"pkg:npm/([\w-]+)@(\d+)\.(\d+)\.(\d+)", purl)
    if m and m[1] in FIRST_CLEAN and tuple(map(int, m.groups()[1:])) < FIRST_CLEAN[m[1]]:
        return [{"id": "replayed", "cve": "CVE-replayed", "cvssScore": 8.6, "title": "replayed"}]
    return []


if __name__ == "__main__":
    srv, base = serve(State(), flag)
    print(f"{base}/api/v3/component-report", flush=True)
    time.sleep(36000)
