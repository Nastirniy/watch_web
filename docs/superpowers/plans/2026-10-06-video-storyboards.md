# Video Storyboards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Seek-bar previews for videos: the media worker generates sprite sheets plus layout in the background (never delaying posters), and the API serves a WebVTT whose cues point at signed `/media-images/` URLs of the sheets.

**Architecture:** A second, low-priority job queue in the existing media worker, state in a new table `video_storyboards`, one keyframe-only ffmpeg pass per video, sheets under `MEDIA_ROOT/_derived/storyboards/`, delivered through the existing imgproxy + nginx `/media-images/` path. A new route builds the WebVTT from the stored layout.

**Tech Stack:** FastAPI, SQLModel / SQLAlchemy async, Alembic, ffmpeg, imgproxy, nginx, pytest (in-memory sqlite).

**Spec:** `docs/superpowers/specs/2026-10-06-video-storyboards-design.md` (read it first; this plan argues from it).

## Global Constraints

- Run tests with `venv/Scripts/python -m pytest` (in-memory sqlite, no MySQL). The full suite must stay green after every task.
- Do not commit: the user commits. "Checkpoint" steps below only run the suite.
- ASCII only in code, comments and docs; no emoji.
- Paths only from enums in `app/backend/enums/routes.py`: add `MediaRoutes.MEDIA_STORYBOARD = "/{media_id}/storyboard"` first; tests call `V1.path(MediaRoutes.MEDIA_STORYBOARD, media_id=...)`; no `"/api/v1/..."` literal.
- Errors: `ApiError(ErrorCode.X, **params)` in routes, every code listed in the route's `problems(...)`; services raise their own exceptions, never import `app.backend.api`.
- Pydantic v2 `@field_validator` only.
- Migration rule after `002_sync_schema`: new table listed in `TABLES_OWNED_BY_LATER_REVISIONS` and `create_table` guarded with an inspector check.
- The API never decodes video; only the worker runs ffmpeg.
- Defaults, verbatim from the spec: `enabled=true`, `slots=1`, `min_duration_seconds=60`, `max_frames=300`, `min_step_seconds=2`, `tile_width=160`, `columns=10`, `rows=10`, `jpeg_qscale=5`, `threads=1`, `nice=10`, `timeout_seconds=1800`, `lease_seconds=300`, `max_attempts=3`, `backoff_seconds=[60, 300, 1800]`, `retry_after_seconds=30`; nice steps `1, 2, 5, 10, 15, 30, 60, 120, 300, 600` seconds.
- Files: `MEDIA_ROOT/_derived/storyboards/{id // 1000:03d}/{id}/sheet-000.jpg`, `sheet-001.jpg`, ...
- The intended contract change (new operation, field `storyboard_status`, codes `storyboard_failed` 409 and `storyboard_unsupported` 422) is accepted by rewriting `api-contract/v1/openapi.json` with `venv/Scripts/python -m app.backend.api_contract`, plus a CHANGELOG entry.

## Review Focus

- ffmpeg writes fewer frames than planned (container duration longer than the stream): no cue may point past the last frame written. Test in Task 4.
- Storyboards turned off while rows are `ready`: the API answers `storyboard_unsupported`, the worker claims nothing, files stay. Tests in Tasks 4 and 6.
- The VTT is cached by the browser longer than the signed sheet URLs inside it live: `Cache-Control` max-age must not exceed `thumbnail.url_ttl_seconds`. Test in Task 6.
- `nice` on Windows (development): no POSIX priority call there, ffmpeg still runs. Covered by the real-ffmpeg test of Task 3 on this machine.
- Duration unknown after the poster (probe found none): no row is created and the client sees `unsupported`, never a `pending` that never ends. Tests in Tasks 2 and 4.

---

### Task 1: Settings, enums, model and migration

