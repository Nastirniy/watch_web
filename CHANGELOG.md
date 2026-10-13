# Changelog

All notable changes to this project are documented here.
Format: Keep a Changelog. Entries are grouped by step of the permission refactor.

## [Unreleased] - Permission system refactor

## PKI client certificates

### Added

- Table `user_certificates` (migration `024_user_certificates`, model `UserCertificate`): one row per client
  certificate a user logs in with, several per user (a renewal may overlap). `source` (`issued`, `uploaded`,
  `legacy`, `configured`), `label` (the readable name), the plain values `subject_cn`, `serial` (lower-case hex),
  `not_before`, `not_after` (empty for `legacy` and `configured`, which have only hashes), the hashes the login
  matches (`fingerprint_hash`, unique; `cn_hash`, `cn_hash_scheme`, `serial_hash`), `ca_issued`, `revoked_at`,
  `revoked_by`, `revocation_reason`, `files_dir`, `export_count`, `last_exported_at`, `created_at`, `created_by`.
  The state (`active`, `expired`, `revoked`) is computed when a row is read, never stored. Table
  `certificate_authority_state` (one row, id 1: the number and dates of the last published CRL).
- Migration `024_user_certificates` turns the certificate of every user into a `legacy` row with the user's exact
  hashes and name hash scheme (`created_at` = the user's): every certificate from before the upgrade keeps logging
  in. Its downgrade gives `users` the columns back: the values of the user's `legacy` row exactly, otherwise the
  hashes of the newest active row; a user with none, or whose CN or serial hash another restored user holds (the
  columns are unique), gets the placeholder `unassigned:<user_id>`, which matches no certificate. The downgrade
  output names every such user and counts the certificates, revocations and exports it drops.
- `POST /users/`: optional `certificate_label`, the readable name of the certificate (1 to 64 characters after
  trimming, no control characters; field code `certificate_label_invalid`; default the CN cut to 64 characters).
- 401 `certificate_revoked` on `POST /auth/login`: the certificate is registered but revoked (the person asks for a
  new one).
- Session revoke cause `certificate_revoked`: a session bound to a certificate that is no longer an active row of
  its user (revoked or expired) ends on its next request or refresh, which answers 401 `token_certificate_mismatch`
  as before; the sessions of the user's other certificates stay.
- Settings `settings.pki` (`MYSETTINGS_PKI__*`): `ca_cert_path`, `signer_socket`, `store_dir`, `crl_path`,
  `extra_crl_paths`, `validity_days` (365), `max_validity_days` (730), `key_size` (2048), `p12_encryption`
  (`aes256` or `legacy`), `crl_days` (7). An uploaded certificate signed by the client CA of `ca_cert_path` is
  `ca_issued` (it goes on the CRL when revoked).
- `python -m app.backend.cli.init_user`: the certificate path is optional; `--label` names the certificate (default
  its CN).
- `GET /certificates/authority` (any authenticated user): the client CA (`subject`, `not_after`, `fingerprint_sha256`,
  `certificate_pem`) and the published CRL (`crl.this_update`, `crl.next_update`, `crl.revoked_count`); 503
  `certificate_authority_not_configured` without PKI settings.
- `/health` has a `pki` part when `settings.pki` is configured (`status`, `ca_expires_in_days`,
  `crl_next_update_in_days` (the earliest of every CRL in the published file), `crl_stale`, `signer_available`);
  the overall status is `degraded` when it is (less than 2 days left, a CRL never published or stale, no signer).
- CRL publishing (`services/crl_publisher.py`): the API sends the revoked serials to the signer over its Unix
  socket and writes `crl_path` (the signed CRL, then the `extra_crl_paths` files) atomically; at API start when the
  file is missing or past half of its validity, and with `python -m app.backend.cli.pki crl`. The API refuses to
  start with a partial `settings.pki`, an unusable CA certificate, a folder it cannot write or an unparsable extra
  CRL. `python -m app.backend.cli.pki status` prints the CA, the CRL, the signer and the certificate counts.
- Permissions `certificate.view`, `certificate.create`, `certificate.revoke` (scoped) and `certificate.export`
  (system: only at the root). The built-in `admin` role holds all four; the base of `post_admin` gains view, create
  and revoke; no other type's ceiling allows them.
- Certificate routes of one user (tag `USERS`). Every one looks the user up as `GET /users/{user_id}` does (404
  `user_not_found`, 403 `user_self_only`, an archived user only with `user.archive` covering them), then needs
  `certificate.view` covering the user (403 `permission_denied`, the user themselves included); a change also needs
  its permission covering the user and every assignment of the user grantable by the caller (403
  `target_user_more_privileged`). Scope answers before existence and state.
  - `GET /users/{user_id}/certificates` (`certificate.view`): the user's certificates, filter `state`.
  - `POST /users/{user_id}/certificates` (`certificate.view` + `certificate.create`): issue, body `{label,
    validity_days?}` (default `settings.pki.validity_days`); 201 with `{certificate, p12_base64, password,
    filename}` and `Cache-Control: no-store`. The key is generated by the API, the certificate signed by the
    signer; the package is stored in `<store_dir>/<user_id>/<certificate_id>/`. An archived user answers 404
    `user_not_found`; 503 `certificate_authority_not_configured` without PKI, `certificate_authority_unavailable`
    when the signer does not answer (nothing is issued).
  - `POST /users/{user_id}/certificates/upload` (`certificate.view` + `certificate.create`): register a file,
    multipart `file` + `label`; 201 with the record (`uploaded`, `ca_issued` when signed by the client CA). The
    same certificate for any user answers 400 `certificate_taken`; `certificate_file_empty`,
    `certificate_file_too_large`, `certificate_invalid` as for `POST /users/`. Works without PKI.
  - `GET /users/{user_id}/certificates/{certificate_id}` (`certificate.view`): one certificate; another user's id
    answers 404 `certificate_not_found`.
  - `POST /users/{user_id}/certificates/{certificate_id}/export` (`certificate.view` + `certificate.export`): the
    stored package again (same `.p12` and password), `Cache-Control: no-store`; counts `export_count` and sets
    `last_exported_at`. 409 `certificate_not_exportable` (not issued here), 409 `certificate_not_active` (revoked
    or expired), 410 `certificate_files_missing` (folder removed), 503 `certificate_authority_not_configured`.
- `GET /certificates/` (`certificate.view`, tag `CERTIFICATES`): the certificates of non-archived users whose home
  post is where both `certificate.view` and `user.view` are in effect (users without a home post: both at the
  root); filters `state`, `source`, `user_id`, `post_id` (403 where the two are not in effect), `expires_within_days`
  (0..3650: not yet expired and expiring within the days); sort `id`, `label`, `not_after`, `created_at`; `page`,
  `page_size`.
- A certificate record (`CertificateResponse`) carries every column of `user_certificates` but the hashes and
  `files_dir`, plus `state` and `has_package`.
- Problems `certificate_not_found` (404), `certificate_authority_unavailable` (503), `certificate_not_active` (409),
  `certificate_not_exportable` (409), `certificate_files_missing` (410); field code `validity_days_too_long`
  (param `max_days`: the limit is `settings.pki.max_validity_days`).
- Audit rows `resource_type` `user_certificate`, actions `issue`, `upload`, `export`: label, serial,
  `sha256_fingerprint`, `not_after` (never a key, a `.p12` or a password; `export` also `export_count`).
- `python -m app.backend.cli.pki issue --username U --label L [--days N] --out DIR`: issues for an existing user
  exactly like the API (folder, audit row with `via: cli`, `created_by` null) and writes `<label>.p12` and
  `password.txt` into `DIR` (a new or empty directory).

### Changed

- `GET /auth/me` has `certificate`: `id`, `label`, `not_after`, `state` of the certificate the session was opened
  with; null for a session bound to none (development login).

- The login matches the three hashes against `user_certificates` (`legacy` rows exactly as the `users` columns
  did). A revoked certificate answers `certificate_revoked`, an expired one `certificate_not_recognized`, then the
  account checks follow as before.
- `POST /users/`: `certificate` is optional (the form stays multipart); without one the user exists but cannot log
  in until a certificate is issued or uploaded. The file becomes an `uploaded` row; an expired file is accepted. Only
  the certificate itself (its fingerprint) is unique: the same certificate registered for any user answers 400
  `certificate_taken`, whose title is now "Certificate already registered"; two certificates may carry the same name
  or serial. The audit row `create` of a user has `certificate_id`.
- The development seed writes the admin's configured (or generated) certificate hashes as a `configured` row.

### Removed

- Columns `users.pki_certificate_cn_hash`, `pki_certificate_cn_hash_scheme`, `pki_certificate_serial_hash`,
  `pki_certificate_fingerprint_hash` and their unique indexes (the hashes live in `user_certificates`).
- `PATCH /users/{user_id}`: the `certificate` form field (now ignored like any unknown field) and the problems
  `certificate_file_empty`, `certificate_invalid`, `certificate_taken` and `certificate_file_too_large` of that
  operation. Replacing a certificate is issuing or uploading a new one and revoking the old one. The session revoke
  cause `certificate_changed` is no longer written (it stays in the enum for existing rows), and the audit rows
  `update` of a user no longer have `certificate_replaced`.

### Migration guide for the web client

Regenerate the types from `api-contract/v1/openapi.json`. The user edit form loses its certificate field; the
create form keeps it, optional, next to `certificate_label`. New translations: `certificate_revoked`,
`certificate_label_invalid` and the revoke cause `certificate_revoked`; the text of `certificate_taken` reads
"Certificate already registered". The certificate pages add `certificate_not_found`,
`certificate_authority_unavailable`, `certificate_not_active`, `certificate_not_exportable`,
`certificate_files_missing` and `validity_days_too_long`, and the permission names `certificate.*`.

## Authorization v2

### Added

- Scope groups: nested groups of posts (migration `021_scope_groups`, table `scope_groups`, column `posts.group_id`).
  `GET/POST /scope-groups/`, `GET /scope-groups/tree`, `GET/PATCH /scope-groups/{group_id}`,
  `POST /scope-groups/{group_id}/archive` and `/restore`. Names are unique within a parent (archived groups included),
  groups nest at most 6 levels deep, a group cannot move into its own subtree, only an empty group can be archived.
  Errors: `scope_group_not_found`, `scope_group_not_archived`, `scope_group_not_empty`, `scope_group_cycle`,
  `scope_group_depth_exceeded` (`max_depth`), `scope_group_name_taken`.
- Permissions `scope_group.view`, `scope_group.create`, `scope_group.update`, `scope_group.archive`.
- `group_id` on `PostCreate`, `PostUpdate` and `PostResponse` (null: directly under the root); an unknown or archived
  group answers 404 `scope_group_not_found`.
- User types and built-in roles (migration `022_builtin_roles`, column `roles.builtin_key`, unique): one code-owned role
  per type (`viewer`, `operator`, `senior_operator`, `analyst`, `post_admin`, `admin`), created and restored at every
  start with exactly the base permissions of its type. A custom role already named like one of these keys (except
  `admin`) is renamed to `<key>_custom` by the migration.
- `GET /user-types/`: the catalog (`key`, `display_name`, `description`, `base_role`, `base_permissions`,
  `allowed_permissions` = the ceiling of the type). Nothing authorizes by type yet.
- Permission levels: `level` (`system` or `scoped`) on `RoleResponse` (system when the role holds a system permission)
  and on `PermissionResponse`. `RoleResponse` also has `is_builtin` and `builtin_key`; the role list and
  `/search/roles` take `is_builtin`.
- `POST /roles/{role_id}/clone` (`role.view` + `role.create`): a custom copy of a role with its permissions
  (403 `grant_exceeds_own_permissions` when the caller lacks one); audit `create` with `cloned_from`.
- Permission `media.monitor` (system level): `GET /media/statistics`, `GET /media/posters/status` and
  `POST /media/posters/reconcile`.
- Role assignments (migration `023_authorization_v2`, table `role_assignments`): a user holds a role at one node of
  the scope tree, the root, a scope group or a post (`scope_group_id`, `scope_post_id`: both null at the root, never
  both set; `scope_key` `root` / `g:<id>` / `p:<id>`, unique per user and role; `created_at`, `created_by`). They are
  the only source of access: an assignment applies to its node and everything below it, archived roles grant
  nothing. The home post (`users.post_id`) is membership only (who manages the user).
- `users.user_type` (NOT NULL): one of the types of `GET /user-types/`.
- Migration `023_authorization_v2` converts every user with the frozen conversion of spec section 8
  (`app/backend/migrations/support/authz_v2.py`): the inferred `user_type`, assignments at the root and at the posts
  of the home post and the access rows (derived roles `<role>_system`, `<role>_posts`, `<role>_view` where a role's
  permissions split), permissions that did nothing left out. One audit row per user (`action` `migrate`,
  `resource_type` `user`, `details`: type, roles and posts before, assignments after, dropped permissions,
  differences). `user_roles` and `user_post_access` are kept as `user_roles_v1` and `user_post_access_v1`; what the
  conversion removed and created is recorded in `authz_v2_migration_log`, so the downgrade restores the legacy rows,
  the removed permissions and their grants exactly and removes the derived roles. A permission the conversion needs
  and the database lacks (`media.monitor` before the first start of this version) is inserted by it. The log's
  `converted` row, written with the converted rows, marks the data as converted: a rerun after an interrupted upgrade
  (MySQL commits the conversion with the first table rename) never converts again and finishes the renames.
- Exact downgrade to 020 also after the new version has started (seeding ran). `022_builtin_roles` records in a
  table of its own, `builtin_roles_migration_log` (`kind`, `ref_id`, JSON `data`), every rename to `<key>_custom`,
  the texts of the `admin` role it marks and the display texts of every permission; its downgrade deletes the
  built-in roles seeding created (all but `admin`, which existed before on an upgraded database) with their grants, gives every renamed role
  its old name back, restores the texts seeding refreshed, deletes `media.monitor` with its grants and drops the
  column and the log. A built-in role a legacy `user_roles` row refers to is kept, and a role whose old name another
  role holds keeps `<key>_custom`; the output says so. `021_scope_groups`'s downgrade deletes `scope_group.*` with
  their grants. A downgrade and upgrade again no longer leave `<key>_custom` copies of the built-in roles behind.
  Names compare without case, as `roles.name` does on MySQL and MariaDB: a legacy "Viewer" is renamed too (the log
  keeps the stored name and the downgrade gives it back), and the suffix is chosen so that no name differs from a
  taken one only by case (before, "operator" next to "Operator_custom" made the rename fail with a duplicate key).
  Changes made through the API after the upgrade are not undone by a downgrade (DEPLOYMENT.md).
- `python -m app.backend.cli.authz_report [--json] [--url URL]`: dry run of the conversion on a database at
  `022_builtin_roles` (run it on a copy of production before the upgrade; read-only): the plan of every user, as the
  migration's audit rows will record it, and the derived roles. A database at 023 or later answers
  `already converted`; one before 022 is refused (exit code 2). Its second line names the administrators after the
  conversion, the active, non-archived users who will hold the built-in `admin` role at the root (`root_admins` in
  `--json`), and a `WARNING` line follows when there are none (recovery: `cli/init_user` with `--user-type admin`,
  DEPLOYMENT.md).
- Role assignments API. `GET /users/{user_id}/assignments` (`user.view` covering the user, 403 `user_self_only`;
  an archived user only with `user.archive` covering them, 404 `user_not_found` otherwise): every assignment of the
  user by id, assignments of archived roles included, as `AssignmentResponse` (`id`, `role` {`id`, `name`,
  `display_name`, `is_builtin`, `is_deleted` (an archived role: the assignment grants nothing)}, `scope` {`kind` `root` / `group` / `post`, `id` (null at the root), `name` (the
  application name at the root)}, `created_at`).
  `POST /users/{user_id}/assignments` (`user.view` + `user.update`, 201, body `AssignmentCreate` {`role_id`,
  `scope`: {`kind`, `id`}}; `id` is required for `group` / `post` and refused for `root`: 422 field codes
  `scope_id_required`, `scope_id_not_allowed`). Checked in this order: `user.update` covering the user and every
  assignment of the user grantable by the caller (403 `target_user_more_privileged`); an archived user 404
  `user_not_found`; an unknown or archived role 404 `role_not_found`; `user.update` and every permission of the role
  held by the caller at the node (403 `grant_exceeds_own_permissions`, `missing` lists both: a group or post outside
  the caller's scope answers this whether it exists, is archived or is live, for a role without permissions too); an unknown or archived group or post 404 `scope_group_not_found` /
  `post_not_found`; a role holding a system permission below the root 409 `role_requires_root_scope`
  (`system_permissions`); a role above the ceiling of the user's type 409 `role_exceeds_user_type` (`user_type`,
  `excess_permissions`); the same role at the same node 409 `assignment_exists`.
  `DELETE /users/{user_id}/assignments/{assignment_id}` (`user.view` + `user.update`, 204): the same user checks,
  then an assignment that is not this user's 404 `assignment_not_found`, `user.update` and every permission of its
  role held by the caller at its node (403 `grant_exceeds_own_permissions`), the user's last assignment 409
  `last_assignment_required` (an assignment of a live role goes only while another one of a live role remains, as
  an assignment of an archived role grants nothing; one of an archived role while any other remains), and for the built-in `admin` role at the root 409 `cannot_remove_own_admin` (the
  caller's own) / `last_admin_required` (no active administrator would remain).
  Audit rows `create` / `delete` with `resource_type` `role_assignment`; the delete row keeps a full copy of the
  removed row (every column) in `old_values`.
- Problem types `assignment_not_found`, `assignment_exists`, `last_assignment_required`, `role_requires_root_scope`,
  `role_exceeds_user_type`; field codes `scope_id_required`, `scope_id_not_allowed`.
- Type change: `PATCH /users/{user_id}` takes `user_type` (form field). Every assignment of the old type's built-in
  role gets the new type's built-in role at the same node (same assignment id); at the root when the new role holds
  system permissions (`admin`), as the default assignment of a new user; when the user already holds the new role at
  that node the old row is removed (merged). A user without an assignment of the old built-in role keeps their
  assignments. Checked in this order, nothing changes on a refusal: `user.update` covering the user and every
  assignment of the user grantable by the caller (403 `target_user_more_privileged`); `user.update` and every
  permission of the new built-in role held by the caller at each node it lands on and at the node a new user of the
  type would get it (the home post; the root without one or for a system role), also when nothing is replaced: the bar
  of `POST /users/` (403 `grant_exceeds_own_permissions`); every assignment of the user, those of archived roles
  included, within the ceiling of the new type (409 `user_type_ceiling_exceeded` with `user_type` and `assignments`:
  [{`id`, `role` {`id`, `name`, `display_name`}, `scope` {`kind`, `id`, `name`}, `excess_permissions`}]); then the
  built-in `admin` role at the root stays with an active user and is not taken from oneself (409 `last_admin_required`
  / `cannot_remove_own_admin`). Every session of the user ends (new `SessionRevokeCause` `user_type_changed`), so the
  web client never shows the pages of the old type. The same type again changes nothing. A concurrent assignment of
  the new built-in role at the same node answers 409 `assignment_exists` (the unique index; before it answered 500),
  and a username or certificate taken concurrently answers `username_taken` / `certificate_taken` also with a type
  change in the request (before: 500). The audit row (`update`, `user`), written in the same transaction as the
  change, carries `replaced_assignments`: per replaced assignment `before` (every column) and `after` (every column)
  or `merged_into` (the id of the assignment it was merged into).
- `UserResponse.user_type`; `GET /users/` and `GET /search/users` filter by `user_type` (422 for an unknown type).
- Auth fields for pages and buttons: `/auth/me`, the login and the refresh response carry `user_type` (pages),
  `permissions` (held at some node: the menu), `assignments` (as `GET /users/{user_id}/assignments` lists them,
  assignments of archived roles included with `role.is_deleted`), `root_permissions` (held at the root: in effect at
  every post) and `post_permissions` (`{"7": [...]}`: per post the permissions that assignments at groups and posts
  put in effect there, for every post they reach; an assignment on an archived post stops applying there, a group's
  keep applying to its archived posts). The permissions in effect at a post are `root_permissions` plus its entry.
  `post_id` and these fields are always present (required in the schema).
- `POST /auth/check-permission` takes an optional `post_id` (positive integer): `allowed` when the permissions are in
  effect at that post (without it: held at some node, as before).
- Problem types `user_type_ceiling_exceeded` and `role_holders_exceed_user_type`. The `user_type` param of these
  and of `role_exceeds_user_type` (and of each entry of `users`) is typed as the `UserType` enum in the schemas.

### Changed

- Built-in roles are not renamable, not archivable, not deletable and their permissions are fixed (409). The four
  `admin_role_*` codes are renamed: `builtin_role_not_renamable`, `builtin_role_permissions_fixed`,
  `builtin_role_not_archivable`, `builtin_role_not_deletable` (one message each, now for every built-in role).
- `002_sync_schema` creates a table owned by a later revision first when the table it creates refers to it
  (`posts.group_id` -> `scope_groups`), so a fresh MySQL install gets the foreign key.
- Permission at the post. Every route requirement (`require(all=..., any=...)`, `POST /auth/check-permission`,
  `GET /auth/route-access`) and the `include_deleted` check now count a permission only when it is held at some node
  of the scope tree (the role assignments; migration 023 converts the roles, home posts and access rows). A
  permission that did nothing without `admin.true` (`post.create/update/archive`, `user.create/update/archive`, `role.*`, dictionary writes,
  `media.update`, `export.create`, `audit.view`, `scope_group.*`) no longer counts for a role without `admin.true`:
  such a user gets 403 `permission_denied` (with `missing`) instead of `admin_required`, and `include_deleted` with
  such an `X.archive` answers 403 `permission_required_for_action`. A user with no home post and no access rows holds
  no post-scoped permission (`post.view`, `observation.*`, `media.*`, `user.view`, ...): routes needing one answer
  403 `permission_denied` instead of `no_post_assigned` or an empty list. `no_post_assigned` now means: the
  permission is held, but applies at no post (for example it is held only at a scope group without posts).
- A permission whose only grants are on archived posts or at archived scope groups is held nowhere (spec section 5:
  such assignments do not apply): it is left out of `permissions` (`/auth/me`, the login and the refresh response),
  does not pass a route requirement (403 `permission_denied` with `missing`; before: `no_post_assigned`, or a list
  or statistics answered empty), `POST /auth/check-permission` and `GET /auth/route-access`, and does not count for
  role edits. A user whose only post is archived therefore also loses the reads without a post (dictionaries, media
  formats, roles) that the grants of that post gave. The assignments are kept and apply again when the post or the
  group is restored.
- The admin gate is removed from `POST /posts/`, `PUT /posts/{post_id}`, `POST /posts/{post_id}/archive` and
  `/restore` (403 `admin_required` is no longer declared there). Each checks its permission at the post:
  `post.update` / `post.archive` in effect at the post (403 `post_write_denied`, `no_post_assigned`); creating a post
  needs `post.create` at its group or above (at the root for a post without a group), moving one (`group_id`)
  `post.update` covering both the old and the new location (a grant on the post itself covers neither): otherwise
  403 `permission_required_for_action` (`action` `create_post` /
  `move_post`). Restoring an archived post needs `post.archive` inherited from a group or the root (a grant on the
  post itself stops applying while it is archived). Administrators keep all of this: `admin.true` with these
  permissions holds them at the root.
- Post records follow `post.view` (`GET /posts/`, `GET /search/posts`, `GET /posts/{post_id}`: 403
  `post_access_denied` now with `required` and `missing`), user records `user.view` (`GET /users/`,
  `GET /search/users`, `GET /users/{user_id}`); users without a home post are listed and shown only with `user.view`
  at the root. The caller is always in their own user list (before: only when their home post was in scope).
- Archived records: an archived post or user is shown only to a holder of `post.archive` / `user.archive` in effect
  at it (before: also to any `admin.true` holder), and `include_deleted` lists archived posts and users only where
  `X.archive` is in effect.
- `GET /posts/{post_id}/observation-stats` follows `observation.view` at the post (403 with `missing`
  `["observation.view"]`): an `admin.true` holder no longer counts posts outside their data scope; holders of the
  built-in `admin` role (or `post.access_all`) still count every post.
- `GET /statistics/` covers the posts where `statistics.view` is in effect (`admin.true` holders: every post, as
  before).
- `GET /media/uploads`: other users' sessions only at the posts where `media.manage_any` is in effect (`admin.true`
  no longer widens the list to every post); the caller's own sessions are always listed.
- The admin gate is removed from every route: a route requires only its listed permissions, held at some node (403
  `permission_denied` with `required` and `missing`). It was on the dictionary writes (`/categories`, `/signal-types`,
  `/tags`: create, update, archive, restore), the `/media-formats` writes, every `/roles` route and `GET /search/roles`,
  every `/scope-groups` route, the user writes (`POST /users/`, `PATCH /users/{user_id}`, post access, archive,
  restore, ending sessions), `GET /users/{user_id}/roles`, `/post-access` and `/sessions`, `POST /observations/report`,
  `GET /media/statistics`, `GET /media/posters/status`, `POST /media/posters/reconcile` and
  `POST /media/{media_id}/thumbnail/regenerate`. A permission that did nothing without `admin.true` is held nowhere,
  and `admin.true` alone passes no route. One route opens to users who could not call it before:
  `GET /users/{user_id}/sessions` needs `user.view` covering the user (before: administrators only, behind the gate),
  and `user.view` was live without `admin.true`, so a holder of `user.view` at a post now sees the sessions of that
  post's users. The new `GET /users/{user_id}/assignments` follows the same rule (the routes it replaces,
  `/users/{user_id}/roles` and `/post-access`, were administrators only).
