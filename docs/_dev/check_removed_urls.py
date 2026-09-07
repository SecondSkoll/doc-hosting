#!/usr/bin/env python3
"""Fail when a removed built URL has no source redirect."""

import csv
import sys
from pathlib import Path


def lines(path: Path) -> set[str]:
    return {line.strip() for line in path.read_text().splitlines() if line.strip()}


def main() -> int:
    base = Path("base/docs/urls.txt")
    compare = Path("compare/docs/urls.txt")
    redirects = Path("compare/docs/redirects.txt")
    if not base.exists() or not compare.exists():
        print("Both base/docs/urls.txt and compare/docs/urls.txt are required")
        return 1
    sources = set()
    if redirects.exists():
        for line in redirects.read_text().splitlines():
            if line.strip() and not line.lstrip().startswith("#"):
                sources.add(next(csv.reader([line], delimiter=" ", skipinitialspace=True))[0])
    missing = []
    for url in sorted(lines(base) - lines(compare)):
        path = url.removeprefix("./").removeprefix("/").removesuffix(".html").rstrip("/")
        candidates = {"index.rst"} if not path else {f"{path}.rst", f"{path}/index.rst", f"{path}/"}
        if candidates.isdisjoint(sources):
            missing.append(url)
    if missing:
        print("Removed URLs without redirects:\n" + "\n".join(missing))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
