Management interface
====================

The Django admin is mounted at ``/manage/``. Authentication is Django's
staff-only login and mutation forms enforce CSRF protection. A superuser is
provisioned automatically at application startup from the charm's
``admin-username``/``admin-password`` options (defaults ``admin``/``admin``).
Change the password through the admin interface after first login;
re-provisioning never overwrites an existing user's password.

.. list-table::
   :header-rows: 1

   * - Area
     - Available operations
   * - Projects
     - Inspect roots and layout state, edit the domain, rotate the project
       secret through a write-only field, or delete. Direct creation and direct
       root/layout edits are disabled.
   * - Publications
     - Inspect current records or delete them with an audit event. Add and edit
       are disabled; publishing owns these records.
   * - Upload sessions
     - Inspect direct-upload manifests, key prefixes, status, expiry, and
       completion times. Add, edit, and delete are disabled; the API owns these
       records.
   * - Redirects
     - Add, update, enable/disable, and delete exact or prefix rules through the
       redirect validation service.
   * - Path migrations
     - Create a root migration and resume a non-completed operation. Operation
       fields become read-only after creation.
   * - Layout changes
     - Select language/version dimensions and resume a non-completed operation.
       Operation fields become read-only after creation.
   * - Audit events
     - Search/filter and inspect. Add, edit, and delete are disabled.

Operation states
----------------

Root migrations and layout changes use ``pending``, ``switched``, and
``completed``:

* ``pending``: copy and metadata switch are outstanding.
* ``switched``: metadata is active and old-key deletion remains.
* ``completed``: old copied keys were deleted and the completion was audited.

Failures before or during these stages leave a recorded operation that can be
resumed, subject to stale-operation and conflict guards. See
:doc:`../how-to/manage-control-plane` for operator procedures.