- `POST /observations/report` needs `observation.view` + `export.create` and covers the posts where both are in effect
  (before: every readable post, behind the admin gate). A `post_ids` entry outside them answers 403
  (`post_access_denied`, or `post_write_denied` with `missing` `["export.create"]`). With `include_deleted`, archived
  bands and observations are included only where `observation.archive` is in effect.
- `GET /media/statistics` and `GET /media/posters/status` need `media.monitor` (before: `media.view` behind the admin
  gate; administrators who held `media.view` hold `media.monitor` at the root). `POST /media/posters/reconcile` acts
  on every post: it needs `media.view` + `media.update` + `media.monitor` (before: `media.view` + `media.update`
  behind the admin gate), so `media.update` held at a group or post is not enough.
- Observations and media follow the permission at the post. Lists and searches (`GET /observations/list-all`,
  `GET /search/observations`, `GET /media/`, `GET /search/media`) cover the posts where `observation.view` /
  `media.view` is in effect; with `include_deleted`, archived rows only where `observation.archive` / `media.archive`
  is in effect (before: archived rows of every readable post, read-only access rows included); the band list of one
  observation likewise. A change checks `X.view` at the post, then the permission of the change there: create
  `observation.create`; update, disappeared, band add and band update `observation.update`; archive, restore and the
  band archive / restore `observation.archive`; media archive / restore `media.archive`; thumbnail regenerate
  `media.update`; uploads and upload sessions `media.create` (before: write access to the post, which any of these
  permissions gave). 403 `post_access_denied` without `X.view` there, `post_write_denied` naming the permission
  otherwise; both now carry `required` and `missing`. Media info, download, stream, thumbnail, storyboard, share and
  the media of an observation check `media.view` at the post.
- `observation.manage_any` and `media.manage_any` apply at the posts where they are in effect (before: wherever the
  caller could write); another user's upload session needs `media.manage_any` at its post.
- Archiving or restoring an observation of an archived post needs `post.archive` in effect at the post (before:
  `admin.true` with `post.access_all`); everyone else gets 409 `post_archived`.
- `GET /users/{user_id}/sessions`, `/roles` and `/post-access` need `user.view` covering the user (the caller, or
  `user.view` in effect at the user's home post, at the root for a user without one), otherwise 403 `user_self_only`.
  The sessions of an archived user, like `GET /users/{user_id}`, only with `user.archive` covering them (404
  `user_not_found` otherwise).
- Regenerating the thumbnail of a media file without an observation needs `media.update` at the root (403
  `permission_required_for_action`).
- `POST /users/` takes a required `user_type` (form field) and gives the new user one assignment: the built-in role of
  the type at the home post, or at the root without a home post or when the role holds system permissions (`admin`).
  The caller needs `user.create` covering that node (403 `permission_required_for_action`, `action` `create_user`)
  and every permission of the role there (403 `grant_exceeds_own_permissions`). An unknown type answers 422
  (`#/user_type`, `enum`). The audit row records `user_type` and `assignments` instead of `role_ids`.
- Changing a user (`PATCH /users/{user_id}`, `POST /users/{user_id}/sessions/end` and
  `/sessions/{session_id}/end` with `user.update`; `POST /users/{user_id}/archive` and `/restore` with
  `user.archive`) needs that permission covering the user (their home post, the root for a user without one) and,
  for another user, every assignment of the user grantable by the caller at its node, assignments of archived roles
  included (they apply again when the role is restored); otherwise 403 `target_user_more_privileged` with `missing`.
  Moving the home post (`post_id`) needs `user.update` covering the new post too (403
  `permission_required_for_action`, `action` `move_user`); a move no longer touches access rows.
- Check order of `POST /users/` and of a move in `PATCH /users/{user_id}`: the scope checks answer before the post is
  looked up, so a `post_id` outside the caller's scope answers the same 403 `permission_required_for_action`
  whether the post is unknown, archived or live (before: 404 `post_not_found` / 409 `post_archived`, which told an
  outsider whether an id exists and whether it is archived). 404 and 409 now answer only for a post inside the
  caller's scope.
- The last administrator is an active, non-archived user holding the built-in `admin` role at the root:
  deactivating or archiving them answers 409 `last_admin_required`. Role edits can no longer take it away (the
  built-in role is immutable), so `PATCH /roles/{role_id}`, `POST /roles/{role_id}/archive` and
  `DELETE /roles/{role_id}/permissions/{permission_id}` no longer declare `last_admin_required` /
  `cannot_remove_own_admin`.
- `DELETE /roles/{role_id}` removes the role's assignments (the holders are read from `role_assignments`); the audit
  copy lists them under `assignments`, each with every column of the removed row (`id`, `user_id`, `role_id`,
  `scope_group_id`, `scope_post_id`, `scope_key`, `created_at`, `created_by`). It also removes the role's rows of `user_roles_v1` (the
  legacy holders 023 keeps, with their foreign key to `roles.id`, until a cleanup revision drops the table): on MySQL
  the permanent delete of an archived legacy role with such rows answered 500. The audit copy lists them under
  `user_roles_v1` with every column (empty without the table). Such a role and its legacy rows do not come back with
  a downgrade.
- `DELETE /roles/{role_id}` answers 409 `last_assignment_required` when an archived holder's only assignment is of
  the role (a restore would bring the user back without any assignment); nothing is removed. Give the user another
  assignment first. Answered after `role_has_active_users`.
- `/auth/me` and the login response: `permissions` are the permissions held at some node (see Added for the other
  auth fields).
- Role edits are bounded by the role's holders: permissions added by `PATCH /roles/{role_id}` (`permission_ids`) or
  `PUT /roles/{role_id}/permissions/{permission_id}` apply at once to every assignment of the role, those of archived
  users included (restoring a user gives the role back as it is). A system permission needs every assignment at the
  root (409 `role_requires_root_scope`, `system_permissions`), and the type of every holder must allow the added
  permissions (409 `role_holders_exceed_user_type`, `users`: [{`id`, `username`, `user_type`,
  `excess_permissions`}], each holder once, archived holders listed too); answered after the 403
  `grant_exceeds_own_permissions`, the role stays unchanged.
- Role edits are bounded by where the caller holds the permissions: a permission added to a role (`PATCH
  /roles/{role_id}` `permission_ids`, `PUT /roles/{role_id}/permissions/{permission_id}`) must be held by the caller
  at every node the role is assigned at, assignments of archived users included, and `POST /roles/{role_id}/restore`
  needs every permission of the role at each of these nodes (a role assigned nowhere: held at some node, as before).
  Before, a permission held at one post could be added to a role held at the root or at another group, which put it
  in effect there. 403 `grant_exceeds_own_permissions` (`missing`: the permissions lacking at any of these nodes),
  answered before the 409s, nothing changed. Removing permissions and archiving a role still need them held at some
  node. The descriptions of both edits in the OpenAPI document state the rule (they said archived holders did not
  count).
- `PATCH /users/{user_id}`: the check of the caller's reach (403 `target_user_more_privileged`) answers before the
  archived state of the user (404 `user_not_found`), so an outsider gets the same 403 for an archived and a live user.
  The same order now holds for `POST /users/{user_id}/archive`, `/sessions/end` and `/sessions/{session_id}/end`
  (404 `user_not_found` for an archived user only inside the caller's reach) and `POST /users/{user_id}/restore` (409
  `user_not_archived` only inside it); before, they answered the state first.
- `RoleResponse.builtin_key`, `is_builtin`, `level` and `PermissionResponse.level` are required in the schema (they
  were always present; openapi-typescript typed them as optional).
- The permission catalog no longer has `admin.true` and `post.access_all` (migration 023 removes the rows and their
  grants; `POST /auth/check-permission` with them answers 422 like any unknown permission).
- `cli/init_user`: `--user-type` (the base role of the type at the root) instead of `--role-ids`, `--list-types`
  instead of `--list-roles`. The development admin seeded at start is of type `admin` with the `admin` role at the
  root.
- Scope groups follow `scope_group.view` at its node. `GET /scope-groups/` lists the groups inside the subtrees where
  the caller holds it (every group with it at the root); `GET /scope-groups/{group_id}` answers 404
  `scope_group_not_found` for a group outside them, like an unknown id, and so do `PATCH`, `/archive` and `/restore`
  of such a group and a `parent_id` outside them on create or move. `GET /scope-groups/tree` shows the subtrees
  where the caller holds `scope_group.view`, with only the posts where `post.view` is in effect: its top level is
  the topmost groups the caller sees (the groups directly under the root with `scope_group.view` at the root) and,
  in `posts`, the posts directly under the root (with `scope_group.view` at the root) and the posts where it is held
  on the post itself. A post or group below an archived group is left out (a post restored while its group is
  archived was shown as a post of the root).
- `group_id` in the path of `GET` / `PATCH /scope-groups/{group_id}`, `/archive` and `/restore` is a positive
  integer (`minimum` 1 in the schema): 0 or a negative id answers 422 `validation_error` (`greater_than_equal`,
  `parameter` `group_id`) instead of 500 `internal_error`.
- Moving a scope group (`PATCH /scope-groups/{group_id}` with a `parent_id` other than the current one) takes every
  post below it along, so it also needs `post.update` covering the old and the new parent location (the group or
  above it, the root for a group directly under it), like a post move: otherwise 403
  `permission_required_for_action` (`action` `move_scope_group`, `permission` `post.update`), answered before the
  new parent's existence and the tree rules (cycle, depth, name), nothing moved. Before, `scope_group.update` alone
  moved posts into the reach of the caller's group assignments.
- `POST /posts/{post_id}/restore` of a post whose group is archived answers 404 `scope_group_not_found` (restore the
  group first), like a group restored under an archived parent; before, the post came back live inside an archived
  group. The post scope (403) and the post state (404, 409) answer first.
- `PATCH /users/{user_id}`: a deactivation answers only `last_admin_required` (deactivating oneself is refused with
  `cannot_deactivate_self` before); `cannot_remove_own_admin` is declared for the type change.
- Texts of the OpenAPI document that still described the admin gate: `include_deleted` of the post, user,
  observation and media lists (and the description of `GET /search/posts`) states the rule (`X.archive` held
  anywhere; archived rows only where it is in effect); `GET /roles/list-all-permissions` and `GET /roles/{role_id}`
  no longer say "admin only" / "administration area"; the title of `poster_failed` reads "Poster extraction failed;
  a holder of media.update can regenerate it" (the code and params are unchanged).

### Removed

- `is_admin`, `accessible_post_ids` and `roles` of `/auth/me` and the login and refresh responses, with the schema
  `RoleBriefResponse`: pages follow `user_type`, buttons `root_permissions` plus `post_permissions`, the roles are in
  `assignments`. Nothing in the API answers who is "the administrator" by a flag.
- 403 `admin_required`: the problem type and every declaration of it.
- The admin flag of a requirement: `required` in 403 bodies and in `GET /auth/route-access` entries is `{all, any}`;
  `POST /auth/check-permission` no longer takes `admin` and no longer answers `admin_required`.
- `GET /users/{user_id}/roles`, `GET /users/{user_id}/post-access` and `PUT` / `DELETE
  /users/{user_id}/post-access/{post_id}` (role assignments replace roles and access rows; their API follows), with
  the schemas `UserRoleResponse`, `UserPostAccessResponse` and `UserPostAccessUpsert`.
- `role_ids` of the user forms (`UserCreateForm`, `UserUpdateForm`) and its field code `role_ids_duplicate`.
- Problem types `post_access_home_post`, `post_access_not_found` and `role_archived_not_assignable`.
- Tables `user_roles` and `user_post_access` (kept as `*_v1` by migration 023 until a later cleanup revision).

### Migration guide for the web client

Regenerate the types from `api-contract/v1/openapi.json` (`WEB_TOOLING.md`); `tsc` then points at every use of a
removed field or code. What replaces each (API.md, "Authorization: pages, buttons, scope", has the details):

