# Authorization v2: user types, scope hierarchy, built-in roles - design

Status: approved in conversation section by section (2026-10-11); this document awaits review.
Sub-project A of two. Sub-project B (PKI: issuing and managing client certificates from a configured CA) gets its
own spec after this one.

## Goal

Make access easy to hand out and easy to reason about:

- an administrator gives a person a **user type** (operator, analyst, ...) and the person gets a working set of
  permissions without assembling 40 permissions by hand;
- the web client splits **pages by user type** and enables **buttons by permissions**;
- access is granted **at a node of a hierarchy** (whole system, a group of posts, a post), Azure style, so a post
  administrator can run their own part of the organisation without seeing or touching the rest;
- `admin.true` disappears: administration is granular permissions, like in Kubernetes, Azure, GitLab and Keycloak.

Success:

- every route checks only its own permissions; no route depends on a global admin flag;
- a person's type always tells the truth about what they can be given (the ceiling);
- existing users keep exactly the access they had wherever the new model can express it; every difference is
  reported before and after the upgrade;
- a delegated administrator can never grant more than they hold, at the node where they hold it.

## Decisions taken

| Question | Decision |
|----------|----------|
| How presets live | Built-in roles owned by the code, synced at every start (like the `admin` role today); not editable, archivable or deletable through the API; can be cloned into custom roles |
| Order of sub-projects | A (this spec) first, then B (PKI) |
| Presets wanted | `viewer`, `operator`, `senior_operator`, `analyst`, `post_admin`, plus the existing `admin` |
| `admin.true` | Removed. Granular permissions everywhere (industry practice, see Background) |
| Page split in the web | Explicit `user_type`, exactly one per user, fixed list in code |
| Ceiling | Each type has a ceiling defined in code and published by the API (`GET /user-types`); going above it means changing the type |
| Type change on downgrade | 409 `user_type_ceiling_exceeded` with the offending assignments; nothing removed silently |
| Data scope | Azure-style role assignments: user + role + scope node, inherited down the tree |
| Hierarchy | Nested groups of posts (`/` > group > subgroup > ... > post), at most 6 group levels |
| Delegated administration | Yes, within the scope: a `post_admin` of a group manages the users whose home post is in that group and assigns roles only inside it |
| Home post (`users.post_id`) | Membership only ("where the person belongs", who manages them); grants no access by itself |
| Production data | Exists, few users: automatic conversion, dry-run report before the upgrade, manual fixes after |
| API contract | Break v1 directly (contract file regenerated, CHANGELOG entry); no v2 |

## Background: how large systems do it

- Kubernetes RBAC: no admin flag; administration is permissions on `roles` and `rolebindings`. Built-in user-facing
  roles (`admin`, `edit`, `view`) are reconciled with the code at every API server start. A role binding can be
  created only by someone who holds every permission of the role at the same scope (or has the explicit `bind`
  verb). https://kubernetes.io/docs/reference/access-authn-authz/rbac/
- Azure RBAC: a role assignment is principal + role definition + scope; scopes form a hierarchy (management group,
  subscription, resource group, resource) and assignments inherit downward; access is the sum of assignments.
  Control plane (`Actions`) is separate from data plane (`DataActions`): an Owner manages a storage account but
  cannot read its blobs. Delegated role assignment can be constrained to some roles and principal types.
  https://learn.microsoft.com/en-us/azure/role-based-access-control/role-definitions,
  https://learn.microsoft.com/en-us/azure/role-based-access-control/delegate-role-assignments-overview
- AWS managed policies and GCP predefined roles: owned by the vendor, updated by the vendor, not editable;
  customers copy them into custom roles.
- GitLab (custom admin roles, 17.9 / 18.3) and Keycloak (fine-grained admin permissions v2, 26.2) split the single
  "administrator" into granular admin permissions.
- Salesforce: every user has exactly one profile (the baseline, apps and pages) and a user license that caps what
  the user may be given; extra access comes from permission sets. This is the model of `user_type` here.

## 1. Concepts

