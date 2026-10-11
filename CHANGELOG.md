# Changelog

All notable changes to this project are documented here.
Format: Keep a Changelog. Entries are grouped by step of the permission refactor.

## [Unreleased] - Permission system refactor

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
