doc-hosting
===========

``doc-hosting`` is a proof of concept for building, publishing, and serving
static documentation from S3-compatible storage in a Juju deployment. Its
FastAPI service provides a health endpoint, an authenticated publishing API,
a public versions API, and documentation URLs backed by S3 objects.

The repository demonstrates a self-hosting pipeline: CI builds documentation,
``scripts/publish.py`` uploads it and registers the build, and the
``doc-hosting-api`` charm serves it.

How-to guides
-------------

Complete a deployment, publishing, build, test, or documentation task.

:doc:`Go to the how-to guides <how-to/index>`

Reference
---------

Look up components, HTTP behavior, configuration, and storage layout.

:doc:`Go to the reference <reference/index>`

.. toctree::
   :hidden:
   :maxdepth: 2

   how-to/index
   reference/index
