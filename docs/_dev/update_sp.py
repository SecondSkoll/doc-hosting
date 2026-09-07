#!/usr/bin/env python3
"""Report the local Sphinx Stack version and the latest upstream release."""

import json
import urllib.request
from pathlib import Path


def main() -> int:
    local = (Path(__file__).parent / "version").read_text().strip()
    with urllib.request.urlopen("https://api.github.com/repos/canonical/sphinx-stack/releases/latest", timeout=10) as response:
        latest = json.load(response)["tag_name"]
    print(f"Local Sphinx Stack version: {local}")
    print(f"Latest Sphinx Stack release: {latest}")
    print("Dependencies are declared in pyproject.toml; requirements.txt was not checked.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