```
user_type (exactly one per user, fixed list in code)
   |-- web: which PAGES to show                      (coarse)
   |-- floor: the built-in base role of the type (the preset)
   |-- ceiling: the permissions the user may hold at all
role assignment = user + role + scope node
   |-- API: what the user may DO, and WHERE          (every route checks only permissions)
   |-- web: which BUTTONS are enabled on a page      (fine)
scope tree
   /                          system: dictionaries, roles, formats, group structure, audit
   +-- group "East"
   |   +-- group "Kharkiv sector"
   |   |   +-- post 7
   |   |   +-- post 8
   |   +-- post 12
   +-- group "West"
       +-- post 9
```

- An assignment at a node applies to the node and everything below it, posts added later included.
- Access is the union of all assignments (additive, no deny).
- The home post of a user is membership: it decides which delegated administrators manage the user, nothing else.

## 2. Data model

| Table / column | Definition |
|----------------|------------|
| `scope_groups` (new) | `id`, `name` VARCHAR(255), `parent_id` (FK `scope_groups.id`, NULL = directly under `/`), `parent_key` INT NOT NULL (= `parent_id` or 0, so the unique index works on MySQL where NULLs never collide), `description` VARCHAR(1000) NULL, `is_deleted`, `created_at`, `updated_at`. Unique (`parent_key`, `name`). Depth at most 6 groups. A group is never moved into its own subtree |
| `posts.group_id` (new) | FK `scope_groups.id`, NULL = directly under `/` |
| `role_assignments` (new, replaces `user_roles`) | `id`, `user_id` FK, `role_id` FK, `scope_group_id` FK NULL, `scope_post_id` FK NULL (both NULL = `/`, never both set: CHECK), `scope_key` VARCHAR(24) NOT NULL (`root`, `g:<id>`, `p:<id>`, written by the code), `created_at`, `created_by` (user id, NULL for migration and seeding). Unique (`user_id`, `role_id`, `scope_key`) |
| `users.user_type` (new) | VARCHAR(32) NOT NULL, values of `UserType` (`enums/general.py`); no DB CHECK, a new type is a new enum value (like `FrequencyKind`) |
| `roles.builtin_key` (new) | VARCHAR(50) NULL, unique; set on built-in roles only |
| `user_roles`, `user_post_access` | Renamed to `user_roles_v1`, `user_post_access_v1` by the migration (kept for an exact downgrade), dropped by a later cleanup revision once the new model runs in production |
| `users.post_id` | Unchanged column, new meaning: membership only |

Models live in `app/backend/models/` (`scope_group.py`, `role_assignment.py`); `UserRole` and `UserPostAccess`
models go away with their tables. New tables are listed in `TABLES_OWNED_BY_LATER_REVISIONS` of
`002_sync_schema_with_models.py`; columns and indexes are guarded with inspector checks.

## 3. Permission catalog and levels

Every permission has a level, defined in code next to `PERMISSION_META` (`bootstrap/permissions.py`):

- `system`: assignable only at `/`. Manages things that belong to the whole system.
- `scoped`: assignable at any node. Its effect is limited to the subtree, except permissions on objects that have
  no post (role definitions, dictionaries, formats), which act wherever they are held ("held anywhere").

A role that holds at least one `system` permission can be assigned only at `/` (409 `role_requires_root_scope`).

| Permission | Level | Effect at a node |
|------------|-------|------------------|
| `user.view`, `user.create`, `user.update`, `user.archive` | scoped | users whose home post is inside the node; at `/` every user, users without a home post included |
| `post.view`, `post.create`, `post.update`, `post.archive` | scoped | post records inside the node (`create`: a new post inside the node) |
| `observation.view/create/update/archive`, `observation.manage_any` | scoped | observations of posts inside the node |
| `media.view/create/update/archive`, `media.manage_any` | scoped | media of posts inside the node |
| `statistics.view`, `export.create` | scoped | aggregates and exports over posts inside the node |
| `role.view` | scoped | role definitions (held anywhere) |
| `category.view`, `signal_type.view`, `tag.view`, `media_format.view` | scoped | dictionaries and formats (held anywhere) |
| `scope_group.view` | scoped | groups inside the node |
| `role.create/update/archive/delete` | system | role definitions |
| `category/signal_type/tag.create/update/archive` | system | dictionaries |
| `media_format.create/update/archive` | system | accepted upload formats |
| `scope_group.create/update/archive` (new) | system | the group structure |
| `media.monitor` (new) | system | `GET /media/storage-statistics`, `GET /media/posters-status` (protected only by the admin gate before) |
| `audit.view` | system | audit log |

