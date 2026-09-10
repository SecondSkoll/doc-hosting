Components
==========

Application
-----------

``app.py`` exposes the FastAPI application. It provides health, direct-upload,
and version endpoints, mounts Django admin at ``/manage/``,
resolves redirects, and serves content from S3. Django ORM services hold
control-plane rules and apply migrations at process startup; PostgreSQL
migration runs are serialized with an advisory lock.

Deployment services
-------------------

``scripts/deploy.py`` deploys these applications in the ``doc-hosting`` model:

.. list-table::
   :header-rows: 1

   * - Application
     - Role
   * - ``doc-hosting-api``
     - FastAPI serving/API layer and Django management interface.
   * - ``postgresql-k8s`` (``14/stable``)
     - Metadata and audit database through the ``postgresql`` integration.
   * - ``minio`` (``latest/edge``)
     - S3-compatible documentation content storage.
   * - ``s3-integrator`` (``2/stable``)
     - Supplies endpoint, bucket, and credentials through the ``s3`` integration.

Rock and charm
--------------

``rockcraft.yaml`` builds an amd64, bare-base FastAPI rock using Ubuntu 24.04 LTS
as its build base. It includes ``app.py``, ``manage.py``, and ``doc_hosting/``.
The ``doc-hosting-api`` charm uses the ``fastapi-framework`` extension and
requires one S3 and one PostgreSQL integration.

Automation
----------

``scripts/deploy.py`` provides ``setup``, ``build``, ``deploy``, ``all``, and
``teardown``. ``scripts/publish.py`` computes a file manifest, authenticates to
the API, uploads the files through the returned presigned URLs, and asks the
API to verify and register the publication. It requires the API token and
project secret, but holds no S3 credentials. The publish workflow runs on
manual dispatch. CI runs unit tests against PostgreSQL and declares an
artifact-build job.

These declarations describe configured behavior, not a local build result.
See :doc:`../how-to/run-tests` for the latest validation boundaries.
