Build the rock and charm
========================

Build the deployable artifacts with the same sequence as
``scripts/deploy.py build``.

Prerequisites
-------------

Install ``uv``, Rockcraft, and Charmcraft. The local registry used below must
be available at ``localhost:32000``; :doc:`deploy-the-poc-locally` provisions
it through MicroK8s.

Build and push the rock
-----------------------

From the repository root, export the application requirements required by the
FastAPI framework extension:

.. code-block:: bash

   uv export --frozen --no-dev --no-hashes --no-emit-project \
     --format requirements.txt --output-file requirements.txt

Pack the rock with experimental extensions enabled:

.. code-block:: bash

   ROCKCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=true uv run rockcraft pack

Copy the newest ``doc-hosting-api_*.rock`` to the local registry:

.. code-block:: bash

   uv run rockcraft.skopeo copy --insecure-policy --dest-tls-verify=false \
     oci-archive:doc-hosting-api_0.1_amd64.rock \
     docker://localhost:32000/doc-hosting-api:0.1

Replace the archive name with the file produced by Rockcraft.

Build the charm
---------------

Run Charmcraft against ``charm/``:

.. code-block:: bash

   CHARMCRAFT_ENABLE_EXPERIMENTAL_EXTENSIONS=true uv run charmcraft pack \
     --project-dir charm

Alternatively, run the complete exact sequence, including automatic newest
artifact selection, with:

.. code-block:: bash

   uv run scripts/deploy.py build

To reuse artifacts in integration tests, set ``CHARM_FILE`` to the packed
charm and ``APP_IMAGE`` to ``localhost:32000/doc-hosting-api:0.1``. See
:doc:`../reference/components` for rock build details.
