- always if you need make url read routes enum and only after make some things
- Add to memoty. Try read enums with routes before make some route like "/url"

## Routes and API versions

- Every path lives in `app/backend/enums/routes.py` as a plain `str` Enum (no `"/api/v1/..."` or `"/"` literal anywhere, tests included). New route: add the member to its group first, then `@router.get(XRoutes.MEMBER.value)`. A collection root is the group's own `ROOT = "/"`. `SystemRoutes` / `DocsRoutes` values are full paths; the other groups are relative to their `BASE`.
- Layout: `app/backend/api/vN/` holds the handler modules of version N (the folder is the version), `api/shared/` the request / response models and helpers, `api/system/` health and the problem pages. A module declares its place: `router = APIRouter(prefix=XRoutes.BASE.value, tags=[XRoutes.tag()])`.
- `api/vN/__init__.py` declares what the version publishes: `V1 = PublishedVersion(APIVersionRoutes.V1, auth, users, ...)` (`api/versioning.py`); a new module goes into that list. `main.py` includes `health.router`, `V1.router`, `problems.router`.
- A full path comes from the version: `V1.path(PostRoutes.POST_BY_ID, post_id=7)` (template without params, `LookupError` if the version does not publish it). A route that returns URLs takes `version: PublishedVersion = Depends(request_version)` and uses `version.path(...)`; never import `V1` inside a handler module.
- Versions run in parallel and never override each other: a version publishes only its own folder, `api/vN` never imports `api/vM`, no shared routers, no re-publishing. What two versions share lives in `services/` or `api/shared/`. A new version starts with only the resources whose contract changes; the client moves to it resource by resource while v1 keeps serving everything. Then: `include_router(V2.router, generate_unique_id_function=lambda r: f"{r.name}_v2")`, later `include_router(V1.router, deprecated=True)`, retire = remove the include and the folder.
- Within one version, deprecate with what FastAPI already has: a new route next to the old one and `deprecated=True` on the old decorator (or `Query(deprecated=True)`); openapi-typescript marks it `@deprecated`.
- `tests/test_route_registry.py` fails on an enum member without a route, a route without an enum member, a route shadowed by an earlier one (register literal paths like `/filter-schema` before `/{id}`) and an import between version folders.

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
- before use API read @app\backend\api\
- to memorize DONT USE EMOJI like this ❌
- do not use aNy emojis

## Authorization

- Every API route declares its requirement with a dependency, never a decorator:
  `auth: AuthContext = Depends(require(all=[Permissions.TAG_VIEW, Permissions.TAG_UPDATE], admin=True))`
  Use `require()` with no arguments for routes that only need an authenticated user. `auth.user` is the User row.
