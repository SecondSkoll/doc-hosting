Deploy the proof of concept locally
===================================

Deploy the complete stack on a local Juju and MicroK8s environment, publish
this documentation, and verify that the API serves it.

Prerequisites
-------------

Use a Linux amd64 host with ``sudo``, snapd, Git, and ``uv``. Run setup in an
interactive terminal and do not run the complete script as root.

.. warning::

   This deployment uses plain HTTP. Keep services and port forwards bound to
   localhost, and do not expose its bearer token or S3 credentials.

Set up, build, and deploy
-------------------------

.. code-block:: bash

   uv sync --dev
   uv run scripts/deploy.py all

``all`` runs ``setup``, ``build``, and ``deploy``. Setup installs the required
snaps, initializes LXD when needed, enables MicroK8s host-path storage, registry,
and DNS, and bootstraps ``doc-hosting-controller``. If setup adds your account
to ``snap_microk8s``, log out and in, then rerun the command.

An existing MicroK8s installation must already use Kubernetes 1.34. Setup does
not change its channel automatically because Kubernetes upgrades must proceed
one minor release at a time. If an older cluster is disposable, remove it with
``sudo snap remove microk8s --purge`` and rerun setup to install
``1.34-strict/stable``. Back up and upgrade a cluster with valuable workloads
instead.

Deployment creates the ``doc-hosting`` model and deploys MinIO,
``s3-integrator`` track 2, and ``doc-hosting-api``. It writes credentials and
endpoints to ``.juju-deploy.env``. Treat this file as a secret.

Reach the services
------------------

.. code-block:: bash

   source .juju-deploy.env
   curl "$API_URL/health"

The response is ``{"status":"ok"}``. If cluster addresses are unreachable,
run these commands in separate terminals:

.. code-block:: bash

   microk8s kubectl port-forward -n doc-hosting svc/minio 9000:9000
   microk8s kubectl port-forward -n doc-hosting pod/doc-hosting-api-0 8080:8080

Then override the generated endpoints:

.. code-block:: bash

   export API_URL=http://localhost:8080
   export S3_ENDPOINT=http://localhost:9000
   curl "$API_URL/health"

Build and publish the documentation
-----------------------------------

.. code-block:: bash

   make docs-html DOCS_BUILDDIR=_build/dirhtml
   uv run scripts/publish.py --env-file .juju-deploy.env \
     --build-dir docs/_build/dirhtml

Environment variables override values from ``.juju-deploy.env``. The default
publication is stored under ``docs/en/latest/``.

Verify the publication
----------------------

.. code-block:: bash

   curl "$API_URL/docs/en/latest/"
   curl "$API_URL/docs/en/latest/reference/"
   curl "$API_URL/api/v1/versions?root_path=docs"

The first response contains the ``doc-hosting`` heading, and the second
contains the ``Reference`` heading. Edit ``docs/``, rebuild, and republish to
iterate. Use publish flags for another version, language, or root path; see
:doc:`publish-documentation`.

Tear down the deployment
------------------------

.. code-block:: bash

   uv run scripts/deploy.py teardown
   uv run scripts/deploy.py teardown --controller
   rm .juju-deploy.env

The first command destroys the model and its storage. The second also destroys
the controller and can remove this project's verified orphaned controller
namespace after an interrupted bootstrap. Installed snaps remain.

Troubleshoot the deployment
----------------------------

* For blocked applications, inspect ``juju status -m doc-hosting`` and
  ``juju debug-log -m doc-hosting``.
* For unreachable services, keep both port-forward commands running and
  re-export the localhost endpoints.
* For MicroK8s permission failures, log out and in after group membership is
  added.
* If setup reports an unregistered controller namespace and it is disposable,
  run ``uv run scripts/deploy.py teardown --controller`` before retrying.
* For a pending controller pod, use the scheduling, storage, and image-pull
  diagnostics printed by setup. Run ``sudo microk8s inspect`` for a full report.
* If CoreDNS or Calico is crash-looping, inspect the current and previous
   container logs printed by setup. Both failing together indicates an unhealthy
   Kubernetes cluster rather than a Juju problem. A stale cluster on an older
   snap revision should be upgraded one minor version at a time or, when
   disposable, purged and recreated on ``1.34-strict/stable``.

See :doc:`../reference/components` for the deployed components and
:doc:`../reference/configuration` for generated environment values.