| Removed | Use instead |
|---------|-------------|
| `is_admin` (login, refresh, `/auth/me`) | Pages by `user_type` (the catalog: `GET /user-types/`), buttons by permissions: `permissions` (held at some node) for the menu, `root_permissions` + `post_permissions` at a post. The administration pages belong to the `admin` type; no flag says "administrator". |
| `accessible_post_ids` | `root_permissions` (in effect at every post) + `post_permissions` (`{"7": [...]}`): the permissions in effect at post 7 are `root_permissions` + `post_permissions["7"]`. Lists are already limited by the API; a post picker uses `GET /posts/` or `GET /scope-groups/tree`. |
| `roles` (auth responses, schema `RoleBriefResponse`) | `assignments`: each a role (`id`, `name`, `display_name`, `is_builtin`, `is_deleted`) at a node (`scope`: `kind` root / group / post, `id`, `name`). |
| `GET /users/{user_id}/roles` | `GET /users/{user_id}/assignments` |
| `GET /users/{user_id}/post-access`, `PUT` / `DELETE /users/{user_id}/post-access/{post_id}` | `GET` / `POST /users/{user_id}/assignments`, `DELETE /users/{user_id}/assignments/{assignment_id}`: a role at a post (or a group, or the root). A read-only access row is an assignment of a role holding only `*.view` permissions; the home post (`post_id`) grants nothing by itself. |
| `role_ids` in the user forms (`POST /users/`, `PATCH /users/{user_id}`) | `user_type` (required on create: the type's built-in role is assigned at the home post, at the root without one or for `admin`; on `PATCH` it changes the type), then the assignment routes for any other role. |
| `admin` in `POST /auth/check-permission`, the admin flag of `required` (403 bodies, `GET /auth/route-access`) | `post_id` in `check-permission` (in effect at that post); `required` is `{all, any}`. |
| 403 `admin_required` | 403 `permission_denied` (with `required` and `missing`). |
| `admin_role_not_renamable`, `admin_role_permissions_fixed`, `admin_role_not_archivable`, `admin_role_not_deletable` | `builtin_role_not_renamable`, `builtin_role_permissions_fixed`, `builtin_role_not_archivable`, `builtin_role_not_deletable` (every built-in role; clone one to edit it). |
| `post_access_home_post`, `post_access_not_found` | The assignment codes: `assignment_not_found`, `assignment_exists`, `last_assignment_required`, `role_requires_root_scope`, `role_exceeds_user_type`. |
| `role_archived_not_assignable` | Assigning an archived role answers 404 `role_not_found`. |
| Field code `role_ids_duplicate` | Gone with `role_ids`; the assignment body has `scope_id_required` and `scope_id_not_allowed`. |
| Permissions `admin.true`, `post.access_all` | Administration is the granular permissions held at the root (the built-in `admin` role at `/`); access to every post is an assignment at the root. `check-permission` with them answers 422. |

New translations: every code of API.md, "Problem codes of authorization and scope" (`assignment_*`,
`last_assignment_required`, `role_requires_root_scope`, `role_exceeds_user_type`, `user_type_ceiling_exceeded`,
`role_holders_exceed_user_type`, `builtin_role_*`, `scope_group_*`) and the field codes `scope_id_required`,
`scope_id_not_allowed`. Drop the removed ones above.

New in the forms: `user_type` on user create and edit, `group_id` on post create and edit, the scope groups pages
(`/scope-groups`), the assignments of a user, `POST /roles/{role_id}/clone`. A type change ends the user's sessions
(`revoke_cause` `user_type_changed`).

## Post archiving and certificate name hash

### Fixed

- Two certificates whose names differ only by case, dashes or colons (`Post-Kyiv` and `postkyiv`) are two different
  certificates: `POST /users`, the certificate replace in `PATCH /users/{id}` and `cli/init_user` no longer refuse the
  second one with `certificate_taken`. The name hash is now computed on the name as written (Unicode NFC, surrounding
  whitespace stripped); serial and fingerprint hashing do not change. A column `users.pki_certificate_cn_hash_scheme`
  says which rule made a stored hash (1 = the old rule, 2 = the new one); login and the duplicate checks compare each
  row under its own scheme. A new certificate is still refused when its name collides with a certificate stored before
  the change (scheme 1, by design: the old hash cannot be recomputed). Uploading the same certificate again through
  `PATCH /users/{id}` moves that user to the new rule; the fingerprint is unchanged, so their sessions are not ended.
- A certificate subject with several CNs: the login reads the same CN the certificate upload stores (the first in the
  certificate), from nginx's RFC 2253 form and from the legacy `/CN=` form. OpenSSL prints every entry last first,
  the attributes of a multi-valued RDN included (`CN=bbb+CN=aaa` for a certificate that has `aaa` first); the login
  takes the last CN of the string.

### Changed

- Posts are archived like every other entity: `POST /posts/{post_id}/archive` and `POST /posts/{post_id}/restore`
  (administration: `post.view` and `post.archive` plus `admin.true`; `post.archive` was seeded but had no route).
  Archiving an archived or unknown post answers 404 `post_not_found`, restoring a live post 409 `post_not_archived`;
  audit rows `archive` / `restore`. `PUT /posts/{post_id}` answers 404 for an archived post and `is_deleted` is never
  read from a request body. `PostResponse` gained `is_deleted`. `GET /posts/` and `GET /search/posts` hide archived
  posts unless `include_deleted=true` (requires `post.archive`, otherwise 403 `permission_required_for_action`, now
  declared on both); `GET /posts/{post_id}` answers 404 for an archived post unless the caller holds `post.archive` or
  is an administrator.
- Data scope: an archived post leaves the scope of its home users and of its `user_post_access` rows, so every list,
  search, report, statistics, `observation-stats` and single-resource check follows (403 for the data of the post,
  `accessible_post_ids` of login and `GET /auth/me` leaves it out). Only `post.access_all` still reads its data; an
  administrator without `post.access_all` does not (observations, media and reports have no administrator
  exception for them). Aggregates keep the administrator exception of CLAUDE.md: `GET /statistics`,
  `observation-stats` and the `media.manage_any` upload list still cover every post for administrators. Archiving
  changes no user row: home posts and access rows stay and apply again after a restore.
- New problem code `post_archived` (409, param `post_id`): the data of an archived post is read-only. Answered after
  the 403 checks by observation create, update (tags included), mark disappeared, every band change, media upload
  (multipart, nginx, resumable create and complete; a session waits open and completes after a restore, UPLOADS.md
  lists it), media
  archive and restore, and poster regenerate. Observation archive and restore work in an archived post only for a caller
  with `admin.true` AND `post.access_all` (and ownership or `observation.manage_any`, plus the route's
  `observation.view` and `observation.archive`): an archived post is in no data scope except `post.access_all`, so the
  403 checks come first. A `post.access_all` holder without `admin.true` gets 409 `post_archived`; a home-post or
  access-row user gets 403 (`no_post_assigned` / `post_access_denied`). `POST /users/`, `PATCH /users/{user_id}` (moving a
  user onto the post) and `PUT /users/{user_id}/post-access/{post_id}` answer it too. `post_archived` and
  `post_not_archived` are new keys for the web localization package; the routes declare them and
  `api-contract/v1/openapi.json` was rewritten (the two routes, `PostArchivedProblem`, `PostNotArchivedProblem`,
  `PostResponse.is_deleted`, `include_deleted` on the post lists, descriptions).
- `GET /statistics`: `posts.total`, `posts.active` and `posts.online` count live posts only (an archived post is neither
  active nor online); no field changed.
- A resumable upload session that is aborted or expired answers 410 `upload_session_closed` on `complete` also when its
  post is archived (the closed check runs before `post_archived`).

### Changed (operators)

- Migration `019_certificate_cn_hash_scheme` adds `users.pki_certificate_cn_hash_scheme` (small integer, NOT NULL, server
  default 1) and runs at startup. Certificates already in the database stay scheme 1 and keep working; they are not
  upgraded automatically (the name itself is not stored) - a user moves to scheme 2 when their certificate is uploaded
  again through `PATCH /users/{id}`, the same file included (an unchanged fingerprint ends no session). A new
  certificate whose name collides with a scheme 1 row is still refused with `certificate_taken` (by design).
  Every certificate stored from now on, the first admin from the configuration included, is scheme 2. Downgrading drops
  the column: users of scheme 2 cannot log in with the older code until their certificate is replaced. A fresh install
  creates the same SMALLINT column (the model declares it now).
- Migration `020_post_archive` adds `posts.is_deleted` (boolean, NOT NULL, server default false, index
  `ix_posts_is_deleted`) and runs at startup; it archives no post, and a fresh install creates the same column from the
  model. Downgrading drops the column and its index: archived posts become live again.

## API audit fixes

### Fixed

- Archive and restore of media and observations check the post scope and ownership before the state of the row:
  an outsider gets the same 403 whatever the id is (archived, live), and only a caller in scope learns 404
  (archive of an archived row) or 409 (restore of a live one). `POST /observations/create` checks write access to the
  post before its existence: an unknown post id and a foreign one both answer 403 `post_access_denied` (or
  `no_post_assigned`); a caller with `post.access_all` still gets 404 `post_not_found`.
- `GET /users/{id}` answers like `GET /users/` for `post.access_all`: users without a post are visible (it was 403
  `user_self_only`). An archived user answers 404 unless the caller holds `user.archive` or `admin.true`; visibility is
  checked first, so an outsider gets 403 `user_self_only` for an archived user as for a live one.
- `PATCH /users/{id}` with a `post_id` the user already has a `user_post_access` row for deletes that row in the same
  transaction; the audit row lists it as `removed_post_access` in `new_values`.
- `PATCH /roles/{id}` with `permission_ids` and `PATCH /users/{id}` with `role_ids` replace the rows by difference: rows
  that stay are kept, removed ones are deleted, new ones are inserted. A set that kept an existing id used to answer
  500 (roles) or 400 `certificate_taken` (users) because the flush wrote the INSERTs before the DELETEs.
- `PATCH /users/{id}` maps a unique violation to `username_taken` only for `users.username` and to `certificate_taken`
  only for the certificate hash columns; any other `IntegrityError` is no longer reported as a taken certificate.
- Repeated ids in `permission_ids` of `POST /roles/` and `PATCH /roles/{id}` answer 422 with the new field code
  `permission_ids_duplicate` (they answered 500). The `FieldErrorCode` schema in `api-contract/v1/openapi.json` gained
  that value.
- The built-in admin role keeps its name: a new `name` for the role named `admin` in `PATCH /roles/{id}` answers 409
  `admin_role_not_renamable` (the unchanged name and the other fields stay editable). After a rename the role could be
  archived, every administrator lost access and seeding created a second, empty `admin` role.
- The built-in admin role keeps its permissions: `permission_ids` of `PATCH /roles/{id}` that differ from its current
  set, `PUT /roles/{id}/permissions/{permission_id}` of a permission it lacks and `DELETE` of one it holds answer 409
  `admin_role_permissions_fixed`; seeding still grants it every permission at start. A limited administrator could
  remove `admin.true` (or any permission they hold) from the admin role and lock every full administrator out until
  the next restart.
- Nobody grants more than they hold: assigning roles (`POST /users/`, `role_ids` of `PATCH /users/{id}`), creating a
  role with `permission_ids`, adding or removing permissions of a role (`permission_ids` of `PATCH /roles/{id}`,
  `PUT` and `DELETE /roles/{id}/permissions/{permission_id}`), archiving and restoring a role answer 403
  `grant_exceeds_own_permissions` when the change grants or revokes a permission the caller does not hold; `missing`
  lists them. Only what the change adds or removes counts: a permission the caller lacks may stay on a role they edit.
  A holder of `user.update` or `role.update` with `admin.true` could give themselves the admin role or any permission.
- Every write to another user answers 403 `target_user_more_privileged` when that user holds a permission the caller
  lacks (`missing` lists them): `PATCH /users/{id}` (any field), archiving and restoring a user, `PUT` and
  `DELETE /users/{id}/post-access/{post_id}`, `POST /users/{id}/sessions/end` and
  `POST /users/{id}/sessions/{session_id}/end`. A caller acting on their own account is not limited by it. Holders of
  the seeded admin role hold every permission and are not affected. A limited administrator could replace a full
  administrator's certificate, deactivate, archive or log them out.
- An active administrator always remains: a role change or deactivation of a user, archiving a user, archiving a role
  or removing `admin.true` from a role answer 409 `last_admin_required` when no active, non-archived user would hold
  `admin.true` through a role that is not archived, and 409 `cannot_remove_own_admin` when the change takes
  `admin.true` from the caller. The only administrator could remove their own `admin.true`.
- The audit rows of `POST /users/` and `PATCH /users/{id}` record `role_ids` (before and after) and
  `certificate_replaced`; a role change left no trace in the audit values.
- An explicit `null` for a NOT NULL column answers 422 `validation_error` with field code `value_null` (pointer on the
  field) instead of 500 (or a misleading 400 for `media_type`): `PUT /observations/{id}` (`signal_type_id`,
  `observed_at`, `confidence_level`), `PUT /tags/{id}` (`name`), `PUT /categories/{id}` (`name`, `alias`),
  `PUT /signal-types/{id}` (`code`, `name`, `is_active`, `sort_order`), `PUT /media-formats/{id}` (`media_type`,
  `content_type`, `label`, `extensions`, `aliases`, `max_file_size_bytes`, `is_active`, `sort_order`) and
  `PUT /observations/{id}/frequencies/{frequency_id}` (`kind`, `frequency_min`, `frequency_max`, `frequency_center`;
  the pointer used to be the body). Nullable fields keep accepting `null`. One shared validator, `not_null()` in
  `utils/validators.py`.
- `POST /observations/create` without `observed_at` stores the current UTC time (it answered 500). `observed_at` of
  the request is no longer nullable in `api-contract/v1/openapi.json`; an explicit `null` there answers 422.
- Upload file names: the extension is read from the original name and only the stem is reduced to ASCII. A name such
  as `foto.png` written in Cyrillic used to lose its extension and every upload path answered 415
  `media_extension_not_accepted`; it is now stored as `upload_<8 hex>.png` (`file_extension` `.png`). ASCII names are
  stored as before.
- Production login reads the CN from nginx's `X-SSL-Client-DN` (`$ssl_client_s_dn`, OpenSSL RFC 2253 form) as OpenSSL
  writes it: RDNs are split on unescaped `,` (attributes on unescaped `+`) and the value is unescaped (`\XX` bytes
  decoded as UTF-8, `\,` and the other specials as the character). A certificate with a non-ASCII CN, such as a
  Cyrillic name (sent as `CN=\D0\86\D0\B2...`), or a CN with an escaped comma answered 401
  `certificate_not_recognized`. A DN whose escaped bytes are not UTF-8 answers `certificate_data_missing`. The
  legacy `/C=.../CN=...` form is read as before.
- A session bound to a client certificate that is no longer the user's ends on its next request or refresh (401
  `token_certificate_mismatch`; the session is recorded `revoked` with `revoke_cause` `certificate_changed`, audit
  `force_logout`). A login racing a certificate change could store its session after the change had revoked the old
  certificate's sessions, and that session kept working with the old certificate.
- `is_online` and `last_activity_at` of users (`UserResponse`, the nested `user` projections, `/auth/me`) and
  `last_user_activity_at` of posts count live sessions only, the same condition as the `is_online` filter and
  `online_user_count`: a session past its idle timeout or hard end no longer counts before anything records its end.
  A user whose sessions all ended has `last_activity_at` `null`, as after a logout.
- An access token without a session id (`sid`) answers 401 `invalid_token`; it used to skip every session and
  certificate check. The login never issues one.
- `GET /problems/{code}` for an unknown code no longer echoes the submitted value: `problem_type_not_found` has no
  params (the `problem_code` member and the code in `detail` are gone from the body and from its schema in
  `api-contract/v1/openapi.json`).
- Uploads over 2 GiB no longer answer 500 on MySQL: `media_files.file_size` is BIGINT (it was INT, uploads allow 10 GB;
  migration `018_media_file_size_bigint`). `upload_sessions` and `media_formats` already used BIGINT for sizes.
- A long request URL or User-Agent no longer fails the audit INSERT (and with it the request) on strict MySQL:
  `request_url` (500) and `user_agent` (255) of `audit_logs` and `user_agent` of `user_sessions` are cut to their column
  lengths (`fit_column`, in `client_info`, `create_audit_log` and `create_session`).
- `link_group` of the frequency bands and `duration_seconds` of observations accept at most 2147483647 (a MySQL INT);
  a larger value answers 422 `validation_error` (it answered 500). `POST /observations/{id}/disappeared` answers 422
  with field code `value_too_large` on `disappeared_at` when the duration it computes does not fit an INT. The
  `maximum` of these fields is in `api-contract/v1/openapi.json`.
- `GET /search/media` no longer returns archived media, and `GET /media` and `GET /search/media` no longer list media of
  an archived observation (their thumbnail URLs were signed although `GET /media/{id}/info` answers 404), in the items
  and in `total`. Both take `include_deleted` (archived media only, never media of an archived observation), which needs
  `media.archive` (403 `permission_required_for_action`, declared on both operations).
- `GET /media` and `GET /search/media` no longer list media without an observation (orphans): the listing always joins
  the observation now (for its archived state and its post), so such a row is left out of the items and of `total` for
  every caller; before, callers with `post.access_all` (no post scope) got them.
- `GET /media?has_thumbnail=true` (and search) no longer returns images above `storage.imgproxy.max_src_resolution_mp`
  (`thumbnail_status` `unsupported`); `has_thumbnail=false` returns them.
- `POST /media/{id}/thumbnail/regenerate` checks write access to the post of the media like archive and restore: an
  administrator of another post answers 403 `post_access_denied` / `post_write_denied` (`no_post_assigned` for one
  with no post at all) and the poster is left alone; media of an archived observation answers 404
  `media_observation_not_found`. `api-contract/v1/openapi.json` is rewritten for the new codes and the
  `include_deleted` query parameter.
- Every list and search orders by the requested field and then by the primary key (without `sort_by`, by the primary
  key), so pages of a non-unique field no longer repeat or skip rows on MySQL.
- `sort_order=*_nulls_first` / `*_nulls_last` answered 500 on MySQL and MariaDB (`NULLS FIRST/LAST` is not their
  syntax); the order is now emulated with an `IS NULL` key in front of the column and means the same.
