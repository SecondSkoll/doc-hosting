#!/usr/bin/env python3
"""Download the Canonical Vale configuration used by Sphinx Stack 2.0."""

import shutil
import subprocess
import tempfile
from pathlib import Path


def main() -> int:
    destination = Path(__file__).resolve().parent
    with tempfile.TemporaryDirectory() as temporary:
        source = Path(temporary)
        subprocess.run(
            ["git", "clone", "--depth", "1", "https://github.com/canonical/documentation-style-guide.git", str(source)],
            check=True,
        )
        for relative in ("styles/Canonical", "styles/config", "vale.ini"):
            target = destination / relative
            if target.exists():
                shutil.rmtree(target) if target.is_dir() else target.unlink()
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source / relative, target) if (source / relative).is_dir() else shutil.copy2(source / relative, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