- `admin.true` is a GATE for administration routes (`admin=True`). It never substitutes a granular permission.
- Write routes require `X.view` AND `X.create` / `X.update` / `X.archive` (`role.delete` only for the permanent delete of roles).
- Data scope: `post.access_all` sees every post; otherwise the home post (`users.post_id`) plus `user_post_access` rows.
  Use `AccessControlService.resolve_post_scope(auth, filters.post_id)` for lists and `check_post_view` / `check_post_write` for single resources.
  Exception for post and user records only (not post data): administrators (`admin.true` plus the route's `X.view`) are not limited by post. Use `post_record_scope` / `check_post_record_view` for posts, `resolve_record_scope` and `check_user_visibility` for users. Aggregates and cross-user lists follow the same split: `GET /statistics` and `GET /media/uploads` with `media.manage_any` cover every post for administrators and `post_scope(auth)` for everyone else. Observations, media and reports never get this exception.
  Ownership overrides: `observation.manage_any`, `media.manage_any`.
- Raise 403 only through `access_denied(AccessErrorCode.X, required=..., missing=..., **params)` from `app/backend/services/access_errors.py`.
- New permission: add it to `Permissions` in `app/backend/enums/general.py`, add its Ukrainian display name to `PERMISSION_META` in `app/backend/database.py`. Seeding grants it to the admin role automatically.
- Every route with `require()` appears in `GET /auth/route-access`; the test `tests/test_route_access.py` fails if a route under `/api/v1` has no requirement.
- Run tests with `venv\Scripts\python -m pytest` (in-memory sqlite, no MySQL needed).
- Add an entry to `CHANGELOG.md` for every change that affects the API or the database.
- Presence: `User.last_activity_at` and `Post.online_user_count` / `Post.user_count` / `Post.last_user_activity_at` are column properties attached in `app/backend/models/presence_properties.py`; `User.is_online` is a Python property. A post has no stored connection status: it has users online when `online_user_count > 0` (filter `has_online_users`). Thresholds live in `settings.security` (`online_threshold_seconds`, `activity_touch_interval_seconds`); sessions are touched by `get_current_user`, no daemon. Post responses go through `post_to_response` in `api/shared/models/response/posts_response.py`.
- `include_deleted` on any list or search must be guarded with `AccessControlService.check_include_deleted(auth, params, Permissions.X_ARCHIVE)`.
- Archiving is the rule for every entity (roles, observations, users, media, dictionaries): `POST /{entity}/{id}/archive` sets `is_deleted=true`, `POST /{entity}/{id}/restore` clears it, both need `X.view` AND `X.archive`. Archived rows answer 404 on update and 409 on restore-when-not-archived. `is_deleted` is never accepted in a request body. Archived roles stop applying immediately (`load_auth_context` filters `Role.is_deleted`), archived tags stay attached to observations, archived media stays linked to its observation but is left out of `media_ids`.
- The only hard delete in the API is `DELETE /roles/{id}` (`role.view` + `role.delete`, administration). It removes an archived role with no active users, plus its `role_permissions` and `user_roles` rows, with core `delete()` statements (no ORM cascade in the async session). Never add another hard delete without a distinct permission, an archive-first precondition and a full copy of the removed rows in the audit log. Audit actions: `archive`, `restore`, `delete` (permanent only).
- Renaming a permission needs a migration that renames the `permissions` row in place (see `010_archive_permissions`); seeding only inserts missing rows and refreshes `display_name` / `description` from `PERMISSION_META`.

## Errors (RFC 9457) and OpenAPI

- Every error is an RFC 9457 problem, `Content-Type: application/problem+json`: `type` (URI `<base>/problems/<code>`, `settings.app.problem_base_url` or the request origin), `title` (fixed per type), `status`, `detail` (title with params filled in), `instance` (request path), `code`, plus the params as extension members. FastAPI's own errors are wrapped by `app/backend/error_handlers.py`: 422 is `validation_error` with `errors: [{code, detail, pointer | parameter + location, **params}]` (never the submitted input), unknown route / method / unparsable body have codes, any other framework status is `about:blank`, unhandled exceptions are 500 `internal_error` with a logged `error_id`, a 401 always has `WWW-Authenticate`.
- A code is the key of exactly ONE message in the web localization package, never a category; a new message is a new code. Problem types live in `enums/errors.py` (`ErrorCode`, `AuthErrorCode`, `AccessErrorCode`) with their status, title, detail template and typed params (`problem(status, title, detail, **params)`); an `Optional[...]` param may be left out and is then absent from the body, never null. Field error codes (items of `validation_error`) are `FieldErrorCode`.
- Raise with `ApiError(ErrorCode.X, headers=..., **params)` (the status comes from the type); `access_denied(AccessErrorCode.X, required=..., missing=..., **params)` for 403; `invalid(FieldErrorCode.X, **params)` in request model validators; `field_problem(FieldErrorCode.X, ("body", "field"))` when a route checks a single value itself. Services raise `CodedError(ErrorCode.X, **params)` and the route converts it with `exc.to_api_error(headers=..., **extra)`. Never `HTTPException(detail=...)`, a hand-made error `JSONResponse` or a plain `ValueError` in a validator. Param names: at least 3 characters, never `type`, `title`, `status`, `detail`, `instance`, `code` (rename: `session_status`, `time_zone`).
- Never put exception text, file paths, SQL, stored internal reasons or the submitted `input` into a detail or a param; log them instead.
- Every route declares the problems it answers on its decorator: `responses=problems(ErrorCode.POST_NOT_FOUND, ErrorCode.POST_CODE_TAKEN, *ADMIN)` (`app/backend/openapi_problems.py`; `*GUARDED` / `*ADMIN` are the 403 of `require(all=...)` / `require(..., admin=True)`). Generic ones are declared once and merged by FastAPI: `internal_error` on the app, the 401 set (`AUTHENTICATED`) and `validation_error` on the version router; login and refresh declare their own 401 set. With success helpers: `responses={**file_response(...), **problems(...)}`. A new `raise ApiError(Code.X)` needs `Code.X` in the route's `problems(...)`: the session fixture in `tests/conftest.py` fails on any problem the tests make the app answer that its operation does not declare. The `openapi()` hook only moves error content to `application/problem+json` and adds the schemas of framework problems; `/problems` and `/problems/{code}` (`api/system/problems.py`) read the document. operationId = endpoint function name (keep it unique).
- A success response that is not a JSON model (file, image, empty body, headers) is declared with `responses=` from `api/shared/models/response/openapi_docs.py` (`file_response`, `empty_response`, `with_headers`); a JSON response always has a `response_model`.
- Client contract for the web team: `API.md` (errors, localization keys, openapi-typescript) and `WEB_TOOLING.md` (export, type checks, translation checks; starter files in `api-contract/web-kit/`).
- The error contract is locked in `api-contract/problems.lock.json` (codes, status, params, codes per operationId). `tests/test_api_contract.py` fails on any difference and marks what breaks the web client. Never run `python -m app.backend.api_contract lock` to silence it: first decide whether the change is intended (a renamed code or param breaks the web translations; a code gone from an operation means it was dropped from a `problems(...)` declaration). If intended: lock, commit the lock with the change, add a CHANGELOG entry.
- `tests/test_error_contract.py` checks the catalog against RFC 9457, forbids hand-made error bodies and plain `ValueError` in validators, and checks every raise passes exactly the declared params; `tests/test_openapi.py` checks the generated document (problem+json errors, 401/403 follow `require()`, no JSON response without a schema, real bodies validate against their problem schema).

## Observation counters

- Observations are counted by `created_at`, never `observed_at` (`GET /posts/{id}/observation-stats`, `GET /statistics`). Counting lives in `services/observation_counts.py`; `observation-stats` is the only source of per-post counters, post responses carry none.
- Counters are aggregates: administrators (`admin.true`) count any post (`check_post_record_view`), everyone else their data scope. Figures are `not_archived` / `archived` pairs that never overlap; archived ones only on the explicit `include_archived` flag. It needs only `observation.view` (counts, not archived rows), so it is the one exception to the `check_include_deleted` rule.

## Observation frequencies

- Bands are rows of `observation_frequencies` (`ObservationFrequency`): `kind` from `FrequencyKind` in `enums/general.py` (no DB CHECK on it, a new kind is a new enum value), optional `link_group` for bands of one emitter, `bandwidth` generated by the database (never write it). An observation keeps at least one active band of any kind.
- Never query `observation_frequencies` directly: go through `filters/observation_frequency_filter.py` (`rows_query` joins observations for `is_deleted` of both tables and the post scope; `observation_has_band` puts every band condition in one subquery so they match the same band).
- Bands change only through `/observations/{id}/frequencies...` with `get_observation_for_write(lock=True)` from `api/shared/helpers/observation_helper.py`; removal is archive, never delete.

### Migration rule after 002_sync_schema

`002_sync_schema` creates every table from the models that is missing in the database. A later revision that creates a new table must either list the table in `TABLES_OWNED_BY_LATER_REVISIONS` inside `002_sync_schema_with_models.py` or guard its `create_table` with an inspector check, otherwise fresh installs fail with "table already exists". The same applies to columns and indexes: guard `add_column` / `create_index` with an inspector check (see `004_role_soft_delete_and_unique_assignments.py`).

## Media pipeline

- The API never decodes images or video. Thumbnails and previews are rendered on demand by imgproxy (libvips) from the original, or from the video poster, and cached by nginx. Only sign URLs in the API (`services/media/urls.py`, `services/media/imgproxy.py`).
- The only precomputed derived file is one poster JPEG per video at `<upload_path>/_derived/posters/{id // 1000:03d}/{id}.jpg`, extracted by `python -m app.backend.workers.media_worker` with ffmpeg. Job state is the `poster_*` columns on `media_files`; claim with the conditional UPDATE in `services/media/jobs.py` (no SKIP LOCKED, must keep working on sqlite tests). `enqueue_poster()` is called inside the upload transaction.
- `MediaFile.thumbnail_status` (ready, pending, processing, failed, unsupported) and `thumbnail_urls` are derived; never store thumbnail paths again.
- `GET /media/{id}/thumbnail` must not have side effects. 202 + Retry-After while a poster is pending, 409 failed, 422 unsupported.
- Public thumbnail URLs carry an `exp:` aligned to a ttl window (`quantised_expiry`) so nginx cache keys stay stable inside the window. Keep that property when touching URL building.
- Serve originals with `original_file_response` (X-Accel-Redirect behind nginx, FileResponse otherwise). Never read a whole file into memory in a route.
- Accepted upload formats are rows of `media_formats` (model `MediaFormat`), managed through `/media-formats` (permissions `media_format.*`, administration) and read through `services/media/formats.py` (`load_registry`, cached 15 s, `invalidate_registry_cache()` after every write). `DEFAULT_FORMATS` there only seeds missing rows; never hard-code a format in a route. Uploads answer 415 (format, with `Accept-Post`) or 413 (size, per-format `max_file_size_bytes`, capped by `storage.max_file_size_gb`). Keep defaults limited to what imgproxy renders, ffmpeg decodes and browsers play.
- Video and audio metadata (`duration_seconds`, `video_codec`, `audio_codec`, `playback_type`, `width`, `height`) come from `services/media/probe.py` (ffprobe, ffmpeg banner fallback): at upload in `_finish_upload`, in the worker when missing, and via `media_tools probe-missing`. `playback_type` is the RFC 6381 string for `canPlayType`; never guess codecs from the MIME type.
- Resumable uploads (`api/v1/media_uploads.py`, `services/media/uploads.py`, table `upload_sessions`): progress is `received_bytes`, one writer per session via `writer_token`, chunks must arrive at `received_bytes` exactly, a failed chunk truncates the part file back. Completion goes through `store_file_from_path` like every other upload; never add a second storage path. Client contract in `UPLOADS.md`; keep it in sync with the status codes.
- Media maintenance commands: `python -m app.backend.media_tools {cleanup-legacy-thumbnails,reconcile-posters,poster-status}`.
- Deployment topology (`DEPLOYMENT.md`): Docker runs only api, media-worker and imgproxy on the host network. MySQL, the entry nginx and the media directory (local disk or a mounted network share) stay on the host; the media directory is bind-mounted at the same absolute path into every container. Do not add nginx or a database to docker-compose.yml.
