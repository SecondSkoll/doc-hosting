#!/usr/bin/env python3
"""Django management entrypoint for the doc-hosting control plane.

The 12-factor charm runs ``python3 manage.py migrate`` before starting the
FastAPI service whenever this file is present in the application directory.
"""

from __future__ import annotations

import os
import sys


def main() -> None:
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "doc_hosting.django_project.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