**Files:**
- Modify: `app/backend/config.py` (new `StoryboardSettings`, field `storyboard` on `StorageConfig`), `app/config.toml` (section `[storage.storyboard]` with every key and a one-line comment), `ENV.md` (one row per `MYSETTINGS_STORAGE__STORYBOARD__*`)
- Modify: `app/backend/enums/general.py`
- Create: `app/backend/models/storyboard.py`, `app/backend/models/storyboard_properties.py`
- Modify: `app/backend/models/__init__.py`
- Create: `app/backend/migrations/versions/017_video_storyboards.py`
- Modify: `app/backend/migrations/versions/002_sync_schema_with_models.py` (`TABLES_OWNED_BY_LATER_REVISIONS`)
- Test: `tests/test_media_settings.py`, `tests/test_migrations.py`

**Interfaces:**
- Produces: `StoryboardSettings` (fields and defaults from Global Constraints; `backoff_seconds` parsed with the existing `_parse_json_sequence`); `settings.storage.storyboard`.
- Produces: `StoryboardJobStatus(str, Enum)`: `PENDING="pending"`, `PROCESSING="processing"`, `READY="ready"`, `FAILED="failed"`; `StoryboardStatus(str, Enum)`: the same four plus `UNSUPPORTED="unsupported"`.
- Produces: `VideoStoryboard(SQLModel, table=True)`, `__tablename__ = "video_storyboards"`: `media_id: int` (primary key, foreign key `media_files.id`), `status: str` (max 12), `attempts: int = 0`, `locked_until`, `worker` (max 64), `error` (max 500), `generated_at` (all Optional), `step_seconds`, `frame_count`, `columns`, `rows`, `tile_width`, `tile_height`, `sheet_count` (Optional[int]); index `ix_video_storyboards_status_locked` on `(status, locked_until)`.
- Produces: `MediaFile.storyboard_job_status` (Optional[str]), a `column_property` scalar subquery over `VideoStoryboard.status`, attached by `configure_storyboard_properties()` called from `models/__init__.py` (same pattern as `presence_properties.py`).
- Produces: `STORYBOARD_DIR = "_derived/storyboards"` and `storyboard_relative_dir(media_id: int) -> str` in `models/storyboard.py` (`"_derived/storyboards/000/91"`).

- [ ] **Step 1: Write the failing tests**
  - `tests/test_media_settings.py::test_storyboard_defaults_and_string_values`: `StoryboardSettings()` equals every default above; `StoryboardSettings(backoff_seconds="[1, 2]").backoff_seconds == [1, 2]` (environment values arrive as strings, like `PosterSettings` in `test_sizes_and_backoff_parse_from_json_strings`); `settings.storage.storyboard` is a `StoryboardSettings`.
  - `tests/test_migrations.py`: `HEAD = "017_video_storyboards"`; in `test_migration_chain_on_empty_database` add `"video_storyboards"` to the expected tables; new `test_017_creates_video_storyboards_on_upgrade` (database at `016_session_certificate_binding` without the table -> upgrade -> table and index exist).
  - `tests/test_storyboard_jobs.py::test_storyboard_job_status_is_visible_on_media` (new file): a video with a `VideoStoryboard(status="ready")` row loads with `media.storyboard_job_status == "ready"`; a video without a row loads with `None`.

- [ ] **Step 2: Run them, expect failures** (`ImportError` / missing table / HEAD mismatch)

Run: `venv/Scripts/python -m pytest tests/test_media_settings.py tests/test_migrations.py tests/test_storyboard_jobs.py -q`

- [ ] **Step 3: Implement** the settings, enums, model, property, migration (inspector-guarded `create_table` and `create_index`, downgrade drops the table only), the `002` list entry, the `config.toml` section and the `ENV.md` rows.

- [ ] **Step 4: Run the same tests, expect PASS; Checkpoint: full suite green**

---

### Task 2: Layout, client status and WebVTT (pure functions)

**Files:**
- Create: `app/backend/services/media/storyboards.py`
- Modify: `app/backend/services/media/urls.py`
- Test: `tests/test_storyboard_layout.py`