- `*_contains` filters match `%`, `_` and `\` literally (`name_contains=%` no longer matches every row).
- `page` is at most 1000000 on every list and search (422 above it); a huge `page` answered 500.
- `POST /tags/create`, `/categories/create` and `/signal-types/create` no longer restore an archived row with the same
  name or code (and without `X.archive`): they answer `tag_name_archived`, `category_name_archived` and
  `signal_type_code_archived`, as rename does; restore stays on `POST /{id}/restore`. Media formats keep their
  restore-through-create.
- `POST /categories/{id}/archive` answers 409 `category_has_signal_types` while the category has non-archived signal
  types; `POST /signal-types/{id}/restore` answers 409 `signal_type_category_archived` while its category is archived;
  `POST /observations/{id}/restore` answers 409 `observation_signal_type_archived` while its signal type is archived.
- `PUT /observations/{id}` skips the existence and active checks of the signal type when `signal_type_id` equals the
  current one, so an observation of a deactivated or archived type can still be edited.
- OpenAPI contract: the four new codes and the archived-name codes on the three create routes are declared
  (`observation_signal_type_archived` on `POST /observations/{id}/restore` only; archive never answers it).
- A datetime with an offset is stored and compared as its UTC instant: `10:00+03:00` is stored as `07:00Z`. The
  drivers drop the offset, so it used to be stored and read as `10:00Z`, and range filters (`*_from` / `*_to` of every
  list and search, the report body), `POST /observations/{id}/disappeared` durations and `GET /statistics` were off by
  the offset (`/posts/{id}/observation-stats`, which converted, disagreed with `/statistics`). A value without an
  offset is UTC, as before. One shared input type, `UtcDatetime` in `utils/validators.py`, for every datetime of a
  request body, query model and query parameter; the filters convert their datetime bounds too. Rows stored before
  this fix from a non-UTC offset keep the shifted time (the offset was never stored).
- Every datetime in a response carries an offset; values read from the database are UTC and written with `Z`
  (observations, users, posts, tags, roles, media and the rest used to come without one, while create responses had
  it), and `Upload-Expires` of the resumable uploads is written with `+00:00`. Bucket times of
  `/posts/{id}/observation-stats` keep the offset of the requested `tz`. One shared response type,
  `ResponseDatetime`. The session times (`GET /auth/sessions`, `GET /users/{id}/sessions`) now end in `Z` instead of
  `+00:00`, and `last_login_at` / `last_activity_at` of `GET /auth/me` have the offset they lacked; these fields are
  `format: date-time` in `api-contract/v1/openapi.json` now (the type stays a string).
- Unique-violation races: a double click (two requests passing the same uniqueness check) answered 500 on tag, category,
  signal type, role and media format writes and on user creation. A unique violation at the commit now answers the code
  the check gives (`tag_name_taken`, `category_name_taken`, `signal_type_code_taken`, `role_name_taken`,
  `media_format_exists`, `username_or_certificate_taken`). `PUT /users/{id}/post-access/{post_id}` and
  `PUT /roles/{id}/permissions/{permission_id}` are idempotent: when a concurrent request created the row first they
  answer 200 with the stored row. `POST /users/` writes the user and its roles in one commit, so a failed role insert no
  longer leaves a user without roles. No new codes; the OpenAPI contract is unchanged.
- Resumable uploads: two concurrent `POST /media/uploads/{upload_id}/complete` both checked, hashed and stored the
  file, and the loser could mark the session `aborted` after the winner had completed it. `complete` now claims the
  session with a conditional UPDATE before it reads the file (status `completing`); a concurrent `complete`, chunk or
  abort answers 409 `upload_session_completing` and changes nothing. A refused file still aborts the session;
  `media_uploads_busy` and unexpected errors, a failed database flush or commit included (the request session is
  rolled back first), put it back to `open`. A claim whose request died is taken over after one hour
  (`COMPLETION_STALE_SECONDS`).
- A chunk request (`PATCH /media/uploads/{upload_id}`) that stalled past the writer stale time (180 s) kept writing
  after another request had taken the session over: both appended to the part file, the first one's error path
  truncated the second one's bytes and the next chunk filled the gap with zeros, so without the optional `sha256` the
  stored file was silently corrupt. A writer now renews its claim every 30 s while bytes arrive and checks it before
  every write, before truncating and before releasing; one that lost the session writes, truncates and releases
  nothing and answers 400 `upload_chunk_not_stored` (`offset` and `Upload-Offset` are the session's offset). A part
  file shorter than `received_bytes` is never extended: the session is aborted and the chunk answers 410
  `upload_bytes_missing`, as `complete` already did. A database error while a chunk streams rolls the request session
  back before the cleanup, so the bytes are truncated and the session is free at once (it stayed held for 180 s).
- A repeated `complete` on a completed upload session returns the stored result without another `create` audit row
  (and without probing the file again).
- `error` of the upload session bodies (`GET /media/uploads`, `GET /media/uploads/{upload_id}`, `POST .../abort`)
  carried the stored internal reason (for example the Python repr of the refused file's problem). It is now the code
  of the problem that aborted the session (`upload_checksum_mismatch`, `upload_bytes_missing`,
  `media_content_invalid`, ...) and `null` after a client abort or an expiry; the reason goes to the log. Rows written
  before this change answer `null`.
- `HEAD /media/uploads/{upload_id}` applies the expiry like `GET`: a session past `expires_at` answers
  `Upload-Status: expired` (it answered `open`) and loses its part file.
- A video whose poster or storyboard extraction takes the media worker down (out of memory, a crash) is no longer
  retried forever: an expired claim is taken again only while attempts are left (`storage.poster.max_attempts`,
  `storage.storyboard.max_attempts`). On its last attempt it becomes `failed` (`thumbnail_status` `failed`, 409
  `poster_failed`) with the error "the worker stopped during processing on the last attempt"; the worker records this
  before every claim (`fail_exhausted_posters`, `fail_exhausted_storyboards`) and so do both reconciliations.
  `POST /media/posters/reconcile` reports them as `failed_abandoned` (new required field of
  `PosterReconcileResponse` in `api-contract/v1/openapi.json`); `media_tools reconcile-posters` and
  `reconcile-storyboards` print it, and `--retry-failed` queues them again.
- An uploaded video is no longer claimable before its file is in storage: the row is created `pending` with a hold
  (`poster_locked_until` one hour ahead, `UPLOAD_HOLD`) that the commit after the move releases. The worker could
  claim it during a long move (a copy across filesystems) and use up every attempt on "source file missing". Clients
  see `thumbnail_status` `pending` as before. When the API stops before the release, the worker takes the row after
  the hold.
- The move of an upload into storage (the rename, or the copy across filesystems) and the walk of the storage
  directory of `GET /media/statistics` run in a thread instead of on the event loop; a copy of a large file to a
  network share blocked every other request. `GET /media/statistics` counts and sums in SQL instead of loading every
  media row; the response is unchanged. The copy across filesystems writes a temporary name in the destination
  directory and replaces it into place; a copy that fails removes it (still `media_store_failed`), so a partial
  file never sits under the stored name.
- Startup seeding of the default media formats skips a default whose extensions or MIME spellings already belong to a
  live row (the conflict check of `/media-formats`), so a renamed default is not added again next to it and two active
  rows never own `.jpg`; the skip is logged. The registry cache no longer stores a read that began before an invalidate,
  so a disabled format stops being accepted at once in the process that disabled it. The server ceiling
  `storage.max_file_size_gb` caps every format limit when it is read, not only when a row is written: session create,
  upload validation, the 413 answer and `GET /media/formats` all use min(format limit, ceiling).
- Video poster URLs carry imgproxy's cache buster `cb:` from `poster_generated_at` (as storyboard sheets carry
  `generated_at`): after `POST /media/{id}/thumbnail/regenerate` the new poster has new URLs and nginx no longer serves
  the render of the old one until its max-age. Image URLs are unchanged.
- A share link (`POST /media/{id}/share`) never expires later than now + `ttl_seconds` (itself capped by
  `storage.thumbnail.share_max_ttl_seconds`). It used to reach up to twice that. The expiry is
  floor(now / w) * w + ttl with w = max(1, ttl // 10), so links issued in one window are byte-identical (stable nginx
  cache key) and live more than ttl - w, that is more than 0.9 * ttl (more than 54 s for the smallest `ttl_seconds`,
  60). The thumbnail URLs in media responses keep `quantised_expiry`.
- Without nginx (`enable_nginx` off) `GET /media/{id}/download` and `/stream` follow RFC 9110 for `Range`: a single
  range `a-b`, `a-` or the suffix `-n` answers 206 with `Content-Range`; an invalid header, another unit or several
  ranges are ignored (200, whole file); an unsatisfiable range answers 416 `range_not_satisfiable` as a problem with
  `Content-Range: bytes */size` (Starlette answered text/plain 400/416 and refused suffix ranges). The file is
  streamed in chunks. New problem code `range_not_satisfiable` (416, param `size_bytes`) for the web localization
  package, declared on both routes; `api-contract/v1/openapi.json` gained it and the schema
  `RangeNotSatisfiableProblem`.
- `GET /media/{id}/thumbnail` behind nginx checks that the source (the original image or the poster) is on disk and
  answers 404 `media_file_missing` otherwise (declared on the route; it handed the path to imgproxy and nginx cached
  imgproxy's 404).
- Archiving a band reads the other bands of the observation with `FOR UPDATE` after the observation row is locked, so
  two concurrent archives can no longer leave an observation with no active band (the band list loaded with the
  observation was a plain read that the row lock does not cover on MySQL).
- A band's `frequency_center` must lie between `frequency_min` and `frequency_max` (both inclusive):
  `POST /observations/create` and `POST /observations/{id}/frequencies` answer 422 `frequency_center_out_of_range` on
  `frequency_center`, and `PUT /observations/{id}/frequencies/{frequency_id}` checks the merged band (stored values plus
  the request, a recalculated center included). New `FieldErrorCode` value; `api-contract/v1/openapi.json` gained it.
- `POST /observations/{id}/disappeared` with `disappeared_at` before `observed_at` answers 422
  `disappeared_before_observed` (the duration used to become negative). `PUT /observations/{id}` with a new
  `observed_at` on a disappeared observation recomputes `duration_seconds` (unless the request sets it) and answers 422
  `observed_after_disappeared` on `observed_at` when the new start is after `disappeared_at`. Two new `FieldErrorCode`
  values in the contract.
- Observation reports write user text (comment, signal type, post, user name and alias, tag names, filter values)
  unchanged and always as a string cell with the `quotePrefix` style: a value starting with `=` is no longer stored as a
  formula (in xlsx only a leading `=` is one) and `#N/A` and the other error names no longer become error cells; the
  spreadsheet shows the text as written (`-80 dBm`, `+380...`) and keeps it text when the cell is edited. Numbers and
  times stay typed.
- `GET /posts/{id}/observation-stats` limits `tz` to 64 characters (422 `validation_error` above that) and an
  `OSError` from the time zone database answers 400 `unknown_time_zone` instead of 500. The `tz` parameter in
  `api-contract/v1/openapi.json` gained `maxLength`.

### Changed

- New problem codes for the web localization package: `admin_role_not_renamable`, `admin_role_permissions_fixed`,
  `last_admin_required`, `cannot_remove_own_admin` (409) and `grant_exceeds_own_permissions`,
  `target_user_more_privileged` (403). The role and user routes above declare them; `api-contract/v1/openapi.json`
  gained their schemas.
- `POST /media/nginx-upload` answers 400 `nginx_file_name_invalid` when `X-File-Name` is not a regular file inside
  `storage.nginx_upload_temp_path` (paths outside it, `..`, links leading out, directories, missing files); the file
  is not touched. The route declares the code and `api-contract/v1/openapi.json` gained it. New problem code for the
  web localization package.
- `POST /auth/logout` has no request body: it ends the session of the access token. With only the Authorization header
  it answered 422; a body that is still sent is ignored. The `session_id` member (used only for tokens without a
  session, which are refused now) and the `LogoutRequest` schema are gone from `api-contract/v1/openapi.json`, and so
  is the problem code `no_active_session` (it can no longer occur): remove its translation from the web localization
  package.
- `GET /categories/{category_id}/signal-types` takes `page` and `page_size` (1 to 200, default 50, like the other
  lists) instead of `skip` and `limit` (contract change: `skip` and `limit` are gone from
  `api-contract/v1/openapi.json`), and orders by `sort_order`, then `id`. `limit=-1` answered 500 on MariaDB.
- Resumable uploads: new session status `completing` (in `status`, `Upload-Status` and the `status` filter of
  `GET /media/uploads`) and new problem code `upload_session_completing` (409) for the web localization package.
  `PATCH /media/uploads/{upload_id}` declares `upload_session_completing` and `upload_bytes_missing`,
  `POST .../complete` declares `upload_session_completing` and `upload_writer_busy`, `POST .../abort` declares
  `upload_session_completing`; `api-contract/v1/openapi.json` gained these, the schema `UploadSessionCompletingProblem`
  and the description of `error`. UPLOADS.md lists the session states.

### Changed (operators)

- Deployment defaults are closed: `nginx-proxy.conf` sets `X-Forwarded-For $remote_addr` (replace, not append; reload
  nginx and re-install the snippet), the image no longer starts uvicorn with `--forwarded-allow-ips '*'` (it trusts
  `127.0.0.1`, or `FORWARDED_ALLOW_IPS` when nginx connects from another address) and the image sets
  `MYSETTINGS_APP__ENVIRONMENT=production` (`app/config.toml`, used by local runs, stays `development`). A forged
  `X-Forwarded-For: 127.0.0.1` passed the `/health` host check and a forged session IP was stored. The test stack
  sets `FORWARDED_ALLOW_IPS=*` because its nginx is a container.
- The API refuses to start in production when `security.secret_key` is empty, `your-secret-key-change-this-in-production`
  or `CHANGE_ME` (case ignored); set `MYSETTINGS_SECURITY__SECRET_KEY`. Outside production one warning line is logged at
  start. `health.enable_host_check` defaults to on in production (off in development; set it to decide), and the
  committed `app/config.toml` no longer lists `"*"` in `health.allowed_hosts`: `/health` answers only requests from
  the host itself. ENV.md and DEPLOYMENT.md state the defaults and why the API binds `127.0.0.1`.
- Run the migration `018_media_file_size_bigint` (it runs at startup): on MySQL it alters `media_files.file_size` to
  BIGINT, keeps every row and is skipped when the column is already BIGINT (sqlite needs no change). The downgrade turns
  the column back into INT; it reads `MAX(file_size)` first and refuses with a clear error, changing nothing, while a
  file over 2 GiB is stored (a non-strict `sql_mode` would otherwise clamp those sizes to 2147483647).
- Every offloaded upload the API refuses after the path check (415, 413, 403, 404, busy store, invalid content) now
  deletes nginx's body file; before, nginx (`client_body_in_file_only`) left up to 10 GB behind for each. Requests
  rejected before the handler runs (401) still leave their file: `python -m app.backend.cli.media_tools
  sweep-nginx-temp [--max-age-hours N]` deletes files in `storage.nginx_upload_temp_path` older than
  `storage.nginx_upload_temp_max_age_hours` (new setting, default 24); run it from cron (DEPLOYMENT.md). The setting
  must name the same directory as nginx's `client_body_temp_path` (`tests/test_nginx_conf.py` checks the default).
  A body file the API cannot delete (`OSError`, for example a permission error) is logged and the upload keeps its
  answer; the sweep skips such a file, logs it and goes on, and prints `skipped N file(s)` after the removed count.

## Backend layout: auth, errors, bootstrap and cli packages

### Changed (operators)

- The command-line tools moved to `app/backend/cli/`; the old module paths are gone, update scripts, cron jobs and
  runbooks (DEPLOYMENT.md, `systemd/` and WEB_TOOLING.md already use the new ones):
  `python -m app.backend.migrations_manager` -> `python -m app.backend.cli.migrations_manager`,
  `python -m app.backend.media_tools` -> `python -m app.backend.cli.media_tools`,
  `python -m app.backend.init_user` -> `python -m app.backend.cli.init_user`,
  `python -m app.backend.api_contract` -> `python -m app.backend.cli.api_contract`.
  The API and the media worker (`python -m app.backend.workers.media_worker`) start as before.

### Changed

- Code only, no route, status or field changes: the authentication chain moved into `app/backend/auth/`
  (`dependencies.py`, `require.py` from `services/permissions.py`, `context.py` from `services/auth_context.py`,
  `service.py` from `services/auth.py`, `certificates.py` from `helpers/certeficates_helper.py`, plus
  `access_control.py`, `access_errors.py`, `route_access.py`, `sessions.py`, `presence.py` from `services/`).
- Contract rewritten on purpose for one description: the `SessionStatus` schema no longer names a source file.
- `app/main.py`, `init_user.py` and `migrations_manager.py` no longer add the project root to `sys.path`: run them
  as modules from the project root (`python -m app.main`, `python -m app.backend.init_user ...`).
- A successful authentication logs at DEBUG instead of INFO (one line per request).
- Code only: the error machinery moved into `app/backend/errors/`: `coded.py` (`CodedError`, `invalid()`, no FastAPI),
  `problems.py` (`ApiError`, from `services/api_errors.py`), `handlers.py` (from `error_handlers.py`), `openapi.py`
  (from `openapi_problems.py`). A route converts a service failure with `ApiError.from_coded(exc, ...)`, which replaces
  `exc.to_api_error(...)`. `services/observation_counts.py` raised `ApiError` and now raises `CodedError` (same codes and
  statuses). A test keeps services, filters, models, workers, enums, `bootstrap/` and `utils/` away from FastAPI and
  Starlette, also through other modules.
- Code only: startup and seeding moved out of `database.py` into `app/backend/bootstrap/` (`startup.py` schema lock and
  migrations, `seed.py`, `permissions.py` with `PERMISSION_META` and `ADMIN_ROLE_NAME`); `database.py` keeps the engine
  and sessions.
- Code only: `helpers/` is gone. `create_audit_log` moved to `services/audit.py`, `client_info(request, auth)` (used by
  routes only) to `api/shared/helpers/audit_helper.py`, `truncate_for_logging` to its one user, `auth/service.py`.

## Media that cannot be rendered; sizes as displayed (found by the live format matrix)

### Added (API clients)

- New problem codes (web translations need both): `image_too_large_for_thumbnails` (422, params `megapixels`,
  `max_megapixels`) from `GET /media/{id}/thumbnail` and `POST /media/{id}/share`; `no_video_stream` (422) from
  those two and from `GET /media/{id}/storyboard`. Contract rewritten on purpose (three operations, two schemas).

### Fixed

- Photos of 108 and 200 MP phone cameras got no thumbnails: imgproxy's resolution limit was 60 megapixels. It is now
  250 (`IMGPROXY_MAX_SRC_RESOLUTION` in `.env`, one variable for imgproxy and the API's new
  `storage.imgproxy.max_src_resolution_mp`); measured, a 200 MP source costs imgproxy 63 MB (JPEG) to 124 MB (PNG)
  per render. The limit stays as the guard against small files holding gigapixels (a 274 KB PNG of 1.6 GP: 3.5 s of
  CPU per render).
- An image above that limit was reported `thumbnail_status: ready` with signed URLs that always answered
  imgproxy's `422 text/plain`. It is now stored with `thumbnail_status: unsupported`, no `thumbnail_urls`, no
  pre-warm, and the thumbnail and share routes answer `image_too_large_for_thumbnails`. The size is read from the
  header at upload; images stored without a size are rendered as before.
- A video upload with only audio was tried three times by the worker and ended `409 poster_failed` ("an
  administrator can regenerate it", which never helps). The probe at upload, or the worker's own, now marks it
  `poster_status = unsupported` (new value; never claimed): `thumbnail_status` and `storyboard_status` are
  `unsupported`, the routes answer `no_video_stream`. Poster counts (`GET /media/posters/status`, `GET /statistics`)
  get an `unsupported` key.
- `width` and `height` of a vertical phone video (stored as a landscape frame plus a display rotation) and of an
  EXIF-rotated photo were the stored frame size, so a player sized from them was landscape. They are now the size
  as displayed, as posters, storyboards and browsers show it. Existing rows: `media_tools probe-missing --all`
  (DEPLOYMENT.md, "Upgrades").
- Every 4xx answered in a route with a database session was also logged as `ERROR Database session error`, and an
  unhandled error got a second line without its `error_id`. `get_session` now only rolls back.

### Backend

- `tests/docker/formats.py`: live format matrix against the e2e stack (JPEG, PNG, WebP, GIF in their variants;
  MP4 H.264 / H.265 / MPEG-4, WebM VP8 / VP9 / AV1, MOV H.264 / ProRes, MKV H.264 / HEVC / VP9; rotation, odd
  sizes, 4K, sparse keyframes, multi-sheet storyboards, files without a video stream, truncated files).

## Video storyboards: seek previews (route and field)

### Added

- `GET /media/{media_id}/storyboard` (`media.view` plus the post scope): WebVTT (`text/vtt`) with one cue per
  frame, each pointing at a tile of a signed sheet URL (`#xywh=x,y,w,h`). `202` + `Retry-After` +
  `X-Storyboard-Status` while the worker has not produced it, `409 storyboard_failed` (`attempts`; also for a ready
  row whose layout or video duration is unusable, never a 500), `422 storyboard_unsupported`
  (`min_duration_seconds`): not a video, too short, or storyboards turned off. The answer follows the
  `video_storyboards` row the route loads. `Cache-Control: private, max-age` is at most the lifetime of the URLs
  inside.
- Sheet URLs are absolute: `storage.imgproxy.public_base_url`, else the request origin (behind nginx: the Host and
  scheme nginx passes), because a player resolves cue URLs against the track, a `blob:` URL. They carry imgproxy's
  cache buster `cb:` from the row's `generated_at`, so a regenerated storyboard never comes from the nginx cache of
  the previous one.
- `storyboard_status` (`ready`, `pending`, `processing`, `failed`, `unsupported`) in `MediaFileInfo` (media info,
  lists, observation media). New problem codes `storyboard_failed`, `storyboard_unsupported` (web translations
  need both). web-kit: `storyboardTrack()` in `client.ts` (`null` for `202`, `409`, `422`; an `ApiFailure` for any
  other answer).
- Media worker: builds the storyboards (an ffmpeg pass over the keyframes, a full decode only when that writes
  nothing; adaptive frame step; both passes repeat the last decoded frame up to the duration and stop at the planned
  frame count, so no cue points at a black tile) into `MEDIA_ROOT/_derived/storyboards/`, after the poster is
  ready. Never delays a poster: a storyboard is claimed only when no poster was claimed, `storyboard.slots` (1, per
  worker process) must stay below `poster.concurrency` or the worker refuses to start; the newest video is claimed
  first. Stop cancels a running storyboard and gives the attempt back; at start it reconciles and removes leftover
  temporary directories (a filesystem error there is logged, posters go on), and every
  `upload.sweep_interval_seconds` it queues videos that became eligible meanwhile. State: new table
  `video_storyboards` (migration `017_video_storyboards`).
- Upgrade note: storyboards are on by default and need `poster.concurrency` of at least 2; a worker with
  `MYSETTINGS_STORAGE__POSTER__CONCURRENCY=1` refuses to start. Raise it to `2` or set
  `MYSETTINGS_STORAGE__STORYBOARD__ENABLED=false` (DEPLOYMENT.md, "Storyboards").
- Operations: `media_tools storyboard-status` and `media_tools reconcile-storyboards [--retry-failed]`.
- Configuration: `[storage.storyboard]` in `app/config.toml` (`enabled`, `slots`, `min_duration_seconds`,
  `max_frames`, `min_step_seconds`, tile and grid, `jpeg_qscale`, `threads`, `nice`, timeouts, leases, retries,
  `retry_after_seconds`). The sheets are served through `/media-images/`; nginx needs no change.
- Docs: CLAUDE.md (media pipeline), DEPLOYMENT.md (worker resources, commands), API.md, WEB_TOOLING.md section 9.

## API errors and infrastructure errors; one request id from nginx to the API log

### Changed (API clients)

- Errors come in two kinds, told apart by `Content-Type` (API.md, "Errors"): `application/problem+json` is the
  API's, handled by `code`; any other error response comes from the infrastructure in front of the API (nginx:
  client certificate, body size, API not reachable or too slow, file serving) and is handled by status only. The
  OpenAPI document is unchanged. web-kit: `apiFailure()` and `infrastructureText()` in `client.ts`; `Messages`
  gets a required `infrastructure` section (`http_error`, `network_error`, optional `by_status`), so every locale
  needs those texts.