Removed: `admin.true` (replaced by the `admin` type and granular permissions), `post.access_all` (replaced by an
assignment at `/`). New permissions get Ukrainian display names in `PERMISSION_META`; seeding inserts them.

## 4. User types and built-in roles

Defined once in a new module `app/backend/auth/user_types.py`: `UserType`, base role (built-in role key), ceiling,
display name, description. Built-in roles carry `builtin_key` equal to the type key and are fully synced by seeding
at every start (missing permissions added, extra ones removed, display texts refreshed). The API refuses to rename,
change the permissions of, archive or delete a built-in role (409 `builtin_role_*`); `POST /roles/{id}/clone` makes
a custom copy.

Sets used below:

- `VIEW` = `post.view`, `observation.view`, `media.view`, `category.view`, `signal_type.view`, `tag.view`

| Type | Base role (floor) | Ceiling = base plus |
|------|-------------------|---------------------|
| `viewer` | `VIEW` | `statistics.view`, `export.create` |
| `operator` | `VIEW` + `observation.create/update/archive`, `media.create/archive`, `media_format.view` | `observation.manage_any`, `media.manage_any`, `statistics.view`, `export.create`, `tag.create/update` |
| `senior_operator` | operator + `observation.manage_any`, `media.manage_any`, `statistics.view`, `export.create` | `media.update`, `tag.create/update`, `user.view` |
| `analyst` | `VIEW` + `statistics.view`, `export.create` | `user.view`, `scope_group.view` |
| `post_admin` | senior_operator + `media.update`, `post.create/update/archive`, `user.view/create/update/archive`, `role.view`, `scope_group.view` | `tag/category/signal_type.create/update` (system: only at `/`) |
| `admin` | every permission | every permission |

Rules:

- Every user has exactly one type. Every assignment's role must fit within the type's ceiling (409
  `role_exceeds_user_type` with `user_type` and `excess_permissions`).
- `GET /user-types` publishes the catalog: `key`, `display_name`, `description`, `base_role` (`id`, `name`,
  `display_name`), `base_permissions`, `allowed_permissions` (the full ceiling, a superset of the base).
- Creating a user creates one default assignment: the base role at the home post, or at `/` when there is no home
  post or the base role holds `system` permissions (`admin`).
- Changing the type (`PATCH /users/{id}` with `user_type`) replaces the role in every assignment of the old base
  role with the new base role at the same node, and ends the user's sessions (new `SessionRevokeCause.USER_TYPE_CHANGED`)
  so the web never shows pages of the old type. A downgrade whose other assignments exceed the new ceiling answers
  409 `user_type_ceiling_exceeded` with `user_type` and `assignments: [{id, role, scope, excess_permissions}]`.

## 5. Evaluating access

### AuthContext v2 (`auth/context.py`)

Loaded once per request, three queries: the user's assignments with role and permissions (archived roles left out,
as today), all groups (`id`, `parent_id`, `is_deleted`), all posts (`id`, `group_id`, `is_deleted`). Everything below
is computed in memory:

| Method | Meaning |
|--------|---------|
| `has_anywhere(p)` | holds `p` at some node: route reachability, objects without a post, the web menu |
| `has_at_root(p)` | holds `p` at `/` |
| `posts_with(p)` | posts where `p` is in effect: `None` = every post (held at `/`); otherwise every post below the groups holding `p` plus the posts holding `p` directly |
| `has_at_post(p, post_id)` | `p` is in effect at that post |
| `covers(p, node)` | the node lies inside a subtree where `p` is held (delegation) |
| `covers_user(p, user)` | `p` is in effect at the user's home post, or at `/` for a user without one |

Effective permissions at a post = assignments at `/`, at every non-archived ancestor group, and at the post itself
when the post is not archived. Assignments at an archived group do not apply (a group can only be archived when
empty, so this only matters after a restore).