**Interfaces:**
- Consumes: `StoryboardSettings`, `VideoStoryboard`, `StoryboardJobStatus`, `StoryboardStatus`, `MediaFile.storyboard_job_status` (Task 1).
- Produces in `storyboards.py`:
  - `NICE_STEPS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600)`
  - `@dataclass(frozen=True) class StoryboardPlan: step_seconds: int; frame_count: int; columns: int; rows: int; tile_width: int; sheet_count: int`
  - `plan_storyboard(duration_seconds: float, config: StoryboardSettings) -> StoryboardPlan`: `raw = max(min_step_seconds, duration / max_frames)`; the smallest nice step `>= raw`, above 600 the next multiple of 600; `frame_count = ceil(duration / step)` (at least 1); `sheet_count = ceil(frame_count / (columns * rows))`.
  - `client_status(media: MediaFile, config: StoryboardSettings) -> str`: the spec's table in section 3 ("Without a row"); with a row: `unsupported` when `config.enabled` is false, else the row status.
  - `build_vtt(layout: VideoStoryboard, duration_seconds: float, sheet_url: Callable[[int], str]) -> str`: `WEBVTT` header, one cue per frame `i`: start `i * step`, end `min((i + 1) * step, duration)`, text `sheet_url(i // per_sheet) + f"#xywh={x},{y},{tile_width},{tile_height}"` with `x = (i % per_sheet % columns) * tile_width`, `y = (i % per_sheet // columns) * tile_height`; timestamps `HH:MM:SS.mmm`.
- Produces in `urls.py`: `storyboard_sheet_url(media_id: int, sheet_index: int, expires_at: datetime) -> str`: `public_base_url + public_prefix + signed_path(get_signer(), build_processing_path(f"{storyboard_relative_dir(media_id)}/sheet-{sheet_index:03d}.jpg", 0, 0, fmt=ThumbnailFormat.JPEG.value, expires_at=expires_at))`.

- [ ] **Step 1: Write the failing tests** in `tests/test_storyboard_layout.py`:
  - `test_step_adapts_to_duration`: 60 s -> step 2, 30 frames, 1 sheet; 7200 s -> step 30, 240 frames, 3 sheets; 36000 s -> step 120, 300 frames; 2100 s (raw 7) -> step 10; 360000 s -> step 1200; 3 s -> step 2, 2 frames.
  - `test_vtt_cues_cross_sheets`: layout step 2, tile 160 x 90, 10 x 10, 250 frames, duration 499.5: cue 0 is `00:00:00.000 --> 00:00:02.000` and ends with `sheet-0#xywh=0,0,160,90` (use a fake `sheet_url=lambda n: f"sheet-{n}"`); cue 11 has `#xywh=160,90,160,90`; cue 100 is on `sheet-1` at `0,0`; the last cue ends at `00:08:19.500`.
  - `test_client_status_without_a_row`: one assertion per row of the spec table (image -> unsupported; video 30 s -> unsupported; poster failed -> failed; poster pending -> pending; poster ready + duration None -> unsupported; poster ready + 120 s -> pending; `enabled=False` -> unsupported).
  - `test_client_status_with_a_row`: row `processing` -> processing; row `ready` with `enabled=False` -> unsupported.
  - `test_sheet_urls_are_signed_public_media_images`: with a key and salt set, `storyboard_sheet_url(91, 2, t)` starts with `settings.storage.imgproxy.public_prefix + "/"`, contains `exp:` and ends with `.jpg`; decoding its source segment gives `local:///_derived/storyboards/000/91/sheet-002.jpg` (reuse `decode_source` from `tests/test_media_thumbnails.py`).

- [ ] **Step 2: Run, expect FAIL** (`ModuleNotFoundError`)

Run: `venv/Scripts/python -m pytest tests/test_storyboard_layout.py -q`

- [ ] **Step 3: Implement** the functions above.

- [ ] **Step 4: Run, expect PASS; Checkpoint: full suite green**

---

### Task 3: ffmpeg extraction

**Files:**
- Modify: `app/backend/services/media/storyboards.py`
- Test: `tests/test_storyboards_ffmpeg.py` (skipped without ffmpeg, like `tests/test_posters_ffmpeg.py`)

