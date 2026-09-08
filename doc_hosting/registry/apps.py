"""Django application configuration for the doc-hosting registry app."""

from __future__ import annotations

from django.apps import AppConfig


class DocHostingRegistryConfig(AppConfig):
    """Application holding the doc-hosting metadata and control-plane models."""

    name = "doc_hosting.registry"
    label = "registry"
    verbose_name = "doc-hosting registry"
    default_auto_field = "django.db.models.BigAutoField"
