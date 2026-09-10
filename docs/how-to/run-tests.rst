Run tests
=========

Run unit tests locally
----------------------

Install dependencies and run the suite:

.. code-block:: bash

   uv sync --dev
   uv run pytest tests/unit -v

Without ``POSTGRESQL_DB_CONNECT_STRING``, tests explicitly use a temporary
SQLite database. The PostgreSQL advisory-lock concurrency test is skipped in
that environment.

Run unit tests with PostgreSQL
------------------------------

Create a disposable PostgreSQL database, then provide its URL:

.. code-block:: bash

   POSTGRESQL_DB_CONNECT_STRING='postgresql://USER:PASSWORD@HOST:5432/DATABASE' \
     uv run pytest tests/unit -v

Do not put a real password in a committed script. CI uses a PostgreSQL 16
service and this variable, so it exercises PostgreSQL-specific behavior.

Run integration tests
---------------------

Integration tests require Linux on amd64, MicroK8s, and a Juju controller:

.. code-block:: bash

   uv run scripts/deploy.py setup
   uv run pytest tests/integration -v -m integration

The integration test deploys MinIO, ``s3-integrator``, PostgreSQL, and the API
charm, then checks publish and serve behavior. To reuse existing artifacts,
set ``CHARM_FILE`` and ``APP_IMAGE`` in the environment before running it.

The latest local review did not run this suite or the rock/charm builds because
the sandbox lacked usable snapd/Juju. It ran 273 unit tests successfully; one
PostgreSQL-only concurrency test was skipped under SQLite (274 collected).