### Routes

- `require(all=[...], any=[...])` checks "held anywhere"; the `admin=` parameter is removed. 403
  `permission_denied` as today; `admin_required` is removed.
- The precise check happens in the handler through `AccessControlService`, which now takes the permission:
  - `post_scope(auth, p)` and `resolve_post_scope(auth, p, requested_post_id)` for lists;
  - `check_post(auth, p, post_id, resource_type)` for single resources: 403 `post_access_denied` for `*.view`
    permissions, `post_write_denied` for the others, with `required`;
  - `check_user(auth, p, target)`: `covers_user`, or the target is the caller for `user.view`.
- The old readable / writable split (`can_write`) becomes per permission: `observation.update` may be in effect at
  post 7 while only `observation.view` is at post 9.
- `post_record_scope`, `resolve_record_scope` and `check_post_record_view` are removed: post records follow
  `post.view`, user records follow `user.view`, data follows the data permissions (the Azure control / data split
  falls out of the permissions; no administrator exception).
- Statistics: over `posts_with(statistics.view)`. Upload list with `media.manage_any`: over
  `posts_with(media.manage_any)`. Ownership overrides (`observation.manage_any`, `media.manage_any`) apply at the
  posts where they are in effect.

### Archived records

- Archived post: assignments made directly on it stop applying (like an access row today); assignments inherited
  from a group or `/` keep read access to its data. Writes to its data answer 409 `post_archived`, except archive
  and restore of observations by a holder of `post.archive` at that post (before: `admin.true` + `post.access_all`).
- `include_deleted` needs `X.archive` held anywhere (`check_include_deleted`), and archived rows are shown only in
  the posts of `posts_with(X.archive)`.
- An archived post or user record is visible to holders of `X.view` and `X.archive` covering it (before: also to
  any `admin.true`).

## 6. Delegation rules (`auth/privileges.py`)

1. **Assignment** of role R to user U at node N (create or delete): the caller holds every permission of R at N
   (403 `grant_exceeds_own_permissions`, as today); R fits U's ceiling (409 `role_exceeds_user_type`); if R holds
   `system` permissions, N is `/` (409 `role_requires_root_scope`); the caller holds `user.update` covering U.
2. **Changing a user** (fields, home post, sessions, archive state, type, assignments): the caller holds the
   relevant `user.*` covering U, and could grant every existing assignment of U (holds the role's permissions at
   its node); otherwise 403 `target_user_more_privileged`, as today. Moving the home post needs `user.update`
   covering both the old and the new post.
3. **Changing a custom role** (`role.*` are `system`, so only holders at `/`): `check_grant` on the permissions
   added or removed, as today.
4. **Changing the type**: rule 2, plus the caller holds every permission of the new base role at the nodes of the
   assignments being changed, plus the ceiling check of section 4.
5. **Last administrator**: after any change at least one active, non-archived user holds the built-in `admin` role
   at `/` (409 `last_admin_required`); a caller cannot remove it from themselves (409 `cannot_remove_own_admin`).
6. **At least one assignment**: deleting the last assignment of a user answers 409 `last_assignment_required`.
7. Assignments to an archived group or post are refused (404 `scope_group_not_found` / `post_not_found`).

## 7. API changes (v1, breaking)

Every new path is first a member of its group in `enums/routes.py`; literal paths are registered before
`/{id}` paths.

### New

