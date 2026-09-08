Manage redirects, roots, layouts, and audit
============================================

Sign in to ``/manage/`` as a Django staff user. These operations change live
metadata and may copy or delete S3 objects. Back up the affected prefix first
and avoid publishing the project while an operation is in progress.

Manage redirects
----------------

#. Open **Redirects** and add a redirect.
#. Choose ``exact`` for one path or ``prefix`` for a segment-boundary subtree.
#. Enter same-site absolute source and destination paths, such as
   ``/docs/en/old`` and ``/docs/en/new``.
#. Save, then request the source without automatically following redirects and
   verify a 301 and the expected ``Location``.
#. Disable a rule to retain it without serving it, or delete it to remove it.

Exact rules win over prefix rules; the longest prefix wins, and prefix rules
preserve the suffix. The form rejects external destinations, reserved source
namespaces, registered-root shadowing, loops, and chains beyond 10 hops.

Migrate a project root
----------------------

#. Confirm that the destination is unclaimed, does not overlap another root,
   and has no conflicting prefix redirect at the old root.
#. Open **Path migrations**, select **Add**, choose the project, and enter the
   new root. Nested multi-segment roots are accepted.
#. Save. The operation copies old-prefix objects, atomically switches the
   project root, project redirects, old-to-new redirect, and audit event, then
   deletes exactly the copied old keys.
#. Verify that the new URL serves content, the old URL returns 301, and the
   operation state is ``completed``.

If copying fails, the operation remains ``pending``. If deletion fails after
the metadata switch, it remains ``switched``. Select non-completed records and
run **Resume/verify selected migrations**, or open and save one record, after
correcting the reported conflict. A stale migration refuses to copy, switch,
or delete; do not edit its read-only operation fields.

Change language and version dimensions
--------------------------------------

The two checkboxes select one of four layouts: language+version, language only,
version only, or root only.

#. Inspect the project's publications. Before disabling a dimension, reduce it
   to exactly one distinct label. If there are no publications, provide the
   label that must survive.
#. Inspect the whole S3 project prefix. Remove or relocate objects that do not
   belong to a recorded publication and resolve existing objects at proposed
   destination keys.
#. Open **Layout changes**, select **Add**, choose the project, and select the
   new dimensions. Supply labels when required.
#. Save. The operation copies content to the new keys, atomically switches the
   flags and redirects, and then deletes old keys.
#. Verify the new URL, old URL redirects, ``GET /api/v1/versions`` layout, and
   ``completed`` operation state.

.. important::

   Enabling a previously disabled dimension requires authoritative key lineage
   from an earlier layout-change state machine. Path shape is not treated as
   proof of publication membership. Any out-of-band object under the project
   root causes a 409, even if its path looks valid. Back up and remove
   unproven objects, complete the toggle, and republish them under the new
   layout. An empty root can be enabled directly.

The operation also rejects multiple surviving labels when disabling a
dimension, existing destination objects, redirect collisions, missing recorded
source keys, stale changes, and another unfinished change. These conflicts
leave the operation retryable. Correct the named key or redirect, restore a
missing source if required, then use **Resume/verify selected layout changes**.

Inspect the audit trail
-----------------------

Open **Audit events**. Filter by event type or search by project root. Events
cover project claims and secret rotation, publication upserts/deletions,
redirect changes, import, and operation request/switch/completion. Event rows
and payloads are read-only in the management interface.

See :doc:`../reference/management` for exact permissions and states and
:doc:`../reference/http-api` for serving and redirect behavior.
