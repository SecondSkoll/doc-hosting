"""ASGI entrypoint for the doc-hosting API layer rock (contract: ``app:app``)."""

from doc_hosting.server import create_app

app = create_app()