| Route | Requirement | Behaviour |
|-------|-------------|-----------|
| `GET /user-types` | `require()` | the type catalog of section 4 |
| `GET /scope-groups/tree` | `scope_group.view` | the subtrees the caller may see, with their posts (`post.view`), for the scope picker |
| `GET /scope-groups/` | `scope_group.view` | flat list, filters `parent_id`, `include_deleted` (query model + criteria) |
| `POST /scope-groups/` | `scope_group.view` + `scope_group.create` | `name`, `parent_id`, `description`; 409 `scope_group_name_taken`, `scope_group_depth_exceeded` |
| `GET /scope-groups/{group_id}` | `scope_group.view` | one group |
| `PATCH /scope-groups/{group_id}` | `scope_group.view` + `scope_group.update` | rename, move (`parent_id`); 409 `scope_group_cycle`, `scope_group_depth_exceeded`, `scope_group_name_taken` |
| `POST /scope-groups/{group_id}/archive`, `/restore` | `scope_group.view` + `scope_group.archive` | archive only an empty group (no active posts or subgroups, 409 `scope_group_not_empty`) |
| `GET /users/{user_id}/assignments` | `user.view` covering the user | assignments with `role` (`id`, `name`, `display_name`, `is_builtin`) and `scope` (`kind`: root / group / post, `id`, `name`) |
| `POST /users/{user_id}/assignments` | `user.view` + `user.update` | body `{role_id, scope: {kind, id}}`; rules of section 6; 409 `assignment_exists` |
| `DELETE /users/{user_id}/assignments/{assignment_id}` | `user.view` + `user.update` | removes the link row (audited like post-access rows today); 409 `last_assignment_required` |
| `POST /roles/{role_id}/clone` | `role.view` + `role.create` | custom copy of any role with a new `name`, `display_name`, optional `description` |

### Removed

`GET /users/{id}/roles`, `/users/{id}/post-access` and `/users/{id}/post-access/{post_id}`, the `role_ids` field of
the user forms, `is_admin` and `accessible_post_ids` in auth responses, `admin_required` in check-permission.

### Changed

- **Users**: `POST /users/` (form) takes a required `user_type` and an optional `post_id`; the default assignment is
  created (section 4). `PATCH /users/{id}` takes `user_type`. `UserResponse` gains `user_type`; the list gains a
  `user_type` filter (query model and criteria, `tests/test_filter_criteria.py`).
- **Roles**: `RoleResponse` gains `is_builtin`, `builtin_key`, `level` (`system` when the role holds a `system`
  permission, otherwise `scoped`); the list gains an `is_builtin` filter; `PermissionResponse` gains `level`.
- **Posts**: `group_id` in the response and in create / update. Create needs `post.create` covering the target
  group; a move needs `post.update` covering both the old and the new location and is audited.
- **Auth**: `/auth/me` and the login response carry `user_type`, `post_id` (home post), `permissions` (held
  anywhere, for the menu), `assignments`, `root_permissions` and `post_permissions` (`{"7": [...]}`: permissions
  from group and post assignments for every post they reach; effective at a post = `root_permissions` + its entry).
  `/auth/check-permission` gains an optional `post_id`. `/auth/route-access` loses the admin flag in `required`.
- **Gate removed everywhere**: export needs `observation.view` + `export.create` (scoped); dictionary writes need
  their own `X.create/update/archive`; storage statistics and poster status need `media.monitor`; every other
  `admin=True` route keeps exactly its listed permissions.

### Errors (`enums/errors.py`)

- New: `role_exceeds_user_type`, `role_requires_root_scope`, `user_type_ceiling_exceeded`, `assignment_not_found`,
  `assignment_exists`, `last_assignment_required`, `scope_group_not_found`, `scope_group_not_empty`,
  `scope_group_cycle`, `scope_group_depth_exceeded`, `scope_group_name_taken`.
- Renamed (a code is one message, and the message now covers every built-in role): `admin_role_not_renamable`,
  `admin_role_permissions_fixed`, `admin_role_not_archivable`, `admin_role_not_deletable` become
  `builtin_role_not_renamable`, `builtin_role_permissions_fixed`, `builtin_role_not_archivable`,
  `builtin_role_not_deletable`.
- Removed: `admin_required`, `post_access_home_post`, `post_access_not_found`.
- `errors/openapi.py`: the `ADMIN` set is removed, `GUARDED` stays. Every route declares the codes it raises.

## 8. Migration and conversion

Two revisions:

- `021_scope_groups` (additive): `scope_groups`, `posts.group_id`. Ships with task 1 on its own.
- `022_authorization_v2`: `role_assignments`, `roles.builtin_key`, `users.user_type` (nullable during the
  conversion, then NOT NULL), renames custom roles whose name is a built-in key to `<name>_custom`, converts every
  user, removes the `admin.true` and `post.access_all` rows from `permissions` and `role_permissions`, renames
  `user_roles` and `user_post_access` to `*_v1`, and records what it did in `authz_v2_migration_log` (`kind`,
  `ref_id`, JSON `data`) for the downgrade.

