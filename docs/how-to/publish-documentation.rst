Publish documentation
=====================

Publish with GitHub Actions
---------------------------

The ``Publish docs`` workflow runs on manual dispatch. Configure these
repository settings:

.. list-table::
   :header-rows: 1

   * - Setting
     - Kind
     - Value
   * - ``DOC_HOSTING_API_URL``
     - Variable
     - API base URL, including port 8080 when used directly.
   * - ``DOC_HOSTING_API_TOKEN``
     - Secret
     - Deployment-wide ``publish-token`` value.
   * - ``DOC_HOSTING_PROJECT_SECRET``
     - Secret
     - Stable secret for this project's root and permitted nested roots.
   * - ``DOC_HOSTING_DOMAIN``
     - Variable
     - Optional serving domain stored on the project.

Do not reuse the global token as the project secret. Never print either value
or store it in workflow YAML. Use TLS when a runner reaches the service over an
untrusted network. A GitHub-hosted runner cannot reach a private MicroK8s
cluster; expose the service securely or use a self-hosted runner.

For manual dispatch, optionally supply ``version``, ``language``, and
``root_path``. An omitted version uses ``latest``.

Publish from the command line
-----------------------------

Export the API URL and both credentials, or put them in a restricted, ignored
env file such as ``.juju-deploy.env``. Then build and publish:

.. code-block:: bash

   uv run --group docs sphinx-build -b dirhtml docs docs/_build/dirhtml
   uv run scripts/publish.py --env-file .juju-deploy.env \
     --build-dir docs/_build/dirhtml

``--build-dir`` is required. Optional flags are ``--root-path`` (``docs``),
``--language`` (``en``), ``--version``, ``--commit-hash``, ``--domain``, and
``--env-file``. Version defaults to the GitHub tag on tag builds and otherwise
``latest``. Commit defaults to ``GITHUB_SHA`` and then the current Git commit.
Domain defaults to ``DOC_DOMAIN`` and then ``localhost``.

The command requires ``API_URL``, ``API_TOKEN``, and ``PROJECT_SECRET``.
Environment values override the env file. The publisher does not need S3
credentials.

The script computes each file's relative path, size, and SHA-256, then:

1. Calls ``POST /api/v1/uploads`` with the publication details and manifest.
2. PUTs each file to its exact-key presigned URL with the returned checksum
   header.
3. Calls ``POST /api/v1/uploads/{upload_id}/finalize`` with the same manifest.

The API chooses the active-layout key prefix. It registers the publication
only after every object passes existence, size, and checksum verification. If
an upload or verification fails, rerun the command; a pending session can be
finalized only until its URLs and session expire. Verify the serving URL shown
by the script and the versions response.

Claim a root safely
-------------------

The first request that passes both credential gates claims the normalized
root. Later publishes require the same project secret. To publish a nested root
such as ``product/guides``, use the ancestor project's secret. A different
secret receives 403. A parent claim that would hide an existing descendant
receives 409.

The secret cannot be recovered from the service. If it is lost or compromised,
a staff user can rotate it on the project's management page; update the GitHub
secret before the next publication.

See :doc:`../reference/http-api` for response fields and errors and
:doc:`../reference/configuration` for all settings.
