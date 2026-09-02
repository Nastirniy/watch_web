- always if you need make url read routes enum and only after make some things
- Add to memoty. Try read enums with routes before make some route like "/url"

## Database Migrations

This project uses **Alembic** for database migrations. Never drop the database - use migrations instead!

### Installation
```bash
pip install alembic  # Required for migrations
```

### Commands:
```bash
# Initialize migrations (first time only)
python -m app.backend.migrations_manager init

# Create new migration after model changes
python -m app.backend.migrations_manager revision -m "Add new field to User"

# Apply all pending migrations  
python -m app.backend.migrations_manager migrate

# Show current migration
python -m app.backend.migrations_manager current

# Show migration history
python -m app.backend.migrations_manager history

# Downgrade to previous migration
python -m app.backend.migrations_manager downgrade

# Run database seeding only
python -m app.backend.migrations_manager seed
```

### Workflow:
1. **Modify models** in `app/backend/models/`
2. **Create migration**: `python -m app.backend.migrations_manager revision -m "Description"`
3. **Review generated migration** in `app/backend/migrations/versions/`
4. **Apply migration**: `python -m app.backend.migrations_manager migrate`
5. **Commit both model and migration files**

### Important:
- **Never drop tables manually** - use downgrade migrations
- **Always review** generated migrations before applying
- **Test migrations** on staging before production
- **App startup** automatically applies migrations and seeds data
- before use API read @app\backend\routes\
- to memorize DONT USE EMOJI like this ❌
- do not use aNy emojis

## Authorization

- Every API route declares its requirement with a dependency, never a decorator:
  `auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE], admin=True))`
  Use `require()` with no arguments for routes that only need an authenticated user. `auth.user` is the User row.
- `admin.true` is a GATE for administration routes (`admin=True`). It never substitutes a granular permission.
- Write routes require `X.view` AND `X.create` / `X.update` / `X.delete`.
- Data scope: `post.access_all` sees every post; otherwise the home post (`users.post_id`) plus `user_post_access` rows.
  Use `AccessControlService.resolve_post_scope(auth, filters.post_id)` for lists and `check_post_view` / `check_post_write` for single resources.
  Ownership overrides: `observation.manage_any`, `media.manage_any`.
- Raise 403 only through `access_denied(AccessErrorCode.X, message, required=..., missing=...)` from `app/backend/services/access_errors.py`.
- New permission: add it to `Permissions` in `app/backend/enums/general.py`, add its Ukrainian display name to `PERMISSION_META` in `app/backend/database.py`. Seeding grants it to the admin role automatically.
- Every route with `require()` appears in `GET /auth/route-access`; the test `tests/test_route_access.py` fails if a route under `/api/v1` has no requirement.
- Run tests with `venv\Scripts\python -m pytest` (in-memory sqlite, no MySQL needed).
- Add an entry to `CHANGELOG.md` for every change that affects the API or the database.
- Presence: `User.last_activity_at` and `Post.online_user_count` / `Post.last_user_activity_at` are column properties attached in `app/backend/models/presence_properties.py`; `User.is_online` and `Post.effective_connection_status` are Python properties. Thresholds live in `settings.security` (`online_threshold_seconds`, `activity_touch_interval_seconds`). Post responses go through `post_to_response` in `routes/models/response/posts_response.py`.
- `include_deleted` on any list or search must be guarded with `AccessControlService.check_include_deleted(auth, params, Permissions.X_DELETE)`.
- Soft deletion is the rule for every entity (roles, observations and users included; DELETE routes set `is_deleted`, restore goes through the update endpoint with `is_deleted=false`). Deleted roles stop applying immediately (`load_auth_context` filters `Role.is_deleted`), deleted tags stay attached to observations.

### Migration rule after 002_sync_schema

`002_sync_schema` creates every table from the models that is missing in the database. A later revision that creates a new table must either list the table in `TABLES_OWNED_BY_LATER_REVISIONS` inside `002_sync_schema_with_models.py` or guard its `create_table` with an inspector check, otherwise fresh installs fail with "table already exists". The same applies to columns and indexes: guard `add_column` / `create_index` with an inspector check (see `004_role_soft_delete_and_unique_assignments.py`).
