Deploy the proof of concept locally
===================================

Deploy MinIO, PostgreSQL, and the API on local Juju/MicroK8s, create an admin
account, publish this documentation, and verify it.

Prerequisites
-------------

Use a Linux amd64 host with ``sudo``, snapd, Git, and ``uv``. Run setup in an
interactive terminal and do not run the complete script as root.

.. warning::

   This deployment uses plain HTTP. Keep services and port forwards on a
   trusted machine. Do not expose its bearer token, project secret, admin
   session, or S3 credentials.

Set up, build, and deploy
-------------------------

.. code-block:: bash

   uv sync --dev
   uv run scripts/deploy.py all

``all`` runs ``setup``, ``build``, and ``deploy``. Setup installs the required
snaps, initializes LXD when needed, enables MicroK8s host-path storage,
registry, and DNS, and bootstraps ``doc-hosting-controller``. If setup adds
your account to ``snap_microk8s``, log out and in, then rerun it.

An existing MicroK8s installation must already use Kubernetes 1.34. Setup does
not upgrade it. The deployment creates the ``doc-hosting`` model and deploys
MinIO, ``s3-integrator`` track 2, ``postgresql-k8s`` track 14, and
``doc-hosting-api``. It integrates both S3 and PostgreSQL and waits for all four
applications.

The generated ``.juju-deploy.env`` contains credentials and endpoints,
including ``PROJECT_SECRET`` and ``ADMIN_URL``. Restrict access to it.

Create a superuser
------------------

No account or password is created automatically. Enter the API unit and run
the Django command with the application's injected environment available:

.. code-block:: bash

   uv run manage.py createsuperuser

Run this from a trusted administrative environment with the deployed
``APP_SECRET_KEY`` and ``POSTGRESQL_DB_CONNECT_STRING`` set. Do not copy their
values into shell history. The command creates the account in the same
PostgreSQL database used by the service.

Before exposing the admin through an ingress, configure the public host and
HTTPS origin:

.. code-block:: bash

   uv run juju config -m doc-hosting doc-hosting-api \
     allowed-hosts=docs.example.com \
     csrf-trusted-origins=https://docs.example.com

Terminate TLS at the ingress. Open the generated ``ADMIN_URL`` and sign in.

Reach and verify the services
-----------------------------

Read ``API_URL`` from the generated file and set it in your shell without
executing the file as shell code. Then check health:

.. code-block:: bash

   uv run curl "$API_URL/health"

The response is ``{"status":"ok"}``. Direct upload requires both the API and
the S3 endpoint embedded in its presigned URLs to be reachable from the
publishing machine. If cluster addresses are unreachable, use a trusted host
with cluster access or expose both services securely. This API port forward is
useful for health and management checks, but changing the publisher's
``S3_ENDPOINT`` does not rewrite URLs signed by the API:

.. code-block:: bash

   uv run microk8s kubectl port-forward --address 127.0.0.1 \
     -n doc-hosting pod/doc-hosting-api-0 8080:8080

Publish and verify
------------------

Run these commands from a machine that can reach ``API_URL`` and the storage
host in the API's presigned URLs:

.. code-block:: bash

   uv run --group docs sphinx-build -b dirhtml docs docs/_build/dirhtml
   uv run scripts/publish.py --env-file .juju-deploy.env \
     --build-dir docs/_build/dirhtml
   uv run curl "$API_URL/docs/en/latest/"
   uv run curl "$API_URL/api/v1/versions?root_path=docs"

The first publication claims ``docs`` using the generated project secret. See
:doc:`publish-documentation` for other roots and layouts.

Tear down
---------

.. code-block:: bash

   uv run scripts/deploy.py teardown
   uv run scripts/deploy.py teardown --controller

The first command destroys the model and its storage. The second also destroys
the controller and can remove a verified orphaned controller namespace. Delete
``.juju-deploy.env`` securely after teardown.

Troubleshoot
------------

* Inspect blocked applications with ``uv run juju status -m doc-hosting`` and
  ``uv run juju debug-log -m doc-hosting``.
* Keep both port forwards running and override both host-side endpoints.
* After MicroK8s group membership changes, log out and in.
* Resume an interrupted controller setup only after following the diagnostics
  printed by the script.

This deployment procedure was not executed in the latest sandbox review
because snapd and Juju were unavailable. Its command and configuration names
were checked against ``scripts/deploy.py`` and the charm declaration.