- Every response carries `X-Request-ID` (behind nginx: nginx's `$request_id`, also in its access log), nginx's own
  error pages included. The `error_id` of a 500 is the request id.

### Breaking

- Public image URLs (`thumbnail_urls` in media responses, `POST /media/{id}/share`) move from `/t/` to
  `/media-images/` (`storage.imgproxy.public_prefix`, nginx `location ^~ /media-images/`): a name that says what
  it serves (every image made from a media file, not only thumbnails) and that a route of the web client at the
  same origin cannot take by accident. No transition: links issued before the deploy (at most 7 days for shared
  ones) stop working; clients take the new URLs from the API.

### Fixed

- Public thumbnail links stop working at their `exp:`. Once rendered, nginx served them from its cache for 30 days
  whatever their expiry: only imgproxy checks `exp:`, and the cache ignored the max-age imgproxy caps at it. The
  `/media-images/` cache now follows imgproxy's `Cache-Control` and serves no stale copies. Thumbnail answers carry
  one `Cache-Control` (imgproxy's own is hidden).
- `GET /media/{id}/download` and `/stream` behind nginx: a file missing on disk is `media_file_missing` (404
  problem), as without nginx; the API handed the path over unchecked and nginx answered its own HTML 404.
- nginx `client_max_body_size` sits above the API's limits (65m for chunks of 64 MB, 11G for files of 10 GB):
  at equal limits nginx answered its own HTML 413 first, and clients never got `upload_chunk_too_large` /
  `media_file_too_large` with their params.
- web-kit `contract-check.ts` handles the 401 `token_certificate_mismatch` (log in again): `npm run api:check`
  failed since that code was added. `session_expired` now leads to a new login, as API.md says, not to a refresh:
  a refresh of an ended session answers `session_expired` again.

- An unhandled error was logged twice: once with its `error_id`, then again by uvicorn without it (Starlette
  re-raises after the `Exception` handler answers). `UnhandledErrorMiddleware` answers the 500 and logs it once.
- Tracebacks in the log carried the values of every variable (loguru's `diagnose`), request bodies with their
  tokens included. Every log sink now has `diagnose=False`.
- Started with `python app/main.py`, the server went silent after the migrations: Alembic's `fileConfig` disabled
  uvicorn's loggers (`disable_existing_loggers=False` now), and uvicorn's default `log_config` dropped the handler
  of `uvicorn.error` ("Uvicorn running on ..."; `log_config=None` now: uvicorn logs through the app's loguru).

### Backend

- `RequestIdMiddleware` (`request_id.py`): every log line carries the request id (`{extra[request_id]}`, "-"
  outside a request). nginx passes `$request_id` (`nginx-proxy.conf`), answers it on every response and writes it
  to `/var/log/nginx/watch_web.access.log` (`log_format watch_web` in nginx-http.conf).
- The test client checks that every error the API answers is a problem (`errors_are_problems`).

## Tag filter: any or all of the tags

### Added

- `tag_match` next to `tag_ids` in `GET /observations/list-all`, `GET /search/observations` (query) and
  `POST /observations/report` (body): `any` (default, the behaviour so far) keeps observations with at least one
  of the tags, `all` keeps observations with every one of them (a repeated id counts once, archived tags count).
  New component schema `TagMatch`. The Excel report header names the mode: "Теги (будь-який)" or "Теги (усі)".

### Fixed

- The descriptions of `tag_ids` said "all required" (list, search) and "ALL of these tag IDs" (report), while
  every one of them matched any of the tags. They now describe `tag_match`.

### Backend

- One condition for lists, searches and reports: `filters/observation_tag_filter.py`
  (`ObservationTagFilter.observation_has_tags`), instead of three copies.

## Each API version has its own OpenAPI document; the contract of a version lives in its folder

### Breaking changes for API clients

- The OpenAPI document and its viewers moved under the version: `/api/v1/openapi.json`, `/api/v1/docs`,
  `/api/v1/redoc` (`/openapi.json`, `/docs`, `/redoc` are gone). Each later version serves its own document
  (`/api/v2/openapi.json`); type names and operation ids are unique within a version, so a new version never
  renames a type of v1 (one document for every version would have).
- The committed contract moved to `api-contract/v1/openapi.json` (was `api-contract/openapi.json`). Its paths,
  bodies, problems and schemas are unchanged; only the API description names the new document URLs. Web: point
  `api:types` at the new file (WEB_TOOLING.md, section 2) and drop `/docs`, `/redoc`, `/openapi.json` from the dev
  server proxy (section 8).

### Backend (no change to the v1 contract)

- Request, response and query models live in the version that publishes them:
  `app/backend/api/v1/models/{request,response,query}/<resource>.py`, response builders next to their models
  (`post_to_response`, the auth builders, `upload_result`). `api/shared` keeps version-neutral helpers only,
  health models are in `api/system/health_checks.py`.
- Services and filters never depend on the API: the observation counters return dataclasses, and every filter
  service declares its criteria as a dataclass (`TagCriteria`, ..., `ObservationReportCriteria`) that the query
  model of a version converts into. `filters/params/` is gone; `SortOrder` and `ObservationReportType` are in
  `enums/general.py`.
- Tests: no component name carries a module path, import rules of the API layers, every v1 query model converts
  into its service's criteria, the document and viewers of each version, no nginx location without a route.

## Sessions live while used, bound to the client certificate; state computed when read; no CORS in the API

### Breaking changes for API clients

- A session lives while it is used: it ends after `session_idle_timeout_minutes` (default 30) without a request,
  and every request and every `POST /auth/refresh` moves that end. Before, it ended exactly 8 hours after the login
  whatever the activity, and the 7-day refresh token was cut by it. `session_max_hours` can add a hard limit since
  the login (default 0: none). Every refresh issues a new access and refresh token; a refresh token stays usable
  for the access token lifetime plus the idle timeout.
- A session opened through nginx with a client certificate is bound to it (RFC 8705 style): every request and
  refresh must present the same certificate, otherwise 401 `token_certificate_mismatch` (new code, in the 401 set
  of every authenticated operation). A copied token is useless without the certificate's private key; a refused
  attempt does not end the owner's session. Development logins (JSON body) are not bound.
- `POST /auth/refresh` with a refresh token that belongs to no session answers 401 `invalid_refresh_token` (every
  token from `POST /auth/login` has one).
- `session_expired` means the session ended (logout, idle timeout, hard limit, revoked): a refresh cannot help,
  log in again (API.md, "Authentication"); with a client certificate that needs no user input.
- `GET /auth/sessions`: only live sessions (before: also sessions idle for hours), each with `status` (`online` /
  `idle`), newest first; `expires_at` is when the session ends unless a request comes. Times carry the UTC offset
  (`2026-10-03T08:00:00+00:00`); they were naive, which JavaScript reads as local time.
- The API sends no CORS headers any more (it allowed `*` with credentials). The web client comes from the same
  nginx and origin; in development the dev server proxies the API paths (WEB_TOOLING.md, section 8). A browser
  application on another origin gets CORS at nginx (DEPLOYMENT.md, "Web client and other clients").

### Added

- `GET /users/{id}/sessions` (`user.view`, administration): a user's sessions, newest first, live only unless
  `include_ended=true`. Each has `status` (`online` / `idle` / `ended`), computed when read, and for ended ones
  `end_reason` (`logout`, `expired`, `idle_timeout`, `revoked`) and, for revoked ones, `revoke_cause`
  (`account_deactivated`, `account_archived`, `certificate_changed`, `ended_by_admin`). The cause describes what
  made the server end the session; the account itself can be unchanged (a new certificate, an administrator).
- `POST /users/{id}/sessions/end` and `POST /users/{id}/sessions/{session_id}/end` (`user.view` + `user.update`,
  administration): revoke all live sessions / one session (`ended_by_admin`); the account stays as it is.
  New codes: `session_not_found` (404), `session_already_ended` (409, `end_reason`).

### Fixed

- `PATCH /users/{id}` validated `role_ids` after committing the other fields: `role_not_found` or
  `role_archived_not_assignable` left the rest of the PATCH saved. Roles are now checked before anything is written
  and replaced in the same transaction.
- `POST /users/{id}/archive` ends the user's sessions in the transaction of the archive flag, like deactivation.

### Database

- Migration `015_session_end_state`: `user_sessions.end_reason`, `user_sessions.revoke_cause`, index on
  `ended_at`; `user_sessions.is_active` is dropped (it duplicated `ended_at` and read like a row lock). Inactive
  rows without `ended_at` get `ended_at = last_activity`; their `end_reason` stays null.
- Migration `016_session_certificate_binding`: `user_sessions.certificate_fingerprint_hash` (SHA-256 of the
  certificate fingerprint). Sessions opened before stay unbound until they end.
- `user_sessions.expires_at` is now when the session ends unless a request comes; requests move it.
- Settings: `security.session_expire_hours` and `security.refresh_token_expire_days` are replaced by
  `security.session_idle_timeout_minutes` (30) and `security.session_max_hours` (0: no hard limit) (ENV.md).
  `security.allowed_origins` is gone with CORS. `app/config.toml` and `.env.example` carry the new keys only.
- Startup reports every setting no model declares, from `config.toml` or a `MYSETTINGS_*` variable:
  `WARNING: unknown setting security.session_expire_hours is ignored`; remove such keys from deployed `.env` files.
- `.env.example`: the admin certificate fingerprint is SHA-1 (`openssl x509 -fingerprint -sha1`), as nginx reports it;
  the SHA-256 the template asked for could never match, and the admin could not log in through nginx.

### Deployment

- `nginx-app.conf` claims only the paths the API publishes (`/api/`, `/docs`, `/redoc`, `/openapi.json`,
  `/problems`, `/health`); `/` belongs to the server block in `nginx.conf`, where the web client build is
  configured (`root`, client-side routes fall back to `index.html`, 404 without a build).
  `tests/test_nginx_conf.py` fails when an API route has no location.

## Client certificates checked by nginx; certificate replacement ends sessions

### Security

- `nginx.conf` had no client certificate verification and did not set the `X-SSL-Client-*` headers that
  `POST /auth/login` trusts in production: nginx passed the client's own headers through, so anyone could send
  `X-SSL-Client-Verify: SUCCESS` with the DN, serial and fingerprint of another user's certificate (public data)
  and log in as that user. Now port 443 requires a client certificate (`ssl_verify_client on`,
  `ssl_client_certificate`, `ssl_crl`), port 80 only redirects, and every location that proxies to the API sets
  the four headers from nginx's variables, replacing whatever the client sent. Verified against nginx 1.27:
  a forged header arrives as the real certificate's value, no certificate or a revoked one gets 400.
- nginx files: `nginx.conf` (server blocks), `nginx-app.conf` (locations, new, shared with the e2e stack),
  `nginx-proxy.conf` (API proxy headers, new). DEPLOYMENT.md: install steps, "Client certificates (mTLS)".
  `tests/test_nginx_conf.py` fails when an API location misses the headers or mTLS is off.
- nginx keepalive to the API and imgproxy works in every location (`proxy_set_header Connection ""` was set at
  server level, which nginx does not copy into locations that set their own headers). The unused WebSocket
  `Upgrade` lines are gone.

### Changed for API clients

- `PATCH /users/{id}` with a different certificate ends every session of the user (`end_reason` `revoked`,
  `revoke_cause` `certificate_changed`; audit `force_logout`), like deactivation: the old certificate's tokens
  answer 401 `session_expired`; the user stays as is and logs in with the new certificate. The
  same certificate uploaded again changes nothing. An administrator replacing their own certificate ends their
  own session as well.
- `POST /auth/login` (production) matches the certificate serial as nginx writes it: OpenSSL prints two hex
  digits per byte, so serial `0x0A1B2C` arrives as `0A1B2C` while the upload stored `a1b2c`. Every certificate
  with an odd number of hex digits in its serial was refused with `certificate_not_recognized`.

## Unexpected failures are 500 `internal_error`, not a domain code

A blanket `except Exception` turned every failure, bugs included, into a code that told the client something it
could act on. Those clauses are gone or catch only the failure they are about; the rest is the global 500
`internal_error` with an `error_id` in the server log.

### Breaking changes for API clients

- `PATCH /media/uploads/{upload_id}`: `upload_chunk_not_stored` (400) now means only that the transfer broke (client
  disconnected) or the disk refused the bytes. Any other failure answers 500 `internal_error`; the part file is
  truncated back to the offset in both cases, so resending from `GET`'s offset still works. Before, a server bug was
  a 400 and a client retrying on 400 looped forever.
- `POST /media/uploads/{upload_id}/complete`: `upload_complete_failed` is gone; an unexpected failure is 500
  `internal_error` (the session stays open, retry later). `media_store_failed` is unchanged.
- `POST /media/upload` and `POST /media/nginx-upload`: `media_upload_failed` is gone (500 `internal_error`).
- `POST /users/` and `PATCH /users/{id}`: `certificate_processing_failed` is gone. A file that is not a PEM or DER
  certificate is still 400 `certificate_invalid`; any other failure is 500 `internal_error`.
- `POST /auth/login` (mTLS / PKI): a database failure while looking up the certificate answered 401
  `certificate_not_recognized`; it is now 500 `internal_error`. `POST /auth/logout`: a database failure answered
  `session_already_invalidated`; it is now 500. `POST /users/{id}/archive` no longer reports success when ending
  the user's sessions failed (the user is archived either way; a session left live is ended the first time its
  token is used).
- `/search/*`: a failure to write the search audit entry fails the request (500) instead of being only logged.
- `database_constraint_violation` and `database_error` are gone (create / update of categories, signal types, tags,
  observations, media formats and users): an unexpected database error is 500 `internal_error` with its `error_id`.
  The status was 500 before too; only the code changes. Known constraints keep their codes:
  `username_or_certificate_taken`, `username_taken`, `certificate_taken`, `post_code_taken`, `data_too_long`.
- `post_code_taken` answers only when the unique index on the code refused the row; any other constraint failure of
  a post write was reported as "code taken" and is now 500.

### Backend

- Startup: a failing Alembic migration stops the startup. The `create_all` fallback, which created the missing tables
  and left the schema half migrated, is gone. It hid a real failure: `alembic.ini` resolved `script_location` and
  `prepend_sys_path` against the working directory, so a start from `app/` (the IDE run configuration) never ran a
  migration and got its tables from `create_all`. Both are now relative to `alembic.ini` (`%(here)s`) with
  `path_separator = os` (the legacy split on `:` cut `E:\...` in two). The first start after this applies every
  migration such a database missed, the data migrations included (`007` HTML entities, `010` permission renames).
  `001` no longer reads `media_files.thumbnail_path` when the table has no such column (built by `create_all` from
  the current models).
- Unique and length violations are recognised by the driver's error number (`api/shared/helpers/db_errors.py`:
  MySQL 1062 / 1406, sqlite message) instead of `str(exc)`, which carries the SQL and its parameters and depends on
  the server's message language. Seeding stops when the admin PKI credentials cannot be generated
  instead of silently skipping the admin user.
- Best-effort blocks catch only their own failure: `OSError` in the media manager (integrity check, cleanup,
  storage statistics), `ImportError` / `RuntimeError` for the optional `imageio_ffmpeg`,
  `NoInspectionAvailable` in the filter engine. A failed file move still removes the new media record whatever the
  error is; only an `OSError` becomes `media_store_failed`.
- The health check logs why the database check failed (the response still carries no error text).
- Removed unused code: `AuthService.cleanup_expired_sessions`, `CertificateParser.validate_certificate`,
  `app/backend/utils/security.py` (nothing imported it), the re-exports of `app/backend/utils/__init__.py`.

## API versions as folders, errors declared on the routes, FastAPI built-ins instead of workarounds

### Breaking changes for API clients

- Security: the `/search/*` routes returned raw table rows; `/search/users` exposed `pki_certificate_cn_hash`, `pki_certificate_serial_hash` and `pki_certificate_fingerprint_hash`. Each search now returns the response model of the entity's list route (`SearchResponse[UserResponse]`, `SearchResponse[TagResponse]`, ...): items have exactly the fields of `GET /users/`, `GET /tags/list-all`, and so on. OpenAPI: `SearchResponse` becomes one schema per entity (`SearchResponse_UserResponse_`, ...).
- `POST /categories/create`, `PUT /categories/{id}`, `POST /signal-types/create`, `PUT /signal-types/{id}` are documented with `CategoryResponse` / `SignalTypeResponse` instead of the table models `Category` / `SignalType` (same fields; `id` is no longer nullable).
- The 10 `GET .../filter-schema` routes are gone (users, posts, roles, observations, media, media-formats, categories, signal-types, tags, search). OpenAPI describes every filter: each list and search route now documents its query parameters with descriptions and types (they are FastAPI query models), and `sort_by` is an enum of the sortable fields of the entity.
- `sort_by` with a field that is not sortable answers 422 `validation_error` (it was silently ignored).
- `status_in` and `tag_ids` (observations list and search) are repeated query parameters: `?tag_ids=1&tag_ids=2` instead of `?tag_ids=1,2`. A non-numeric tag id answers 422 instead of 500.
- An id below 1 in a path (`/users/{user_id}`, `/users/{user_id}/roles`, every `/roles/{role_id}...` and `.../permissions/{permission_id}`) answers 422 `validation_error` (item code `greater_than_equal`, `parameter` + `location: path`) instead of 400 `user_id_invalid` / `role_id_invalid` / `permission_id_invalid`; those three codes are gone.
- Ids in bodies (`role_ids`, `permission_ids`, observation `tag_ids`) are positive integers in the schema; a value below 1 is the pydantic item code `greater_than` instead of `role_ids_invalid`, `permission_ids_invalid`, `tag_id_invalid`. Field codes gone with them: `role_ids_required` and `permission_ids_not_list` (never reachable: `min_length` and the list type are checked first) and the unused `end_before_start`.
- Unexpected failures of `GET /users/`, `GET /roles/`, `GET /roles/list-all-permissions`, `POST /auth/login`, `/auth/refresh`, `/auth/logout` and `GET /auth/sessions` answer 500 `internal_error` (with `error_id`) like every other route; the codes `users_list_failed`, `roles_list_failed`, `permissions_list_failed`, `authentication_unavailable`, `token_refresh_failed`, `logout_failed`, `sessions_list_failed` are gone. Fixed with it: `GET /roles/?include_deleted=true` without `role.archive` answered 500 instead of 403 `permission_required_for_action`.
- `POST /users/` and `PATCH /users/{id}`: `role_ids` is sent as one form field per role (`role_ids=1`, `role_ids=2`), the standard way to send a list in a form, instead of one field holding a JSON array (`"[1, 2]"`). The code `role_ids_not_json_array` is gone; an invalid id is a `validation_error` item at `#/role_ids/<index>`. OpenAPI documents the forms as `UserCreateForm` / `UserUpdateForm` (FastAPI form models) instead of `Body_create_user` / `Body_update_user`. Unknown form fields are still ignored.

### Changed for API clients

- 18 operations without inputs (`GET /auth/me`, `/auth/sessions`, `/auth/route-access`, the `filter-schema` routes, `/media/formats`, `/media/statistics`, `/media/posters/status`, `/observations/report/types`, `/roles/list-all-permissions`) now also list 422 `validation_error`: the version router declares it for all of its routes. Only their error union widens.
- `FieldError.pointer`, `.parameter` and `.location` are optional members in OpenAPI (absent when not set) instead of nullable ones; the bodies never carried `null`. The generated type becomes `pointer?: string`.
- `PATCH /media/uploads/{upload_id}` documents its `Upload-Offset` header as a parameter.
- `GET /auth/route-access` lists the routes of the API version it is called through (all of them today).
- No path, method, tag or operationId changed.

### Web tooling: stock FastAPI + openapi-typescript + TypeScript only

- The contract is one file, FastAPI's OpenAPI document: `api-contract/openapi.json` (committed) or `GET /openapi.json`. Gone: `problems.json`, `python -m app.backend.api_contract export|check|lock`, `api-contract/problems.lock.json`, `scripts/check-i18n.mjs`. `python -m app.backend.api_contract` only writes `openapi.json`.
- OpenAPI no longer injects the `Problem` and `ProblemCode` schemas: every problem type is still a component `<Code>Problem`, and `api-contract/web-kit/src/api/client.ts` derives `Problem` / `ProblemCode` from them with plain TypeScript. `FieldErrorCode` is generated from the model (`FieldError.code` is `FieldErrorCode | string`).
- Translations are checked by `tsc`: a locale `satisfies Messages` (`web-kit/src/i18n/messages.ts`) fails on a missing code (TS2741) and on a code the API no longer has (TS2353). Verified with openapi-typescript 7.13, TypeScript 5 and openapi-fetch 0.17 on a clean project. The placeholder check of the old script is not replaced; see WEB_TOOLING.md section 5.

### Backend

- `tests/test_parallel_versions.py` proves the version model: a second version with its own module on its own prefix runs next to v1, operationIds stay unique (FastAPI's `generate_unique_id_function` per include), each handler returns URLs of its own version, `/auth/route-access` lists the caller's version only, a route published twice in one version is rejected.
- Layout: `app/backend/api/v1/` holds the v1 handler modules (the folder is the version), `api/shared/` the request / response models and helpers, `api/system/` health and the problem pages; `app/backend/routes/` is gone. `api/v1/__init__.py`: `V1 = PublishedVersion(APIVersionRoutes.V1, auth, users, ...)` (`api/versioning.py`) builds the version router, rejects a route or operation published twice and gives full paths: `V1.path(PostRoutes.POST_BY_ID, post_id=7)`. Versions are self-contained and run in parallel; CLAUDE.md, "Routes and API versions".
- `enums/routes.py`: plain `str` Enums; collection roots are `ROOT` members of their own group; `/docs`, `/docs/oauth2-redirect`, `/redoc`, `/openapi.json` come from `DocsRoutes`. Removed members no route used: `MediaRoutes.MEDIA_BY_ID`, `SystemRoutes.ROOT`, `STATIC`, `FAVICON`.
- Errors in OpenAPI: every route declares its problems with `responses=problems(...)`; internal_error is declared on the app, the 401 set and validation_error on the version router. The static call graph of `openapi_problems.py` is gone; the `openapi()` hook only moves error content to `application/problem+json` and adds framework problems. A session fixture fails the test run on any problem the app answered that its operation does not declare.
- `require()` returns a `Require` that is the FastAPI dependency itself (no closure with a marker attribute). `AuthContext.session_id` replaces `request.state.session_id`; the logout fallback to the body's `session_id` works again. One JWT check (`AuthService.verify_token`). `helpers.audit_helper.client_info(request, auth)` replaces 7 copies of the audit fields; the client address is `request.client.host` (uvicorn `--proxy-headers`).
- Framework errors are mapped by status, the route access map is built on first use, request headers are `Header()` parameters, the report is a plain `Response`, upload helpers live in `api/shared/helpers/upload_helper.py`.
- Search audit entries store the real request path (it was hard-coded as `/api/v1/search/<entity>`, wrong for `signal_types` and `media_formats`).
- Removed dead code: `filters/base.py`, `search_request.py`, a second `ObservationFilterParams`, unused validator models, unused audit contextvars, `AuditedSession`, the `SELECT 1` on every request (the engine has `pool_pre_ping`), commented-out routes, unused imports.
- Fixed imports of `AuthErrorCode` / `AccessErrorCode` from `enums.general` (they live in `enums/errors.py`); the app did not start.

## Locked error contract and web tooling

### Changed for API clients

- `GET /media/statistics` no longer returns `storage_path` (the absolute path of the media directory on the server).

### Added for API clients

- `python -m app.backend.api_contract export --out <dir>` writes `openapi.json` and `problems.json` (every problem code with status, title, detail and typed params, the field error codes, pydantic's built-in codes) for the web tooling, without a running server.
- `WEB_TOOLING.md` with starter files in `api-contract/web-kit/`: openapi-typescript 7 + TypeScript 5 strict + openapi-fetch 0.17 setup, compile-time contract checks, a translation check (`check-i18n.mjs`: missing translations, placeholders that are not params, `--fill`). Every step was run on a clean project.

### Backend

- `api-contract/problems.lock.json` locks the error contract: codes, their status and params, the codes of every operation (by operationId, never Python names). `tests/test_api_contract.py` fails on any difference with a report of what breaks the web client; `python -m app.backend.api_contract check` prints it, `lock` accepts an intended change.

## Errors as RFC 9457 Problem Details, documented in OpenAPI

Error bodies had four shapes under `detail`: a string (most 400 / 404 / 409), a FastAPI list (422), `{code, message, ...}` (401 / 403) and `{message, ...}` without a code (media, uploads, role delete). The web client could only tell most errors apart by their English text, and `/docs` documented none of them.

### Breaking changes for API clients

- Every error, whatever the status, is an RFC 9457 problem with `Content-Type: application/problem+json` and the members at the top level of the body (no `detail` wrapper any more):
  - `type`: URI of the problem type, `<origin>/problems/<code>` (or `settings.app.problem_base_url` + `/<code>`); it opens the documentation of the type;
  - `title`: fixed summary of the problem type; `status`: the HTTP status; `detail`: English text of this occurrence; `instance`: path of the request;
  - `code`: the key of exactly one message in the web localization package, never a category;
  - every other member is a param of the message (`post_id`, `name`, `offset`, `max_file_size_bytes`, ...), typed per code in OpenAPI. An optional param is left out, never `null`. 403 keeps `required` / `missing`.
- 422 (any request validation: body, query, path, form, JSON syntax, and the checks the routes make on single values) is the `validation_error` problem with `error_count` and `errors: [{code, detail, pointer | parameter + location, <params>}]` instead of FastAPI's list `[{type, loc, msg, input, ctx}]`. `pointer` is a JSON Pointer into the body (`#/username`); query, path, header and cookie values come as `parameter` + `location`. The submitted value is never echoed back. Built-in checks keep pydantic's codes (`missing`, `string_too_short`, `int_parsing`, `json_invalid`, ...); the rules of the API have their own (`username_reserved`, `value_null`, `role_ids_duplicate`, ...), without the "Value error, " prefix.
- Unknown route: 404 `route_not_found`; wrong method: 405 `method_not_allowed` (with `Allow`); unparsable form body: 400 `body_parse_error`; any other framework status: `type: about:blank` with the status phrase as `title`; any unhandled exception: 500 `internal_error` with an `error_id` that is in the server log, never the exception text. Every 401 carries `WWW-Authenticate: Bearer`.
- 403 codes split so that each names one message: `not_owner` becomes `observation_not_owner`, `media_not_owner` or `upload_session_not_owner`; the `include_deleted` check answers `permission_required_for_action` (`permission`, `action`) instead of `permission_denied`. `admin_required` reads "Administrator access required". `post_access_denied` / `post_write_denied` carry `post_id` and `resource_type`. 401 codes are unchanged (a refresh token sent as bearer still answers `invalid_token`); `session_not_found` was never sent and is gone.
- Params renamed where they clashed with RFC 9457 members or were shorter than 3 characters: `status` became `session_status` (closed upload session), `observation_status` (media of an inactive observation) and `thumbnail_status` (failed poster); `tz` became `time_zone`.
- No response carries server internals any more: uploads no longer return the service error text (it could name the temp file path or the storage error), a closed upload session no longer returns `error`, the health endpoint no longer names the client host.
- Media uploads: the 415 codes tell the reason apart (`media_type_unknown`, `media_extension_not_accepted`, `media_extension_dangerous`, `media_content_type_not_accepted`, `media_content_type_extension_mismatch`), always with `media_type`, `accepted_content_types`, `accepted_extensions`. A chunk 413 answers `upload_chunk_too_large` (`max_chunk_bytes`) or `upload_chunk_overrun` (`expected_size`) with the `offset`. A storage failure answers 500 `media_store_failed` instead of 400; too many concurrent stores answers 503 `media_uploads_busy` instead of 400 and no longer aborts a resumable session (retry `complete`). `UPLOADS.md` lists the codes per step.

### Documentation for API clients

- `/docs`, `/redoc` and `/openapi.json` document, for every operation and status, the exact problem types it can answer (`oneOf` with discriminator `code`, one component schema per problem type with its params typed, `title` / `status` / `code` as constants). They are derived from the code of the route, its helpers and its dependencies, so they cannot drift. `components.schemas.ProblemCode` and `FieldErrorCode` list every code for the localization package; `Problem` is the union of all problem types.
- `/problems` and `/problems/{code}`: HTML documentation of every problem type (status, title, detail template, params, operations that answer it); this is what `type` points to.
- operationId is the endpoint name (`update_post`) instead of `update_post_api_v1_posts__post_id__put`, so generated client functions get readable names.
- Success responses documented as they really are: `/media/{id}/download` and `/stream` (the file, 206 for Range), `/media/{id}/thumbnail` (image, or 202 with `Retry-After`), `/media/{id}/share` (202), `/observations/report` (xlsx), `PATCH /media/uploads/{id}` (204 with `Upload-*` headers), `Upload-*` and `Location` headers of the upload sessions; `UploadResultResponse` for the three upload routes, `StorageStatisticsResponse` for `/media/statistics`, `ReportTypeChoice` for `/observations/report/types`. No response is documented as JSON without a schema any more.
- `API.md`: the contract for the web client (errors, localization keys, validation pointers, 401 handling, openapi-typescript and openapi-fetch). The generated types were checked with openapi-typescript 7 and TypeScript 5 in strict mode: errors of an operation narrow by `code`, wrong params and codes fail to compile.
- New setting `app.problem_base_url` (`MYSETTINGS_APP__PROBLEM_BASE_URL`, `ENV.md`).

### Database

- No migration. Tests: `tests/test_error_contract.py` (catalog against RFC 9457, static rules, handler bodies), `tests/test_openapi.py` (generated document, real bodies validate against their problem schema), plus the updated route tests.

## Username and post code: validation errors, case, silent no-ops and 500s

### Changed for API clients

- `PATCH /users/{id}` with a body that is not `multipart/form-data` or `application/x-www-form-urlencoded` (JSON included) answers 415 with `Accept-Patch: multipart/form-data, application/x-www-form-urlencoded`. It used to answer 200 and change nothing, because the fields are form fields.
- `POST /users/`, `PATCH /users/{id}`: an empty certificate file answers 400 "Certificate file is empty" and a file over 5 MB answers 413, as documented; both answered 500 "Failed to process certificate file". `PATCH /users/{id}` with a certificate that belongs to another user answers 400 "Certificate already exists for another user" instead of 500.
- `PUT /posts/{id}`: `null` for `name`, `code` or `is_active` answers 422 ("Value cannot be null, omit the field to keep the current value") instead of 500; omit the field to keep its value. `description`, `region`, `city` still accept `null`.
- `POST /posts/`, `PUT /posts/{id}`: a `name` or `code` of spaces only answers 422 ("Value cannot be empty"); it was stored as an empty string.
- `POST /posts/`, `PUT /posts/{id}`: post code uniqueness ignores case on every database, as the MySQL unique index already did (`abc` answers 400 when `ABC` exists; a post can change only the case of its own code). A code taken by a concurrent request answers 400 "Post with this code already exists" instead of 500.
- `PUT /posts/{id}` sets `updated_at`; it kept the creation time.

- `POST /users/`, `PATCH /users/{id}`: a request that passes the form checks but fails the model rules (reserved username, characters outside `a-z A-Z 0-9 . _ -`, a value that gets too short once spaces are trimmed, duplicate or non-positive `role_ids`) used to answer 422 `{"detail": "Server side error"}`. It now answers the `validation_error` problem (see "Errors as RFC 9457 Problem Details"), e.g. `errors: [{"code": "username_reserved", "pointer": "#/username", ...}]`. A one-letter username answers `string_too_short` with `min_length: 3`.
- `POST /users/`, `PATCH /users/{id}`: `role_ids` that is valid JSON but not an array (`"5"`) answers 400 `role_ids_not_json_array` instead of 500.
- `username` keeps the case the client sent (`Ivan.Petrenko` is stored and returned as is); it was lowercased before. Existing usernames are unchanged. Uniqueness ignores case: `ivan.petrenko` answers 400 "User with this username already exists" when `Ivan.Petrenko` exists. A user can change only the case of their own username. Reserved names are rejected in any case (`ADMIN`).
- `PATCH /users/{id}`: a username taken by a concurrent request (unique index) answers 400 "User with this username already exists" instead of "Certificate already exists for another user" (MySQL) or 500 (sqlite).

### Database

- No migration. Tests: `tests/test_username_validation.py`, `tests/test_user_form_errors.py`, `tests/test_post_update.py`.

## Post presence without a stored connection status

Nothing kept the post connection fields up to date: `posts.last_connection_at` was never written, and `posts.connection_status` was only a manual value set through `PUT /posts/{id}`. Presence of a post is its home users' activity.

### Breaking changes for API clients

- Post responses (`GET /posts/`, `GET /posts/{id}`, `POST /posts/`, `PUT /posts/{id}`, `GET /search/posts`): `connection_status` and `stored_connection_status` are removed; a post has users online now when `online_user_count > 0`. `last_connection_at` is renamed `last_user_activity_at`: the latest activity of any home user (users whose `users.post_id` is the post), `null` when none has an active session.
- `PUT /posts/{id}` no longer accepts `connection_status`; the field is ignored like any unknown field.
- Post list and search filters: `connection_status` is replaced by `has_online_users` (`true`: posts with at least one home user online now, `false`: posts without); `last_connection_from` / `last_connection_to` are renamed `last_user_activity_from` / `last_user_activity_to`. `sort_by=connection_status` is removed and `sort_by=last_connection_at` is renamed `last_user_activity_at`.

### Database

- Migration `014_drop_post_connection_columns`: drops `posts.connection_status` and `posts.last_connection_at` with their indexes. Manually set values (`maintenance`, `error`) are lost; downgrade adds the columns back empty.
- Tests: `tests/test_presence.py::test_post_presence_is_derived_from_home_users`, `tests/test_migrations.py::test_014_drops_post_connection_columns`.

## Observation counters of a post

### Added for API clients

- New `GET /posts/{post_id}/observation-stats` (`post.view` + `observation.view`), the only source of observation counters of a post; post responses carry none. Everything counts by `created_at`. Response: `all_time`, `last_24h` (the 24 hours before `generated_at`) and `period` with `date_from`, `date_to`, `group_by`, `tz` and `buckets`. Every figure is a pair `not_archived` / `archived`; the two never overlap (all observations = `not_archived` + `archived`). `archived` is `null` unless `include_archived=true`. It needs no `observation.archive`: the route returns counts, never archived rows, so `observation.view` is enough.
- Query: `date_from` / `date_to` (default the last 30 days, naive values are UTC), `group_by` = `day` (default, period up to 366 days) or `hour` (up to 31 days), `tz` (IANA zone of the buckets, default the server setting `settings.timezone.timezone`, UTC unless configured), `include_archived`. `buckets` lists every bucket of the period, empty ones included, with `start` / `end` in the zone; day buckets are calendar days of the zone. 400 for a reversed or too long period or an unknown zone, 404 for a missing post.
- Scope: administrators (`admin.true`) count any post, like `GET /statistics`; everyone else needs the post in their data scope (403 `post_access_denied`).
- `last_observation_at`: `created_at` of the latest not archived observation of the post (archived ones never count, `include_archived` or not). `days_without_observations`: calendar days in the zone `tz` between the date of `last_observation_at` and the date of `generated_at` (0 today, 1 yesterday, whatever the hour). Both `null` when the post has no observation.

### Changed for API clients

- `GET /statistics/`: observations are counted by `created_at` instead of `observed_at` (`total_in_period`, `deleted_in_period`, `by_status`, `by_post`).

### Database

- Migration `013_observation_count_index`: index `ix_observations_post_deleted_created` on `observations(post_id, is_deleted, created_at)`.
- Tests: `tests/test_observation_counts.py`.

## Member count on posts

### Added for API clients

- Post responses (`GET /posts/`, `GET /posts/{id}`, `PUT /posts/{id}`, `GET /search/posts`) have a new `user_count` field: the number of active, not archived users whose home post (`users.post_id`) is the post. Users who only reach the post through `user_post_access` are not counted. `sort_by=user_count` is accepted on the post list. No database change (computed by a subquery).
- Test: `tests/test_presence.py::test_post_status_is_derived_from_home_users`.

## Post and user records for administrators; statistics and upload sessions by scope

### Changed for API clients

- Post records (`GET /posts/`, `GET /posts/{id}`, `GET /search/posts`): an administrator (`admin.true`, the routes require `post.view`) sees every post, whatever their data scope. Everyone else still sees only the posts of their data scope. This covers the post record itself (name, code, status, presence); observations, media and reports of a post keep the data scope (`post.access_all`, home post, `user_post_access`). `PUT /posts/{id}` was already open to administrators for every post and now matches what they can read.
- `GET /statistics/` no longer requires `admin.true`, only `statistics.view`. Administrators get figures over every post as before. Everyone else gets them over their data scope: posts in scope, users whose home post is in scope, observations and media (and the poster queue) of those posts. The response has a new `post_ids` field: the posts the figures cover, `null` for every post. The description of `statistics.view` is updated by seeding.
- User records (`GET /users/`, `GET /users/{id}`, `GET /search/users`): the same rule. An administrator with `user.view` sees users of every post; `post_id` filters of an administrator never answer 403. Everyone else keeps the post scope. The user management routes (`PATCH /users/{id}`, archive / restore, roles, post access) were already open to administrators for every user.
- `GET /media/uploads` with `media.manage_any`: administrators list the sessions of every post, everyone else only the sessions opened for posts of their data scope (it used to list every post). Without `media.manage_any` the list stays limited to the caller's own sessions. Working on a session (status, chunks, complete, abort) keeps requiring write access to its post.
- `POST /media/{id}/thumbnail/regenerate` is an administration route (`admin.true`), so it keeps working for media of every post.
- Tests: `tests/test_scope3.py::test_statistics_endpoint`, `tests/test_post_access.py::test_administrators_see_every_post_record_but_not_its_data`, `tests/test_post_access.py::test_administrators_see_every_user_record`, `tests/test_media_uploads.py::test_session_list_scope`.

## Several frequency bands per observation

An observation had one fixed rc set and one video set of frequency columns. Bands are now rows of `observation_frequencies` (`kind` = `rc` / `video`), any number per observation. No compatibility layer: the flat fields are gone from requests, responses, filters and reports.

### Breaking changes for API clients

- `POST /observations/create`: `frequency_min`, `frequency_max`, `frequency_center`, `video_frequency_min`, `video_frequency_max`, `video_frequency_center` are replaced by `frequencies: [{kind, frequency_min, frequency_max, frequency_center?, link_group?}]`, at least one band of any kind (422 otherwise). Per band: `frequency_min > 0`, `frequency_max > frequency_min`, 3 decimals; `frequency_center` defaults to the middle of the band (rounded to 3 decimals); `bandwidth` is always `frequency_max - frequency_min`, generated by the database. `link_group` (optional, >= 1) marks bands of one emitter inside the observation, e.g. rc and video of one drone.
- `PUT /observations/{id}` no longer changes frequencies; the removed fields are ignored like any unknown field. Use the band routes below.
- Observation responses (`/observations/*`, `/search/observations`): the eight flat fields `frequency_*`, `bandwidth`, `video_frequency_*`, `video_bandwidth` are replaced by `frequencies: [{id, observation_id, kind, link_group, frequency_min, frequency_max, frequency_center, bandwidth, is_deleted, created_at, updated_at}]`, active bands only, in creation order.
- List and search filters: `video_frequency_center_from` / `video_frequency_center_to` are removed; new `frequency_kind` (`rc` / `video`). `frequency_center_from/to`, `bandwidth_from/to` and `frequency_kind` must all hold for the same active band. `sort_by=video_frequency_center` is removed. `sort_by=frequency_center` sorts observations by the lowest center among their active bands that match the band filters (with `frequency_kind=video` it sorts by video bands). It is a correlated subquery per observation that no index can serve, so it is expensive on a large table: use it with narrowing filters and pagination.
- Report (`POST /observations/report`): one row per band; the observation columns repeat for each band. `rc_freq_part` lists rc bands, `video_freq_part` video bands, `all_fields` every band with the new columns "Тип частоти" and "Група" instead of the separate video columns. `video_frequency_center_from/to` are removed; `frequency_center_from/to` and `bandwidth_from/to` apply to the bands of the report's kind. The record counter shows bands and observations. `include_deleted` also includes archived bands.
- Report data scope: the report now applies the caller's post scope (`post.access_all` or home post plus `user_post_access`) like every other read; `post_ids` outside the scope answer 403 `post_access_denied`. It used to return every post to any administrator.

### Added

- `GET /observations/{id}/frequencies` (observation.view, post view access): bands of one observation. `include_deleted=true` (requires `observation.archive`) adds archived bands and allows an archived observation.
- `POST /observations/{id}/frequencies` (observation.view + observation.update): adds one band.
- `PUT /observations/{id}/frequencies/{frequency_id}` (observation.view + observation.update): partial update; `link_group: null` clears the group; when min or max changes without `frequency_center`, the center is recalculated. 422 when the result has `frequency_max <= frequency_min`.
- `POST /observations/{id}/frequencies/{frequency_id}/archive` and `/restore` (observation.view + observation.archive). Archive of an unknown or already archived band: 404; of the last active band: 409; restore of a band that is not archived: 409.
- All band routes follow the observation rules: owner or `observation.manage_any`, write access to the post, archived observation 404. Band changes lock the observation row (MySQL) and touch `observations.updated_at`. Audit `resource_type=observation_frequency`, actions `create`, `update`, `archive`, `restore`; observation audit rows carry the active bands in `frequencies`.
- Tests: `tests/test_observation_frequencies.py`, `tests/test_migration_012_observation_frequencies.py`.

### Database migrations

- `012_observation_frequencies`: creates `observation_frequencies` (generated `bandwidth`, CHECK constraints, indexes `(observation_id, is_deleted)`, `(kind, frequency_center)`, `(frequency_center)`), copies every rc and video set of `observations` into one band each (`link_group = 1` when an observation had both), then drops the eight columns and their indexes. It stops before any change and lists the observation ids when a set cannot become a band (a center or one bound without the min/max pair, min <= 0, max <= min). Downgrade restores the columns from the first active band of each kind; further bands are lost.
- CHECK constraints are enforced from MySQL 8.0.16 (older versions parse and ignore them; the API validates the same rules).
- Rollout: deploy API and clients together, the old request shape is rejected.

## Sessions of deactivated users expire at once

Deactivating (`PATCH /users/{id}` with `is_active=false`) or archiving a user already ended their sessions. The check is now the same wherever the flag was changed, and the client learns the real reason.

### Changed for API clients

- Every authenticated request and `POST /auth/refresh` reject a deactivated account with 401 `account_deactivated` and an archived one with 401 `account_deleted` before looking at the session, so a client sees the account status instead of `session_expired` (which used to come first when the session had been ended). A refresh attempt with those tokens fails with the same codes: do not retry, send the user to login.
- A session still live for such an account (flag changed directly in the database, restored backup) is ended on the first request or refresh carrying its token, so it stops counting towards presence (`is_online`, `online_user_count`). Audit action `force_logout` with `details.reason` `account_deactivated` / `account_deleted`.
- `PATCH /users/{id}` with `is_active=false` ends the user's sessions in the same transaction as the flag change, after every validation of the request (certificate, roles): a rejected update no longer leaves the user logged out. Re-sending `is_active=false` for an inactive user is a no-op. Deactivating your own account answers 400, like archiving it.
- Reactivation (`is_active=true`, or `POST /users/{id}/restore`) does not revive ended sessions: old tokens keep answering 401 `session_expired`, the user logs in again.
- Tests: `tests/test_user_deactivation.py`.

## Resumable uploads (bad connections)

The one-shot upload routes fail as a whole when the connection drops. A new session-based upload sends the file in chunks at explicit offsets and continues from the last stored byte after any failure. Client contract with every status and what to do on it: `UPLOADS.md`.

### Added

- `POST /media/uploads` (media.view + media.create, post write access): opens a session for `{observation_id, media_type, original_filename, expected_size, content_type?, sha256?}`; format, declared type and per-format size are checked here (415 with `Accept-Post`, 413, 404, 403). 201 with the session (`id`, `received_bytes`, `expected_size`, `max_chunk_bytes`, `expires_at`, `urls`) and headers `Upload-Offset`, `Upload-Length`, `Location`.
- `PATCH /media/uploads/{id}`: one raw chunk (`Content-Type: application/offset+octet-stream`, `Upload-Offset` must equal the session's `received_bytes`). 204 with the new `Upload-Offset`; 409 with the real offset when it disagrees or another chunk is in flight; 413 above `storage.upload.max_chunk_bytes` (64 MB) or past `expected_size`; 410 when the session is completed, aborted or expired; 400 when the transfer broke mid-chunk (the file is truncated back to the previous offset). One writer per session, enforced with a database token.
- `GET` / `HEAD /media/uploads/{id}`: current offset and status (`Upload-Offset`, `Upload-Length`, `Upload-Status` headers; JSON on GET). `GET /media/uploads?status=&observation_id=`: the caller's sessions (media.manage_any: everyone's) for resuming after a reload.
- `POST /media/uploads/{id}/complete`: size check (409 with the missing bytes), optional sha256 (422, session aborted), then the same storage, format and probe path as the other uploads (415 / 413 abort the session). 200 with the usual upload response plus `upload_id`; idempotent for completed sessions.
- `POST /media/uploads/{id}/abort`: drops the bytes (409 for completed sessions).
- Sessions belong to their creator; `media.manage_any` may operate on any. Post write access is re-checked on every call. Open, abort and completion are audited (`resource_type=upload_session`).
- Expiry: `storage.upload.session_ttl_hours` (24) after creation; the media worker sweeps every `storage.upload.sweep_interval_seconds` (600), `python -m app.backend.media_tools expire-uploads` does it on demand. Part files live under `<upload_path>/_tmp/uploads/`.
- nginx: new location for `/api/v1/media/uploads` with `proxy_request_buffering off` and `client_max_body_size 64m` (`nginx.conf`).
- Upload responses of all three routes carry `upload_id` (`null` for the one-shot routes).
- Tests: `tests/test_media_uploads.py`; the Docker e2e uploads a video through a session with a simulated interruption.

### Database migrations

- `011_upload_sessions`: creates `upload_sessions` (guarded for fresh installs).

## Archive instead of delete; one permanent delete for roles

Nothing in the API removed data, but the permissions and routes were called "delete". They are now called what they do. The single operation that really removes rows is new, applies to roles only, and has its own permission.

### Breaking changes for API clients

- Permissions renamed: `user.delete`, `post.delete`, `observation.delete`, `role.delete`, `category.delete`, `signal_type.delete`, `tag.delete`, `media.delete`, `media_format.delete` are now `user.archive`, `post.archive`, `observation.archive`, `role.archive`, `category.archive`, `signal_type.archive`, `tag.archive`, `media.archive`, `media_format.archive`. Every role keeps exactly the grants it had (migration `010_archive_permissions` renames the rows in place). `include_deleted=true` on lists, searches and the report needs the matching `X.archive`. Display names and descriptions (`GET /roles/list-all-permissions`) now say "Архівування ..." and explain that data is kept.
- `role.delete` changed meaning: it is the permanent delete of roles (display name "Остаточне видалення ролей"). The old soft-delete row is renamed to `role.archive` by the migration, so no role keeps the new permission by accident; seeding creates the new row and grants it to the admin role only.
- Removed routes: `DELETE /users/{id}`, `DELETE /observations/{id}`, `DELETE /media/{id}`, `DELETE /categories/{id}`, `DELETE /signal-types/{id}`, `DELETE /tags/{id}`, `DELETE /media-formats/{id}` (405 now). Use the archive routes below.
- `DELETE /roles/{id}` is now the PERMANENT delete (see below). Clients that used it to hide a role must call `POST /roles/{id}/archive` instead.
- `is_deleted` is no longer accepted in any request body: removed from `PATCH /users/{id}` (form field), `PATCH /roles/{id}`, `PUT /observations/{id}` and `PUT /media-formats/{id}`. Roles and media formats answer 422 (unknown field), observations ignore it. Archived rows answer 404 on update until restored.
- Audit `action` for hiding a row is `archive` (was `delete`), for bringing it back `restore`; `delete` is written only by the permanent delete of roles (with `details.permanent = true`). Old audit rows are not rewritten.
- `MediaFileInfo` gained `observation_id` and `is_deleted`. Archiving a media file no longer unlinks it from its observation (needed for restore); `media_ids` of an observation leaves archived files out, `GET /media` never lists them.

### Added

- `POST /{entity}/{id}/archive` and `POST /{entity}/{id}/restore` for users, roles, observations, media, categories, signal types, tags and media formats. Requirements: `X.view` + `X.archive`; users, roles and the dictionaries are administration routes (`admin.true`); observations and media follow the ownership rules (owner or `observation.manage_any` / `media.manage_any`, write access to the post). Both answer the entity (`is_deleted` shows the new state). Archive of an unknown or already archived row: 404. Restore of a row that is not archived: 409. Archiving your own user: 400. The admin role cannot be archived (409). A signal type with live observations cannot be archived (400). Restoring a media format re-checks extension and alias collisions (400).
- `DELETE /roles/{id}` (`role.view` + `role.delete`, `admin.true`): permanently removes a role together with its `role_permissions` rows and its `user_roles` rows. Preconditions, each 409: the role is archived; no active user holds it (`detail.assigned_users` lists them; assignments of archived users are removed with the role); the built-in `admin` role is never deletable. 204 on success; the audit row keeps the role, its permission names and the removed assignments. The name becomes free for reuse.
- Seeding refreshes `display_name` / `description` of every permission from the code on each start.
- Tests: `tests/test_roles_soft_delete.py` (archive lifecycle, permission split, permanent delete), `tests/test_migration_010_archive_permissions.py`, updated `tests/test_scope3.py`, `tests/test_media_formats.py`, `tests/test_observation_ownership.py`, `tests/test_tags_on_observations.py`.

### Database migrations

- `010_archive_permissions`: renames the nine `X.delete` permission rows to `X.archive` in place (grants preserved). If a target name already exists, the grants of the old row are merged into it and the old row is dropped. Downgrade drops the permanent `role.delete` row with its grants and renames back.

## Observation Excel report: duration in minutes

- `GET /observations/report`, report type `all_fields`: the column `Тривалість (сек)` is now `Тривалість (хв)` and holds a numeric value in minutes with one decimal (150 s -> 2.5, 45 s -> 0.8). The filter summary rows `Тривалість від/до` show the same unit. The API still stores and filters `duration_seconds` / `duration_from` / `duration_to` in seconds; only the spreadsheet presentation changed.
- Tests: `tests/test_observation_report.py`.

## Playback metadata for video and audio

A page needs to know whether the browser can play a file before it builds a player. The container type alone does not tell (an MP4 with H.265 has the same `video/mp4` as one with H.264), so the API now reads the stream headers with ffprobe and exposes the codec string `HTMLMediaElement.canPlayType()` expects.

### Added for API clients

- `MediaFileInfo` (media info, media lists, `media_files` inside observations, upload responses) gains `duration_seconds`, `video_codec`, `audio_codec` and `playback_type`. `playback_type` is the RFC 6381 string, for example `video/mp4; codecs="avc1.64001F, mp4a.40.2"` or `audio/ogg; codecs="opus"`; until a file has been probed it falls back to the bare `mime_type`, and for images it is `null`. `width` and `height` are now filled for video too. Recommended client flow: `video.canPlayType(playback_type)` before showing a player; when the answer is empty show the poster and a download button.
- Probing happens at upload (headers only, bounded, never fails the upload), during poster extraction for rows that lack it, and for existing files with `python -m app.backend.media_tools probe-missing`. Without ffprobe (Windows development with imageio-ffmpeg) the ffmpeg banner is parsed instead, which yields codecs and duration but no H.264 level.
- Tests: `tests/test_media_probe.py`; the Docker e2e checks the probed fields after a video upload.

### Database migrations

- `009_media_playback_metadata`: adds `media_files.duration_seconds`, `video_codec`, `audio_codec`, `playback_type` (all nullable, guarded for fresh installs).

## Accepted media formats (managed dictionary)

The upload allowlist is a table, `media_formats`, managed through the API like the other dictionaries. Each row is one Content-Type with its extensions, alias spellings and its own size limit in bytes. Uploads read the active rows; nothing is hard-coded any more.

### Breaking changes for API clients

- Uploads (`POST /media/upload`, `POST /media/nginx-upload`) accept only the active rows of `media_formats`. Seeded defaults: images `image/jpeg` (.jpg .jpeg), `image/png`, `image/webp`, `image/gif`, 50 MB each; video `video/mp4` (.mp4 .m4v), `video/webm`, `video/quicktime` (.mov), `video/x-matroska` (.mkv), limit `storage.max_file_size_gb`; audio `audio/mpeg` (.mp3), `audio/wav`, `audio/flac`, `audio/ogg` (.ogg .oga .opus), `audio/mp4` (.m4a), 1 GB each. Dropped from the old code list: bmp, tiff, avi, wmv, flv, raw aac, and `media_type=document`.
- A rejected format answers **415 Unsupported Media Type** (was 400) with `detail: {message, media_type, accepted_content_types, accepted_extensions}` and an `Accept-Post` header listing the accepted `Content-Type` values for that media type. Checked: the declared `Content-Type` (multipart part, or the request header on the nginx route; `application/octet-stream` means "detect from the bytes"; a declared type that belongs to another format than the extension is rejected too), the file extension, and the MIME type detected from the content.
- A file above the format's limit answers **413** with `detail: {message, content_type, max_file_size_bytes, file_size_bytes}`. `storage.max_file_size_gb` is now the server ceiling (nginx `client_max_body_size` must match); no per-format limit can exceed it.

### Added

- `GET /media/formats` (media.view): active formats grouped by media type (`id`, `content_type`, `extensions`, `label`, `aliases`, `max_file_size_bytes`), `media_types`, and `max_file_size_bytes` (the ceiling); the response also carries `Accept-Post`.
- Administration dictionary `/media-formats` (new permissions `media_format.view`, `media_format.create`, `media_format.update`, `media_format.delete`, held by the admin role): `GET /list-all` (filters `id`, `media_type`, `content_type`, `content_type_contains`, `label_contains`, `is_active`, `include_deleted` with media_format.delete; sortable by id, media_type, content_type, label, max_file_size_bytes, sort_order, is_active), `GET /filter-schema`, `POST /create`, `GET|PUT|DELETE /{format_id}`. Writes are administration routes (admin.true plus the granular permission), audited as `resource_type=media_format`. Validation: `content_type` must be lower-case `type/subtype` and start with `media_type/` (other spellings go into `aliases`); extensions are normalised to `.ext`; no extension or MIME spelling may belong to two live formats; `max_file_size_bytes` is capped by the ceiling. Soft delete; `POST /create` with a deleted `content_type` restores it, `PUT` with `is_deleted=false` (media_format.delete) too. `is_active=false` keeps a row but stops accepting it.
- `GET /search/media-formats` (media_format.view) with the same filters.
- Seeding inserts any missing default format at startup and never edits existing rows, so a fresh install and an upgraded one both get the list; changes made by administrators persist.
- Tests: `tests/test_media_formats.py`.

### Database migrations

- `008_media_formats`: creates `media_formats` (guarded for fresh installs). Rows are seeded by the application, not by the migration.

## Plain text in the API (no HTML entities)

Request validation used to run `html.escape` on every string, so names, descriptions, comments and aliases were stored and returned as `кур&#x27;єр`, `AT&amp;T`, `&quot;Північ&quot;`. The API speaks JSON: escaping for display is the client's job.

### Changed for API clients

- Every string in every response is plain text: `'`, `"`, `&`, `<`, `>` arrive as the characters the user typed. Rows that still hold entities from before this change are decoded on the way out (`PlainTextModel`, base of `SecureBaseModel`, `PostResponse`, `ObservationWithMedia`, `MediaFileInfo`, `SearchResponse`, auth responses), including nested projections and search `items`.
- `SecurityValidators.sanitize_string` no longer HTML-escapes input. Entities a client sends anyway (`&#x27;`, `&#39;`, `&amp;`, the malformed `&x27;`) are decoded before validation, so the SQL and script pattern checks see the real text. Clients must stop unescaping API values themselves, otherwise a legitimate `&amp;` typed by a user turns into `&`.

### Database migrations

- `007_unescape_html_entities`: data repair, decodes entities in `users.username_alias`, `posts.name/code/description/region/city`, `tags.name`, `categories.name/alias`, `signal_types.code/name/description`, `observations.comment`, `roles.name/display_name/description`, `media_files.original_filename`. Handles double-escaped values. A row whose decoded value would collide on a unique column is skipped and logged. No downgrade action.

### Tests

- `tests/test_plain_text.py`: decoding rules, validator behaviour, escaped rows through post, tag, observation list and search endpoints, new input stored without entities. `tests/test_migrations.py` covers the 007 repair and the unique-clash skip.

## Scope 4: media pipeline (thumbnails on demand, video posters, containers)

Thumbnails are no longer generated by the API or by the inotify + systemd watcher. imgproxy renders every size on demand from the original (images) or from a poster frame (videos) and nginx caches the result; a small media worker extracts the poster frames with ffmpeg.

### Breaking changes for API clients

- `GET /media/{id}/thumbnail` gained `size` (`small`, `medium`, `large`, `preview`; default `medium`) and `format` (`auto`, `jpeg`, `webp`; default `auto`, WebP when the client accepts it). A 202 answer (video poster not extracted yet) has an empty body, `Retry-After: 2` and an `X-Thumbnail-Status` header, and no longer triggers generation. A video whose poster extraction failed returns 409 with `{status, attempts, message}` instead of retrying forever; audio and documents return 422 as before. Images are always ready.
- `MediaFileInfo` (media info, media lists, `media_files` inside observations): `thumbnail_id` removed; added `thumbnail_status` (`ready`, `pending`, `processing`, `failed`, `unsupported`), `thumbnail_urls` (`small`, `medium`, `large`, `preview`: signed public URLs valid for `storage.thumbnail.url_ttl_seconds`, `null` until ready) and `poster_generated_at`. Put `thumbnail_urls` straight into `<img src>`: no bearer token and no per-thumbnail API call are needed.
- Upload responses (`POST /media/upload`, `POST /media/nginx-upload`) gain `thumbnail_status` and `thumbnail_urls`. Validation failures now return the real reason in `detail` (400). The multipart upload limit is `storage.max_file_size_gb` like the nginx path.
- `GET /media/{id}/download` lost the `validate_integrity` and `stream` query parameters; the file is served by nginx (X-Accel-Redirect) or a Range-capable `FileResponse`. `GET /media/{id}/stream` works for every media type and no longer returns 501 when nginx is disabled.
- `POST /media/{id}/thumbnail/regenerate` returns `{media_id, thumbnail_status, message}`; for images it is a no-op.
- `has_thumbnail` filter on media lists: images always match `true`; videos match `true` once the poster is ready.
- `GET /statistics/`: `media.posters` with counts by poster status.

### Added

- `POST /media/{id}/share` (media.view, post scope): `{size, format, ttl_seconds}` returns `{url, expires_at}`, a signed public thumbnail or preview URL that expires (capped by `storage.thumbnail.share_max_ttl_seconds`). Audited.
- `GET /media/posters/status` (administration, media.view): poster queue depth by status, worker heartbeats, `worker_alive`.
- `POST /media/posters/reconcile` (administration, media.view + media.update): `{retry_failed}` re-queues videos whose poster is missing or untracked, optionally the failed ones. Audited.
- Media worker `python -m app.backend.workers.media_worker`: claims pending videos, runs ffmpeg with a timeout, writes posters atomically, retries with backoff, reconciles on start, heartbeats to `worker_heartbeats`, stops cleanly on SIGTERM.
- Maintenance commands `python -m app.backend.media_tools cleanup-legacy-thumbnails | reconcile-posters | poster-status`.
- Deployment: `Dockerfile` (API and worker from one image), `docker-compose.yml` (application services only: api, media-worker, imgproxy on the host network; MySQL, nginx and the media directory stay on the host), `nginx-http.conf` (cache zone and upstreams) and rewritten `nginx.conf` for the host nginx (`/_thumb/` internal and `/t/` public cached locations), `systemd/watch-web-media-worker.service`, `DEPLOYMENT.md`.
- `requirements.txt` is now plain ASCII (it was UTF-16 with a corrupted tail) and no longer lists PyAV; `imageio-ffmpeg` added for Windows development.
- Settings: `storage.temp_path`, `storage.ffmpeg_path`, `storage.ffprobe_path`, `storage.imgproxy.*`, `storage.poster.*`; `storage.thumbnail` gained `preview_size`, `url_ttl_seconds`, `share_max_ttl_seconds`, `prewarm`. See `ENV.md`.
- Tests: `tests/test_imgproxy_signing.py`, `tests/test_media_thumbnails.py`, `tests/test_poster_jobs.py`, `tests/test_posters_ffmpeg.py` (skips without ffmpeg), legacy upgrade check in `tests/test_migrations.py`.
- End-to-end stack `tests/docker/compose.test.yml` with `tests/docker/e2e.py`: uploads through multipart and nginx offload, thumbnails through nginx (cache MISS then HIT, WebP negotiation), signed public URLs and tamper rejection, video poster extraction by the worker, Range downloads, the nginx-less proxy path.

### Removed

- `app/backend/services/thumbnail_generator.py`, `thumbnail_worker.py`, `systemd/media-watcher.*`, `systemd/thumbnailgen@.service`, `NGINX_THUMBNAIL_GUIDE.md`, the nginx symlink directory (`/protected_media`) and PyAV.
- Settings `storage.external_thumbnail_generation`, `storage.nginx_symlink_path`, `storage.thumbnail.enabled`, `max_concurrent`, `format`, `video_frame_position` (now `storage.poster.frame_position`).

### Changed

- Startup migrations and seeding are serialised across uvicorn workers with a MySQL advisory lock (`GET_LOCK('watch_web_schema_init')`). Before this, two workers ran Alembic at once and MySQL rejected one with a concurrent DDL error, which surfaced as a failed worker start.
- The application image sets `USER`, `LOGNAME` and `HOME` so the containers can run as any uid (the media directory owner) without an `/etc/passwd` entry; aiomysql otherwise fails on `getpass.getuser()`.

### Database migrations

- `005_video_posters`: adds `media_files.poster_status`, `poster_attempts`, `poster_locked_until`, `poster_worker`, `poster_generated_at`, `poster_error`, index `ix_media_files_poster_status`, table `worker_heartbeats`; queues every existing video (`poster_status = pending`). Guarded for fresh installs.
- `006_drop_legacy_thumbnails`: drops `media_files.thumbnail_path` and `thumbnail_error`.

### Rollout order

1. Install the new host nginx configuration (`nginx-http.conf` + `nginx.conf`, see `DEPLOYMENT.md`); set `IMGPROXY_KEY` / `IMGPROXY_SALT` and the matching `MYSETTINGS_STORAGE__IMGPROXY__*` values; start the imgproxy container.
2. Migrate (`python -m app.backend.migrations_manager migrate`), or let the API apply migrations on start.
3. Start the media worker (compose service `media-worker` or the systemd unit); it regenerates posters for every existing video.
4. Deploy the API. Remove the old `media-watcher` and `thumbnailgen@` units.
5. Run `python -m app.backend.media_tools cleanup-legacy-thumbnails` to delete the old `*_thumb_*.jpg` files.

## Scope 3: session expiry, soft delete of observations and users, statistics, dead routes

### Breaking changes for API clients

- Sessions now expire: a request or refresh with a session older than `security.session_expire_hours` (8 h) returns 401 `session_expired` and the session is closed. Previously the session lifetime was never enforced and a session lived as long as its refresh token.
- `include_deleted=true` on list and search endpoints (observations, report, users, roles, categories, tags, signal types) requires the matching delete permission (`observation.delete`, `user.delete`, ...), otherwise 403 `permission_denied` with the missing permission.
- Removed never-implemented route members: `AuthRoutes.CHANGE_PASSWORD`, `AuthRoutes.CERTIFICATE_VALIDATE`, the whole `ExportRoutes` and `SignalRoutes` groups, `PostRoutes` status/bulk/location/statistics/activity/regions/maintenance members and `ObservationRoutes` bulk-update/statistics. The `ExportJob` table stays untouched.
- `UserResponse` and the nested creator projections gain `is_deleted`.

### Added

- `DELETE /observations/{id}`: soft delete (observation.view + observation.delete, write access to the post, owner or observation.manage_any). Media stay attached. Restore with `PUT /observations/{id}` and `is_deleted=false`; changing `is_deleted` through the update requires observation.delete, and deleted observations cannot be edited otherwise (404).
- `DELETE /users/{id}` (administration, user.view + user.delete): soft delete, deactivation and session invalidation; own account cannot be deleted. Restore with `PATCH /users/{id}` form field `is_deleted=false` (re-activates the user); changing `is_deleted` requires user.delete.
- `PUT` and `DELETE /roles/{id}/permissions/{permission_id}`: grant or revoke a single permission (administration, role.view + role.update), audited, returning the role's permissions.
- `GET /statistics/` (administration, new permission `statistics.view`, held by the admin role): posts total/active/online, users total/active/online/deleted, observations in a period (default last 30 days) by status and by post, deleted observations in the period, media count and total size. Optional `date_from` / `date_to`.
- Tests: `tests/test_scope3.py`.

### Security

- Session lifetime is enforced on every authenticated request and on refresh.

## Scope 2: presence, role soft delete, deleted tags, assignment uniqueness

### Breaking changes for API clients

- Posts are returned as `PostResponse` instead of the raw row: `connection_status` is now the effective status (`online` while at least one home user is online, otherwise the stored value), `stored_connection_status` carries the stored value, `last_connection_at` is the latest of the stored value and the home users' activity, and `online_user_count` is new. Search results for posts use the same shape.
- `PUT /posts/{id}` accepts `connection_status` with `offline`, `maintenance` or `error` only; `online` is rejected (422) because it is derived from presence.
- Filtering posts by `connection_status=online` matches posts with online users; other values match the stored status of posts without online users.
- `DELETE /roles/{id}` is a soft delete: the role disappears from lists, stops granting permissions immediately, cannot be assigned (400) and cannot be deleted again (404). `GET /roles/{id}`, `PATCH /roles/{id}` and `GET /roles/{id}/permissions` still work for deleted roles so they can be inspected and restored. Creating a role with the name of a deleted role returns 409.
- `GET /users/{id}/roles` and `/auth/me` no longer list deleted roles.
- Observation responses gain `tags: [{id, name, is_deleted}]` next to `tag_ids`. Updating an observation may keep already attached deleted tags; adding a deleted tag that was not attached is still rejected.
- Duplicate rows in `role_permissions` and `user_roles` are rejected by unique indexes.

### Added

- User presence: every authenticated request refreshes the session's `last_activity` (at most once per `security.activity_touch_interval_seconds`, default 60). A user is online when an active session was touched within `security.online_threshold_seconds` (default 300). Exposed as `is_online` and `last_activity_at` on `UserResponse` and on the nested `user` / `disappeared_by_user` projections of observations and media; `last_activity_at` and `online_threshold_seconds` on `/auth/me`.
- `GET /users?is_online=true|false` filter (also in the users search entity) and sorting by `last_activity_at`.
- Post presence: `online_user_count`, effective `connection_status`, derived `last_connection_at`; sorting by `online_user_count`.
- `GET /roles?include_deleted=true` and `PATCH /roles/{id}` with `is_deleted` to soft-delete or restore a role. `RoleResponse.is_deleted`.
- Observation report: deleted tags are marked with " (видалено)" in the tags column, and the header lists tag filters as names with ids instead of bare ids.
- Tests: `tests/test_presence.py`, `tests/test_roles_soft_delete.py`, `tests/test_tags_on_observations.py`, migration 004 checks in `tests/test_migrations.py`.

### Changed

- Seeding restores the `admin` role if it was soft-deleted.
- `SimpleFilterEngine` accepts mapper-level column properties (presence values) as sortable fields.

### Database migrations

- `004_role_soft_delete`: adds `roles.is_deleted` (default false, indexed), removes duplicate `role_permissions` / `user_roles` rows keeping the oldest, and creates unique indexes `uq_role_permissions_role_permission` and `uq_user_roles_user_role`. Every step is guarded so it is a no-op on fresh installs where `002_sync_schema` already created these objects.


### Breaking changes for API clients

- `admin.true` no longer satisfies other permission checks. A user holding only `admin.true` is rejected on every route that requires a granular permission (403 `permission_denied` with the missing list). The seeded admin role holds every permission, so seeded admins are unaffected; custom roles must be reviewed.
- Administration routes (roles, permission catalogue, user create/update/roles/post-access, create/update/delete of categories, tags, signal types and posts, media storage statistics, thumbnail regenerate, observation report) now require `admin.true` in addition to the granular permissions (403 `admin_required` otherwise).
- Write routes require the matching view permission as well: `X.view` plus `X.create` / `X.update` / `X.delete`.
- Cross-post visibility now comes from the `post.access_all` permission and the `user_post_access` table, not from `admin.true`. Roles that relied on the admin bypass need `post.access_all` granted explicitly.
- Posts list and `GET /posts/{post_id}` are limited to the caller's post scope; other posts return 403 `post_access_denied`.
- A `post_id` filter outside the caller's scope on list and search endpoints returns 403 `post_access_denied` instead of being silently replaced by the caller's own post.
- `PUT /observations/{id}` and `POST /observations/{id}/disappeared` require ownership or `observation.manage_any` (403 `not_owner`). `DELETE /media/{id}` requires ownership or `media.manage_any`; deleting media of a non-active observation returns 403 `observation_not_active` unless `media.manage_any` is held.
- `GET /users/{user_id}` is allowed for the user itself and for users whose post is inside the caller's scope (403 `user_self_only` otherwise). `GET /roles/{role_id}` is administration only.
- 403 bodies are structured: `{"detail": {"code", "message", "required": {"all", "any", "admin"} | null, "missing": [...]}}`. Free-text 403 strings are gone.
- Refresh tokens are rejected as bearer tokens (401 `invalid_token`).
- Login, refresh and `GET /auth/me` responses gained `permissions`, `roles`, `is_admin`, `post_id` and `accessible_post_ids` (`null` means every post).
- `POST /observations/create` and `PUT /observations/{id}` return the same shape as the list (`ObservationWithMedia`) instead of the bare row, including `media_ids`, `tag_ids`, `user` and `disappeared_by_user`.
- Observation and media responses embed the creator as `user: {id, username, username_alias}` (`disappeared_by_user` on observations).
- Category DELETE requires `category.delete` (was `category.create`), signal type DELETE requires `signal_type.delete` (was `signal_type.update`), media search requires `media.view` (was `observation.view`).

### Added

- Step 5: `AuthContext` (permissions, roles, readable and writable post ids) loaded once per request by `get_auth_context`; all checks read it, no per-check queries.
- Step 6: `require(all=[...], any=[...], admin=True)` FastAPI dependency and `Require` specification (`app/backend/services/permissions.py`).
- Step 6: route access map generated at startup from the `require()` dependencies (`app/backend/services/route_access.py`).
- Step 7: context based `AccessControlService`: `resolve_post_scope`, `check_post_view`, `check_post_write`, `check_observation_ownership`, `check_media_ownership`, `check_user_visibility`.
- Step 9: `POST /api/v1/auth/check-permission` evaluates a requirement for the caller (`allowed`, `missing`, `admin_required`).
- Step 9: `GET /api/v1/auth/route-access` lists only the routes the caller may call, with their requirements.
- Step 9: `GET /api/v1/users/{user_id}/post-access`, `PUT` and `DELETE /api/v1/users/{user_id}/post-access/{post_id}` manage extra post access (`can_write`).
- Step 9: `UserPublicResponse` projection (`id`, `username`, `username_alias`) embedded as `user` / `disappeared_by_user` on observations and `user` on media.
- Step 11: pytest suite under `tests/` (in-memory sqlite, no MySQL needed): Require unit tests, auth contract, route access map, table-driven access matrix over every guarded route, post scope, ownership, seed re-grant, migration chain. Run with `venv\Scripts\python -m pytest`.
- Step 1: permissions `post.access_all` (cross-post data scope), `observation.manage_any` (edit observations of other users), `media.manage_any` (delete media of other users or of non-active observations).
- Step 1: `AccessErrorCode` enum for HTTP 403 bodies: `permission_denied`, `admin_required`, `post_access_denied`, `post_write_denied`, `no_post_assigned`, `not_owner`, `user_self_only`, `observation_not_active`.
- Step 1: route enum members `AuthRoutes.ROUTE_ACCESS`, `UserRoutes.USER_POST_ACCESS`, `UserRoutes.USER_POST_ACCESS_BY_POST` and literal `SearchRoutes` members for every search entity.
- Step 2: `UserPostAccess` model (`user_post_access` table): extra posts a user may read, optionally write (`can_write`).

### Changed

- Step 4: 401 and 403 bodies share the `ErrorResponse` schema (`detail.code`, `detail.message`, `detail.required`, `detail.missing`) and are documented on every router in OpenAPI.
- Step 5: `get_current_user` rejects tokens whose `type` is not `access`.
- Step 8: filter services take `access_post_ids` (a set of post ids) instead of a single `access_post_id`; observation and media filters eager-load the creator relationships.
- Step 9: every route declares its requirement with `Depends(require(...))`; the per-route table lives in the plan and in the route files themselves.
- Step 9: search routes use `SearchRoutes` enum members instead of string literals and share their requirement with the search filter schema endpoint.
- Step 3: seeding extracted into `seed_roles_and_permissions(session)`. On every startup (and `migrations_manager seed`) the admin role is re-granted every permission it does not hold, so administrative control cannot be lost permanently through role edits.

### Fixed

- Step 9: category DELETE, signal type DELETE and media search checked the wrong permission.
- Step 9: observation update and mark-disappeared had no ownership check.
- Step 3: the admin role could permanently lose permissions removed through `PATCH /roles/{id}`.

### Removed

- Step 10: `PermissionService` and its decorators (`require_permission`, `require_permissions`, ...), the admin-bypass methods of `AccessControlService` (`check_post_membership`, `check_resource_access`, `check_ownership_or_admin`, `check_combined_access`, `filter_query_by_post_access`, `check_role_membership`, `check_user_self_or_admin`, `get_user_post_filter`, `is_post_member`), and the unused `get_current_active_user` dependency.
- Step 1: unused `SearchRoutes.ENTITY` (`/{entity}`), replaced by explicit members.

### Database migrations

- Step 0: `001_relativize_media_paths` now skips itself when the `media_files` table does not exist yet (fresh installs).
- Step 0: new revision `002_sync_schema` reconciles any database with the current models: creates missing tables, adds missing columns (NOT NULL columns without a server default are added as NULLABLE and reported), creates missing indexes and unique indexes. It never drops or alters existing objects; downgrade is a no-op. Verified on the development MySQL database (no missing objects) and on an empty sqlite database (full schema created, second run is a no-op).
- Step 2: new revision `003_add_user_post_access` creates the `user_post_access` table with unique (user_id, post_id).
- Step 0: `migrations/env.py` honours the `WATCH_WEB_ALEMBIC_URL` environment variable so migrations can be run against another database (used by tests).

### Security

- `admin.true` is a gate for the administration area only. Data scope and ownership are separate permissions, so a role can be given administration access without cross-post data access and the other way round.
- Refresh tokens can no longer be used as access tokens.
- 403 responses no longer leak internal enum names in free-text messages.

### Production rollout

1. Back up the database.
2. Deploy the code and run `python -m app.backend.migrations_manager migrate` (applies `002_sync_schema` then `003_add_user_post_access`; both are additive). Starting the application does the same.
3. Run `python -m app.backend.migrations_manager seed` (or rely on startup) so the three new permissions exist and the admin role is re-granted everything.
4. Review custom roles: grant `post.access_all`, `observation.manage_any` or `media.manage_any` where the old admin bypass was relied on.
5. Verify `GET /api/v1/auth/me` for an administrator shows `is_admin: true` and the full permission list.
