# Changelog

All notable changes to this project are documented here.
Format: Keep a Changelog. Entries are grouped by step of the permission refactor.

## [Unreleased] - Permission system refactor

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
