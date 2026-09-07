Run tests
=========

Run unit tests
--------------

Install the development dependencies and run the moto-backed unit suite:

.. code-block:: bash

   uv sync --dev
   uv run pytest tests/unit -v

Run integration tests
---------------------

Integration tests require Linux on amd64, MicroK8s, and a Juju controller.
Provision the controller first:

.. code-block:: bash

   uv run scripts/deploy.py setup
   uv run pytest tests/integration -v -m integration

The test builds the documentation, deploys MinIO, ``s3-integrator``, and the
API charm, then checks the publish-and-serve path. To skip packing and reuse
existing artifacts, set both values before running the test:

.. code-block:: bash

   export CHARM_FILE=/path/to/doc-hosting-api_amd64.charm
   export APP_IMAGE=localhost:32000/doc-hosting-api:0.1

See :doc:`build-the-rock-and-charm` for the exact artifact build commands and
:doc:`../reference/components` for component relationships.
