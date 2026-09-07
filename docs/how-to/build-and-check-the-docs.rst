Build and check the documentation
=================================

Use the repository's Make targets to build and validate documentation.

Prerequisites
-------------

Install Python 3.12 and ``make``. The root Makefile installs ``uv`` with
``python3 -m pip`` if it is unavailable and synchronizes the locked ``docs``
dependency group into ``.venv``.

Build or preview the documentation
----------------------------------

From the repository root, build the ``dirhtml`` output with warnings treated
as errors:

.. code-block:: bash

   make docs-html

The output is in ``docs/_build``. For a rebuilding development server at
``http://127.0.0.1:8000``, run:

.. code-block:: bash

   make docs-run

Run documentation checks
------------------------

Run the individual checks from the repository root:

.. code-block:: bash

   make docs-linkcheck
   make docs-spelling
   make docs-woke
   make docs-vale
   make docs-lint-md

The accessibility check additionally requires npm and a locally usable Chrome
or Chromium installation. It installs ``pa11y`` under ``docs/_dev``:

.. code-block:: bash

   make docs-pa11y

Run ``make docs-help`` to list supported targets, ``make docs-clean`` to remove
generated documentation, or ``make docs-update`` to check the copied Sphinx
Stack support files for updates. Because this project declares dependencies in
``pyproject.toml`` rather than ``docs/requirements.txt``, the update script
cannot compare the dependency list and reports that limitation.

Use pre-commit
--------------

The hooks in ``docs/_dev/.pre-commit-config.yaml`` invoke spelling, link, and
inclusive-language checks. If ``pre-commit`` is installed, run them with:

.. code-block:: bash

   pre-commit run --config docs/_dev/.pre-commit-config.yaml --all-files

See :doc:`../reference/components` for where the documentation build fits in
the publishing pipeline.
