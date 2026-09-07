Publish documentation
=====================

Publish with GitHub Actions
---------------------------

The ``Publish docs`` workflow runs on pushes to ``main``, tags matching
``v*``, and manual dispatch. Configure these repository settings:

.. list-table::
   :header-rows: 1

   * - Setting
     - Kind
     - Value
   * - ``DOC_HOSTING_API_URL``
     - Variable
     - API base URL, including port 8080
   * - ``DOC_HOSTING_API_TOKEN``
     - Secret
     - Value of the charm's ``publish-token``
   * - ``DOC_HOSTING_S3_ENDPOINT``
     - Variable
     - S3-compatible endpoint reachable by the runner
   * - ``DOC_HOSTING_S3_BUCKET``
     - Variable
     - Destination bucket
   * - ``DOC_HOSTING_S3_ACCESS_KEY``
     - Secret
     - S3 access key
   * - ``DOC_HOSTING_S3_SECRET_KEY``
     - Secret
     - S3 secret key
   * - ``DOC_HOSTING_DOMAIN``
     - Variable
     - Optional serving domain stored in the registry

For manual dispatch, optionally supply ``version``, ``language``, and
``root_path``. A tag build otherwise uses the tag as its version; other builds
use ``latest``. A GitHub-hosted runner cannot reach a private MicroK8s cluster,
so expose the service securely or use a self-hosted runner.

Publish from the command line
-----------------------------

Build the docs as described in :doc:`build-and-check-the-docs`, then run:

.. code-block:: bash

   uv run scripts/publish.py --env-file .juju-deploy.env \
     --build-dir docs/_build

``--build-dir`` is required. Optional flags are ``--root-path`` (``docs``),
``--language`` (``en``), ``--version``, ``--commit-hash``, ``--domain``, and
``--env-file``. Version defaults to the GitHub tag for tag builds and otherwise
``latest``. Commit defaults to ``GITHUB_SHA`` and then ``git rev-parse HEAD``.
Domain defaults to ``DOC_DOMAIN`` and then ``localhost``.

The command reads ``API_URL``, ``API_TOKEN``, ``S3_ENDPOINT``,
``S3_ACCESS_KEY``, ``S3_SECRET_KEY``, ``S3_BUCKET``, and ``S3_REGION`` from the
environment first and then from the env file. ``API_URL``, ``API_TOKEN``, both
keys, and the bucket are required. It prints one upload line per file, an
upload summary, the registered entry, and the final serving URL.

See :doc:`../reference/configuration` for a complete setting reference and
:doc:`../reference/storage-layout` for the resulting object keys.