Seeding (after migrations, at start) creates and syncs the built-in roles and inserts the new permissions.

### Conversion of one user (frozen in the 022 file)

The conversion function and its tables (permission levels, the gated-only set, type ceilings) are copied into the
migration file, so later code changes never change what 022 does. For user U with roles R (archived roles converted
the same way; derived roles of an archived role are archived too):

1. Holder of the built-in `admin` role: type `admin`, assignment `admin @ /`. Done.
2. `P(R)` = permissions of R without `admin.true` and `post.access_all`. When U does not hold `admin.true`, the
   gated-only permissions are dead today and are left out (reported): `user.create/update/archive`,
   `post.create/update/archive`, `role.*`, `category/signal_type/tag.create/update/archive`, `media.update`,
   `media_format.create/update/archive`, `export.create`.
3. U holds `post.access_all`: every R becomes `P(R) @ /`.
4. Otherwise, for each R:
   - root part = `system` permissions of `P(R)`, plus, when U holds `admin.true`, the record administration and
     global aggregates of `P(R)`: `post.view/create/update/archive`, `user.view/create/update/archive`,
     `scope_group.view`, `statistics.view` (the old record exception: these acted on every post and user record),
     plus `media.monitor` when `P(R)` holds `media.view` (storage statistics and poster status were `media.view` +
     the gate); assigned at `/` when not empty;
   - post part = the `scoped` rest (without what the root part holds), assigned at the home post and at every access
     row with `can_write`;
   - view part = the `*.view` permissions of the post part plus `export.create` when present (an export reads the
     data of read-only rows today), assigned at every access row without `can_write`.
5. Each (permission set, node) is assigned to the role whose set is exactly that: R itself when it matches, an
   already derived role, or a new derived role `<R.name>_system` / `_posts` / `_view` (display name with
   "(система)" / "(пости)" / "(перегляд)"), made unique with a numeric suffix when needed.
6. Type: the first of `viewer, operator, senior_operator, analyst, post_admin, admin` whose ceiling covers every
   permission U ends up with; a user converted from `post.access_all` prefers `analyst` over `viewer` when both
   cover.
7. A user with no permissions gets no assignment (reported; the API enforces "at least one" from now on).

Differences the new model cannot express 1:1 are reported, never hidden:

- `media.manage_any` + `admin.true`: the upload list was global while managing stayed in the data scope; now both
  follow `posts_with(media.manage_any)`;
- archived post and user records visible to `admin.true` without `X.archive`;
- observation archive / restore in archived posts needs `post.archive` at the post (before `admin.true` +
  `post.access_all`).

### Report

- Before the upgrade: `python -m app.backend.cli.authz_report [--json]` loads the conversion function from the 022
  file and runs it read-only on the current (020 / 021) database: every user, inferred type, assignments before and
  after, derived roles, removed dead permissions, every difference. Run it on a copy of the production database.
- During the migration: one audit row per converted user (`action=migrate`, `resource_type=user`) with the same
  details, written with core inserts.

### Downgrade

Exact, from the kept tables and the log: rename `*_v1` back, re-insert the removed permission rows and grants,
rename built-in-name collisions back, drop derived roles, `role_assignments`, `users.user_type` and
`roles.builtin_key`. 021's downgrade drops `posts.group_id` and `scope_groups` (groups are lost; documented).

### Verification

Tests on sqlite (fresh install; upgrade from 020 with fixture data covering every rule; downgrade), then a run on
throwaway MySQL 8 and MariaDB containers as for 018 - 020.

Rollout: `authz_report` on a database copy, review, deploy (start applies the migrations), the administrator fixes
the reported differences through the API, the web moves to the new auth fields.

## 9. Documents

- `api-contract/v1/openapi.json` regenerated on purpose; CHANGELOG entry with a migration guide for the web client.
- `API.md`: pages by `user_type`, buttons by `root_permissions` + `post_permissions`, the scope picker
  (`/scope-groups/tree`), the new problem codes.