**Interfaces:**
- Consumes: `StoryboardPlan` (Task 2); `resolve_binary` from `services/media/posters.py`.
- Produces:
  - `class StoryboardError(Exception)`
  - `@dataclass(frozen=True) class StoryboardResult: tile_height: int; sheet_count: int`
  - `extract_storyboard(source: Path, target_dir: Path, plan: StoryboardPlan, config: StoryboardSettings, ffmpeg: str) -> StoryboardResult`: runs the spec's command (`-nostdin -hide_banner -skip_frame nokey -i source -an -sn -dn -vf "fps=1/{step},scale={tile_width}:-2,tile={columns}x{rows}" -q:v {jpeg_qscale} -threads {threads}`) into a temporary directory next to `target_dir`; on POSIX with `config.nice > 0` the child runs under `os.nice(config.nice)` (via `preexec_fn`), never on Windows; timeout `config.timeout_seconds`; `tile_height = first sheet height // rows` (Pillow); replaces `target_dir` atomically (rename the temp directory over a removed old one); raises `StoryboardError` on a non-zero exit, a timeout or no sheet written, leaving no temp directory behind.

- [ ] **Step 1: Write the failing tests**:
  - `test_storyboard_from_test_pattern`: a 70 s `testsrc` 320 x 240 video (helper like `_synth_video`, `rate=10`), `StoryboardSettings(columns=4, rows=4, timeout_seconds=120)`, plan from `plan_storyboard(70, config)` (step 2, 35 frames, 3 sheets): result `tile_height == 120`, `sheet-000.jpg` .. `sheet-002.jpg` exist and are 640 x 480, no temp directory left.
  - `test_a_broken_source_raises_and_leaves_nothing`: a text file named `.mp4` -> `StoryboardError`, `target_dir` absent, no temp directory.

- [ ] **Step 2: Run, expect FAIL** (`ImportError`)

Run: `venv/Scripts/python -m pytest tests/test_storyboards_ffmpeg.py -q`

- [ ] **Step 3: Implement `extract_storyboard`.**

- [ ] **Step 4: Run, expect PASS; Checkpoint: full suite green**

---

### Task 4: Job store

**Files:**
- Create: `app/backend/services/media/storyboard_jobs.py`
- Test: `tests/test_storyboard_jobs.py`

