"""WSGI entrypoint for the doc-hosting Django project."""

from __future__ import annotations

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "doc_hosting.django_project.settings")

application = get_wsgi_application()