- `CLAUDE.md`, section Authorization: the bullets about `admin.true` (gate, record exception, "write routes require
  X.view AND X.create ... admin") and about the data scope (`post.access_all`, home post, `user_post_access`) are
  replaced by:
  - "Access is role assignments (user + role + scope node: `/`, a scope group, a post), inherited down the tree,
    additive. Every route requires only its own permissions: `Depends(require(all=[Permissions.X_VIEW,
    Permissions.X_UPDATE]))`; there is no admin gate. The precise check is in the handler through
    `AccessControlService` with the permission (`check_post(auth, p, post_id, ...)`, `post_scope(auth, p)`,
    `check_user(auth, p, user)`)."
  - "Every user has one `user_type` (`auth/user_types.py`): base role (built-in, synced by seeding, read-only in the
    API) and ceiling. Pages in the web follow the type; the API never authorizes by type."
  - "Permission levels: `system` (assignable only at `/`) and `scoped`; a new permission declares its level next to
    `PERMISSION_META`. Records follow `X.view`, data follows the data permissions; there is no administrator
    exception."
  - "Delegation (`auth/privileges.py`): grant only what you hold at the node; target users only when you could grant
    all their assignments; the last holder of `admin @ /` stays."
  - The `include_deleted`, archive, hard-delete and audit bullets stay, with `X.archive` evaluated by scope.
  - Section "Observation counters": "administrators (`admin.true`) count any post" becomes "counts every post where
    `observation.view` is in effect"; the `include_archived` exception stays.
- Memory: `permission-model-decisions.md` is superseded by this design.

## 10. Testing

`venv\Scripts\python -m pytest` (in-memory sqlite):

- new: `test_scope_tree` (`posts_with`, inheritance, archived post direct vs inherited, depth, cycles),
  `test_user_types` (catalog, ceiling, type change, downgrade 409, sessions ended), `test_assignments` (delegation
  at scope, `system` only at `/`, duplicates, last assignment, last admin), `test_scope_groups` (API, name per
  parent, archive only when empty), `test_builtin_roles` (sync at start, 409 on changes, clone),
  `test_migration_022_authz` (every conversion rule on fixture data from 020, report equals audit rows, downgrade
  restores exactly);
- rewritten: `test_access_matrix`, `test_admin_privileges`, `test_post_access`, `test_route_access`,
  `test_require`, `test_seed`, `test_scope3`, `test_post_archive`;
- contract: `test_openapi`, `test_error_contract`, `test_route_registry`, `test_filter_criteria`; `test_api_contract`
  after the deliberate regeneration;
- live: 021 and 022 on throwaway MySQL 8 and MariaDB.

## 11. Delivery

Branch `authorization-v2`, subagent-driven development. Every task: an implementer subagent (TDD), then a **fresh
reviewer subagent** that sees only this spec, the plan and the diff (spec compliance, then code quality), fix rounds
until approved, `pytest` green, commit. After the last task a **final fresh validator** reviews the whole branch
against this spec (logic, security, contract, migration) before the merge.

Tasks (the plan details them):

1. Scope tree: `scope_groups`, `posts.group_id`, migration 021, `/scope-groups` API.
2. Permission levels, `UserType` and the type catalog, built-in roles with sync and clone, `builtin_role_*` codes,
   `GET /user-types`.
3. `role_assignments`, AuthContext v2, migration 022 with the conversion and the log, `authz_report` CLI.
4. `require` without `admin=`, `AccessControlService` with permissions; routes of posts and users.
5. Routes of observations, media, export, statistics, dictionaries, formats.
6. Users API: `user_type`, assignments, delegation rules, last admin, default assignment.
7. `/auth/me`, login response, `check-permission`, `route-access`.
8. Documents: contract, CHANGELOG, API.md, CLAUDE.md, memory.

## Out of scope

- PKI (sub-project B, next spec).
- Just-in-time elevation (Azure PIM), deny assignments, conditions on assignments (ABAC), groups of users as
  principals.
- Caching the scope tree between requests (three small queries per request are fine at this size; revisit with
  measurements).