**Interfaces:**
- Consumes: Task 1 model and enums; `utcnow` from `services/media/jobs.py`.
- Produces (mirroring the poster functions in `jobs.py`, conditional UPDATE, no SKIP LOCKED):
  - `async enqueue_storyboard(session, media: MediaFile) -> bool`: inserts a `pending` row and commits only when storyboards are enabled, the media is a non-archived video, the duration is known and `>= min_duration_seconds`, and no row exists; returns whether it inserted.
  - `async claim_next_storyboard(session, worker: str, lease_seconds: Optional[int] = None, now: Optional[datetime] = None) -> Optional[VideoStoryboard]`: oldest claimable row (pending with no or expired lock, or processing with an expired lease) of a non-archived video; `None` when storyboards are disabled.
  - `async renew_storyboard_lease(session, media_id: int, worker: str) -> bool`
  - `async complete_storyboard(session, media_id: int, worker: str, plan: StoryboardPlan, result: StoryboardResult) -> bool`: writes the layout; `sheet_count = result.sheet_count`, `frame_count = min(plan.frame_count, result.sheet_count * plan.columns * plan.rows)`.
  - `async fail_storyboard(session, media_id: int, worker: str, error: str) -> Optional[str]`: backoff from `storyboard.backoff_seconds`, `failed` after `storyboard.max_attempts`.
  - `async release_storyboard_claims(session, worker: str) -> int`
  - `async reconcile_storyboards(session, storage_root: Path, retry_failed: bool = False) -> Dict[str, int]` with keys `queued_missing_rows`, `requeued_missing_files`, `retried_failed` (the spec's reconciliation cases; nothing when disabled).
  - `async storyboard_counts(session) -> Dict[str, int]` by status.

- [ ] **Step 1: Write the failing tests** (sqlite `db` fixture, helpers like `_videos` in `tests/test_poster_jobs.py`, a `storyboard_settings` fixture that monkeypatches `settings.storage.storyboard`):
  - `test_enqueue_only_long_videos_with_a_known_duration` (120 s -> row; 30 s, duration None, image, archived, disabled -> none; second call -> False).
  - `test_claim_is_exclusive_and_leases_expire` (as the poster test).
  - `test_failure_backs_off_then_fails_permanently` (attempt 1 -> pending with `locked_until` ~ now + 60 s; attempt 3 -> failed).
  - `test_archived_and_disabled_are_never_claimed`.
  - `test_fewer_frames_than_planned_never_point_past_the_last_sheet` (plan 250 frames on 10 x 10, result 2 sheets -> stored `frame_count == 200`, `sheet_count == 2`).
  - `test_reconcile` (ready without files -> pending; video with ready poster and 120 s without row -> new pending; failed with `retry_failed` -> pending, attempts 0; disabled -> all counts 0).

- [ ] **Step 2: Run, expect FAIL**

Run: `venv/Scripts/python -m pytest tests/test_storyboard_jobs.py -q`

- [ ] **Step 3: Implement `storyboard_jobs.py`.**

- [ ] **Step 4: Run, expect PASS; Checkpoint: full suite green**

---

### Task 5: Worker integration

**Files:**
- Modify: `app/backend/workers/media_worker.py`
- Test: `tests/test_storyboard_jobs.py` (worker section)

**Interfaces:**
- Consumes: Tasks 2-4.
- Produces:
  - `StoryboardExtractor = Callable[[Path, Path, StoryboardPlan], StoryboardResult]` (source, target_dir, plan); default wraps `extract_storyboard` with `settings.storage.storyboard` and the resolved ffmpeg.
  - `MediaWorker.__init__(..., storyboard_extractor: Optional[StoryboardExtractor] = None)`; raises `ValueError` naming both settings when storyboards are enabled and `storyboard.slots >= concurrency`.
  - `async MediaWorker.process_storyboard(row: VideoStoryboard) -> bool`: loads the `MediaFile` of the row (source path, duration), `plan = plan_storyboard(media.duration_seconds, settings.storage.storyboard)`, target `storage_root / storyboard_relative_dir(media.id)`, runs the extractor with `asyncio.to_thread`, lease renewal loop like `_renew_lease_loop`, then `complete_storyboard(..., plan, result)` or `fail_storyboard`.
  - In `run()`: after `claim_next_poster` returns `None`, and while fewer than `storyboard.slots` storyboard tasks run, claim with `claim_next_storyboard` and run it under the same semaphore permit; after `complete_poster` succeeds in `process()`, call `enqueue_storyboard`; on start, `reconcile_storyboards` next to `reconcile_posters`; on stop, `release_storyboard_claims`; heartbeat `details` gains `storyboards={n}`.

- [ ] **Step 1: Write the failing tests**:
  - `test_a_poster_is_not_delayed_by_a_running_storyboard`: concurrency 2, slots 1; a fake storyboard extractor that sets a `threading.Event` "started" and then blocks on a `threading.Event` "release" (it runs in a thread through `asyncio.to_thread`); a 120 s video with a ready poster and a pending storyboard row; start `worker.run()` as a task; wait until "started"; insert a new video (poster pending); assert its poster becomes `ready` within a few poll intervals while "release" is still unset; set "release"; `request_stop()`; the storyboard row is `ready`.
  - `test_slots_must_leave_a_place_for_posters`: `MediaWorker(..., concurrency=2)` with `slots=2` raises `ValueError`; with storyboards disabled it does not.
  - `test_a_completed_poster_enqueues_the_storyboard`: poster run with a fake poster extractor on a 120 s video -> a `pending` storyboard row exists.

- [ ] **Step 2: Run, expect FAIL**

Run: `venv/Scripts/python -m pytest tests/test_storyboard_jobs.py -q -k "worker or slots or poster"`

- [ ] **Step 3: Implement the worker changes.**

- [ ] **Step 4: Run, expect PASS; Checkpoint: full suite green**

---

### Task 6: API route, response field and contract

**Files:**
- Modify: `app/backend/enums/routes.py` (`MediaRoutes.MEDIA_STORYBOARD`, next to `MEDIA_THUMBNAIL`)
- Modify: `app/backend/enums/errors.py`: `STORYBOARD_FAILED = ("storyboard_failed", problem(409, "Storyboard generation failed", attempts=int))`, `STORYBOARD_UNSUPPORTED = ("storyboard_unsupported", problem(422, "No storyboard for this media", "Storyboards exist for videos of at least {min_duration_seconds} s while they are turned on", min_duration_seconds=int))`
- Modify: `app/backend/api/v1/models/response/media.py`: `STORYBOARD_PENDING_HEADERS` (`Retry-After`, `X-Storyboard-Status`, `Cache-Control: no-store`), `STORYBOARD_HEADERS` (`Cache-Control`); field `storyboard_status: str = "unsupported"` in `media_info_payload`, `MediaFileInfo` and the list item model next to `thumbnail_status` (value `client_status(media, settings.storage.storyboard)`)
- Modify: `app/backend/api/v1/media.py` (new route `get_media_storyboard`)
- Modify: `api-contract/v1/openapi.json` (rewritten), `api-contract/web-kit/src/api/client.ts` (example `storyboardTrack(mediaId)`: fetch with the token, `URL.createObjectURL(new Blob([text], { type: "text/vtt" }))`)
- Test: `tests/test_media_storyboard.py`

**Interfaces:**
- Consumes: `client_status`, `build_vtt` (Task 2), `storyboard_sheet_url` and `quantised_expiry` (`urls.py`), `VideoStoryboard` (Task 1).
- Produces: `GET /api/v1/media/{media_id}/storyboard`, `response_class=Response`, `responses={**file_response("WebVTT storyboard: one cue per frame, signed sheet URLs with #xywh", ["text/vtt"], STORYBOARD_HEADERS), **empty_response(202, "The storyboard is not generated yet; ask again after Retry-After", STORYBOARD_PENDING_HEADERS), **problems(AccessErrorCode.NO_POST_ASSIGNED, AccessErrorCode.POST_ACCESS_DENIED, ErrorCode.MEDIA_NOT_FOUND, ErrorCode.MEDIA_OBSERVATION_NOT_FOUND, ErrorCode.STORYBOARD_FAILED, ErrorCode.STORYBOARD_UNSUPPORTED, *GUARDED)}`; requirement `require(all=[Permissions.MEDIA_VIEW])` plus `check_post_view`; 200 body from `build_vtt` with `expires_at = quantised_expiry(settings.storage.thumbnail.url_ttl_seconds)` and `Cache-Control: private, max-age={url_ttl_seconds}`.

- [ ] **Step 1: Write the failing tests** in `tests/test_media_storyboard.py` (scene helper like `_scene` in `tests/test_media_thumbnails.py`; rows inserted directly):
  - `test_a_ready_storyboard_is_webvtt_with_signed_sheet_urls`: 200, `content-type` starts with `text/vtt`, body starts with `WEBVTT`, every cue URL starts with `settings.storage.imgproxy.public_prefix + "/"` and has `#xywh=`; `Cache-Control` max-age `<= settings.storage.thumbnail.url_ttl_seconds`.
  - `test_pending_answers_202_with_retry_after` (`Retry-After == "30"`, `X-Storyboard-Status == "pending"`, empty body).
  - `test_failed_is_storyboard_failed` (409, `attempts`).
  - `test_unsupported_cases` (image, 30 s video, `enabled=False` with a ready row -> 422 `storyboard_unsupported` with `min_duration_seconds == 60`).
  - `test_post_scope_and_archived` (outsider 403, archived 404).
  - `test_media_responses_carry_storyboard_status` (`GET` media info of a video with a ready row -> `"ready"`; image -> `"unsupported"`).

- [ ] **Step 2: Run, expect FAIL**

Run: `venv/Scripts/python -m pytest tests/test_media_storyboard.py -q`

- [ ] **Step 3: Implement** the enum member, codes, headers, field, route.

- [ ] **Step 4: Run the new tests, expect PASS; then the contract**

Run: `venv/Scripts/python -m pytest tests/test_api_contract.py -q`
Expected: FAIL naming `get_media_storyboard`, the media schemas with `storyboard_status` and the two problem schemas, nothing else. Then `venv/Scripts/python -m app.backend.api_contract`, rerun: PASS.

- [ ] **Step 5: web-kit type check** (as in the session: scratch copy of `api-contract/web-kit`, `npx openapi-typescript api-contract/v1/openapi.json -o src/api/schema.ts`, `npx tsc --noEmit -p tsconfig.json`). Expected: exit 0.

- [ ] **Step 6: Checkpoint: full suite green**

---

### Task 7: Operations and documentation

**Files:**
- Modify: `app/backend/media_tools.py` (subcommands `storyboard-status` and `reconcile-storyboards [--retry-failed]`, following `poster-status` / `reconcile-posters`)
- Modify: `CLAUDE.md` (media pipeline: "the only precomputed derived file is one poster" becomes posters and storyboards, a storyboard never delays a poster, state in `video_storyboards`, claims through `storyboard_jobs.py`), `DEPLOYMENT.md` (worker resources and the two commands), `API.md` (the route, `storyboard_status`, the two codes), `WEB_TOOLING.md` (the player example), `nginx-app.conf` (comment of `/media-images/`: storyboard sheets too), `CHANGELOG.md` (Added: storyboards; new codes need web translations)
- Test: `tests/test_storyboard_jobs.py::test_media_tools_storyboard_commands`

**Interfaces:**
- Consumes: `reconcile_storyboards`, `storyboard_counts` (Task 4).
- Produces: `async storyboard_status() -> int` and `async reconcile_storyboards_command(retry_failed: bool) -> int` in `media_tools.py` (exit code 0).

- [ ] **Step 1: Write the failing test**: the commands take their session maker from `app.backend.database.get_session_maker` (as `poster_status` does); the test monkeypatches `app.backend.database.get_session_maker` to an async function returning the sqlite `session_maker` fixture and `app.backend.database.close_db` to a no-op, runs both commands, and checks exit code 0 and the printed counts (`capsys`).
- [ ] **Step 2: Run, expect FAIL**
- [ ] **Step 3: Implement the subcommands; update the documents.**
- [ ] **Step 4: Run, expect PASS; Checkpoint: full suite green; `git diff -U0 | grep '^+' | LC_ALL=C grep -c '[^ -~\t]'` prints 0**

---

### Task 8: Live end-to-end check

**Files:**
- Modify: `tests/docker/e2e.py`

- [ ] **Step 1: Extend the video section**: after the poster check, poll `f"{API}/media/{vid}/storyboard"` (the e2e script runs outside the app and builds its URLs from its `API` constant, as every other check there does) until 200 or a 120 s timeout; assert `WEBVTT`; fetch the first cue URL through nginx (no token) -> 200 `image/jpeg`; the sample video is shorter than 60 s, so set `MYSETTINGS_STORAGE__STORYBOARD__MIN_DURATION_SECONDS: "1"` in `tests/docker/compose.test.yml` for the worker and api.
- [ ] **Step 2: Run the stack** (Docker Desktop must be running; docker.exe path in memory `bash-heredoc-backslashes`):

```bash
docker build -t watch-web .
docker compose -f tests/docker/compose.test.yml up -d
venv/Scripts/python tests/docker/e2e.py
docker compose -f tests/docker/compose.test.yml down -v
```

Expected: exit code 0, the storyboard checks listed as passed.
- [ ] **Step 3: Checkpoint: full suite green; report to the user what changed and that nothing is committed.**
