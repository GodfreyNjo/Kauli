"""Fails when a file has trailing whitespace or no final newline.

The same rules as .editorconfig, enforced. (Line endings are left to git, which stores LF.)
"""
from __future__ import annotations

import sys
from pathlib import Path


def problems_in(path: Path) -> list[str]:
    raw = path.read_bytes()
    found = []
    if raw and not raw.endswith(b"\n"):
        found.append("has no newline at the end")
    for number, line in enumerate(raw.decode("utf-8", errors="ignore").splitlines(), start=1):
        if line != line.rstrip():
            found.append(f"line {number} has trailing whitespace")
            break
    return found


def main(paths: list[str]) -> int:
    failed = False
    for name in paths:
        for problem in problems_in(Path(name)):
            print(f"{name}: {problem}")
            failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
