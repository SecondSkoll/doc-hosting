"""URL configuration for the doc-hosting control plane.

The Django application is mounted under ``/manage/`` by the FastAPI server,
so every pattern here lives below the ``manage/`` prefix: the staff-only
admin site and the static assets it needs (served from the staticfiles
finders so no separate collection step is required in the rock, with a
``STATIC_ROOT`` fallback for deployments that run ``collectstatic``).
"""

from __future__ import annotations

import posixpath
from pathlib import Path

from django.conf import settings
from django.contrib import admin
from django.http import FileResponse, Http404, HttpRequest
from django.urls import path

admin.site.site_header = "doc-hosting administration"
admin.site.site_title = "doc-hosting admin"
admin.site.index_title = "Documentation hosting control plane"


def serve_static(request: HttpRequest, path: str) -> FileResponse:
    """Serve a static asset from the staticfiles finders or ``STATIC_ROOT``."""
    from django.contrib.staticfiles import finders

    normalized = posixpath.normpath(path).replace("\\", "/")
    if not normalized or normalized.startswith(("/", "../")) or normalized == "..":
        raise Http404
    absolute = finders.find(normalized)
    if absolute is None and settings.STATIC_ROOT:
        candidate = Path(settings.STATIC_ROOT) / normalized
        if candidate.is_file():
            absolute = str(candidate)
    if absolute is None:
        raise Http404
    return FileResponse(open(absolute, "rb"))


urlpatterns = [
    path("manage/static/<path:path>", serve_static),
    path("manage/", admin.site.urls),
]
