# Video storyboards (seek previews) - design

Status: approved in conversation section by section (2026-10-06); this document awaits review.

## Goal

When a user hovers or drags over the seek bar of a video player, the player shows the frame of that moment.
Industry form: a storyboard, i.e. sprite sheets (a grid of small frames in one JPEG) plus a WebVTT file whose cues
point at a frame with a media fragment (`sheet.jpg#xywh=x,y,w,h`). Players read this format natively or with a
standard plugin (video.js VTT thumbnails, Plyr `previewThumbnails`, Vidstack `thumbnails`).

Success:

- a ready video gets a storyboard in the background, without anyone asking for it;
- a newly uploaded video gets its poster as fast as today: storyboards never delay posters;
- the client needs no tricks: sprite images load in plain `<img>` / CSS without a token.

## Decisions taken

| Question | Decision |
|----------|----------|
| Video durations | Very different (seconds to hours): the frame step adapts to the duration |
| Priority against posters | Reserved capacity: storyboards run only while the poster queue is empty and take at most `slots` places, always fewer than the poster concurrency, so one place is always free for a poster |
| Where the state lives | Own table `video_storyboards` (approach A), not columns on `media_files` |
| Delivery of sprites | Signed, expiring public URLs through imgproxy and the nginx cache, the existing mechanism for images made from media |
| Name of that public path | `/media-images/` (renamed from `/t/`, done before this feature) |
| How the web client plays video today | Unknown: design for a plain player without tricks |

Out of scope, separate sub-project: `GET /media/{id}/stream` needs `Authorization: Bearer`, which a plain
`<video src>` cannot send. Playing videos without tricks needs signed, expiring stream URLs (the same Valet Key
pattern as `/media-images/`). Storyboards are designed not to depend on how that is solved.

## 1. Generation and scheduling

### Enqueueing

- When the media worker completes a poster successfully (the duration is known then) and the video lasts at least
  `storyboard.min_duration_seconds`, it inserts a `pending` row into `video_storyboards`.
- Existing videos, and videos uploaded while the feature was off, are picked up by reconciliation (worker start and
  `media_tools reconcile-storyboards`).

### Priority

- Each loop turn the worker tries to claim a poster first.
- Only when no poster was claimed, and fewer than `storyboard.slots` storyboards are running, it claims a storyboard.
- `storyboard.slots` must be below `poster.concurrency`; otherwise the worker refuses to start (configuration error).
- ffmpeg for storyboards runs with `-threads storyboard.threads` and, on POSIX, a lowered priority
  (`storyboard.nice`, 0 = unchanged).

### Extraction

A first ffmpeg pass that decodes keyframes only (see the fallback below):

```
ffmpeg -nostdin -hide_banner -skip_frame nokey -i <original> -an -sn -dn \
       -vf "tpad=stop_mode=clone:stop_duration=<duration + 2 * step>,fps=1/<step>,trim=end_frame=<frame_count>,scale=<tile_width>:-2,tile=<columns>x<rows>" \
       -q:v <jpeg_qscale> <tmpdir>/sheet-%03d.jpg
```

- Frame `i` stands for time `i * step`. Where keyframes are sparser than the step, neighbouring tiles repeat; this
  is accepted.
- Both passes repeat the last decoded frame past the container duration (`tpad`; keyframes-only decoding ends at
  the last keyframe, which a default GOP puts seconds before the end) and stop at `frame_count` (`trim`), so exactly
  `frame_count` frames are written, late tiles repeat the last frame and no cue points at the black that pads the
  last sheet.
- Keyframe-only decoding does not guarantee a frame: a short video with a single keyframe writes nothing (found by
  the live e2e run). When the keyframe pass writes no sheet, one full-decode pass (the same command without
  `-skip_frame nokey`) runs within the remaining time of the same timeout, with the same cancel handling. Other
  failures (an unreadable source) are not retried this way.
- `frame_count = ceil(duration / step)`, `sheet_count = ceil(frame_count / (columns * rows))`.
- `tile_height` is read from the first written sheet (`sheet height / rows`), never computed from the aspect ratio.

### Adaptive step

`step = round_up_to_nice(max(min_step_seconds, duration / max_frames))`, nice values 1, 2, 5, 10, 15, 30, 60, 120,
300, 600 seconds. Defaults `max_frames = 300`, `min_step_seconds = 2`, tile 160 px wide, grid 10 x 10. Examples:
1 min -> step 2 s, 30 frames, 1 sheet; 2 h -> step 30 s, 240 frames, 3 sheets.

### Reliability

- Lease and renewal while ffmpeg runs, timeout, retries with backoff and a final `failed`, as for posters.
- Output goes to a temporary directory next to the target and is renamed into place atomically:
  `MEDIA_ROOT/_derived/storyboards/{id // 1000:03d}/{id}/sheet-000.jpg ...`. A half-written storyboard is never
  visible.
- Archived videos are never claimed; their files stay (archiving is reversible).

## 2. Data

### Table `video_storyboards`

| Column | Purpose |
|--------|---------|
| `media_id` | Primary key, foreign key to `media_files.id` |
| `status` | `pending` / `processing` / `ready` / `failed`: new domain enum `StoryboardJobStatus` in `enums/general.py` |
| `attempts`, `locked_until`, `worker`, `error`, `generated_at` | Job state, claimed with a conditional UPDATE (sqlite tests keep working, no SKIP LOCKED). `error` is for logs and administrators only, never in an API response |
| `step_seconds`, `frame_count`, `columns`, `rows`, `tile_width`, `tile_height`, `sheet_count` | Layout, written on success; the API builds the WebVTT from it without touching the files |

Index on `(status, locked_until)` for claiming.

### Migration `017_video_storyboards`

- Creates the table; the table goes into `TABLES_OWNED_BY_LATER_REVISIONS` of `002_sync_schema` and `create_table`
  is guarded with an inspector check (CLAUDE.md, "Migration rule after 002_sync_schema").
- Downgrade drops the table and leaves files on disk.

### Reconciliation

- `ready` without its files -> `pending`.
- `processing` with an expired lease -> claimable again.
- A non-archived video with a ready poster and a known duration of at least the threshold, without a row -> new
  `pending` row.
- Nothing is created while `storyboard.enabled` is false.

## 3. API and delivery

### Route

`GET /api/v1/media/{media_id}/storyboard`, enum member `MediaRoutes.MEDIA_STORYBOARD = "/{media_id}/storyboard"`.
Requirement `require(all=[Permissions.MEDIA_VIEW])` plus `AccessControlService.check_post_view`, like the
thumbnail route; archived media answers 404.

| State | Answer |
|-------|--------|
| ready | `200 text/vtt; charset=utf-8`, declared with `file_response(...)` |
| pending / processing | `202`, `Retry-After: storyboard.retry_after_seconds`, `X-Storyboard-Status`, empty body |
| failed | `409 storyboard_failed` (new `ErrorCode`, param `attempts`) |
| unsupported | `422 storyboard_unsupported` (new `ErrorCode`, param `min_duration_seconds`): not a video, shorter than the threshold, or storyboards turned off |

### WebVTT body

```
WEBVTT

00:00:00.000 --> 00:00:02.000
https://<origin>/media-images/<signature>/<options>/exp:<t>/<source of sheet-000>.jpg#xywh=0,0,160,90
```

- One cue per frame; the last cue ends at the duration.
- Sheet URLs are built by `services/media/urls.py` like the other public URLs: imgproxy signature, `exp:` from
  `quantised_expiry(thumbnail.url_ttl_seconds)`, `public_base_url` + `public_prefix`. The processing options keep
  the sheet as it is (`rs:fit:0:0:0:0`, width and height 0 mean the source size) and the `jpg` extension keeps it a
  JPEG, so the `#xywh` coordinates stay valid.
- `Cache-Control: private, max-age` no longer than the lifetime of the URLs inside.
- nginx needs nothing new: `/api/` reaches the route, `/media-images/` serves the sheets with the expiry-respecting
  cache.

### Media responses

New field `storyboard_status` (`ready`, `pending`, `processing`, `failed`, `unsupported`; new client-facing enum
`StoryboardStatus`, separate from the four-state `StoryboardJobStatus` stored in the table), derived when the row is
read, like `thumbnail_status`. Without a row:

| The video | `storyboard_status` |
|-----------|---------------------|
| not a video, or storyboards turned off | `unsupported` |
| duration known and below the threshold | `unsupported` |
| poster failed | `failed` |
| poster pending or processing | `pending` |
| poster ready, duration unknown (probe found none) | `unsupported` (the step needs the duration) |
| poster ready, duration at least the threshold | `pending` (reconciliation adds the row) |

### Client

Fetch the VTT with the bearer token, turn it into a `blob:` URL, give it to the player as its thumbnails track.
Example in `api-contract/web-kit/src/api/client.ts` and `WEB_TOOLING.md`.

### Contract

New operation, new field, two new problem codes: `api-contract/v1/openapi.json` is rewritten on purpose, CHANGELOG
entry, the web locales need `storyboard_failed` and `storyboard_unsupported`.

## 4. Configuration and operations

`[storage.storyboard]` in `app/config.toml`, `StoryboardSettings` in `config.py`, every key as
`MYSETTINGS_STORAGE__STORYBOARD__*` in `ENV.md`:

| Key | Default | Meaning |
|-----|---------|---------|
| `enabled` | `true` | Off: no claims, the API answers `unsupported`, existing files stay |
| `slots` | `1` | Storyboards at once; must be below `poster.concurrency` |
| `min_duration_seconds` | `60` | Shorter videos get none |
| `max_frames`, `min_step_seconds` | `300`, `2` | Bounds of the adaptive step |
| `tile_width`, `columns`, `rows` | `160`, `10`, `10` | Tile size and grid |
| `jpeg_qscale` | `5` | ffmpeg `-q:v` (2 best, 31 worst) |
| `threads`, `nice` | `1`, `10` | ffmpeg threads and POSIX niceness (0 = unchanged) |
| `timeout_seconds`, `lease_seconds` | `1800`, `300` | Time limit per video; lease renewed while running |
| `max_attempts`, `backoff_seconds` | `3`, `[60, 300, 1800]` | Retries after a failure |
| `retry_after_seconds` | `30` | `Retry-After` of the 202 |

Operations:

- `python -m app.backend.media_tools storyboard-status`: counts per state and the latest errors.
- `python -m app.backend.media_tools reconcile-storyboards [--retry-failed]`.
- The worker heartbeat details include the number of running storyboards.

Disk: a 10 x 10 sheet of 160 px tiles is roughly 100-200 KB; a two-hour recording about 0.5 MB.

Documentation: CLAUDE.md (the rule "the only precomputed derived file is one poster per video" becomes "posters and
storyboards; a storyboard never delays a poster"), ENV.md, DEPLOYMENT.md (worker resources), WEB_TOOLING.md,
API.md, CHANGELOG.md.

## 5. Testing

Written before the code, following `test_poster_jobs.py`, `test_posters_ffmpeg.py`, `test_media_thumbnails.py`:

- Layout and VTT (pure): adaptive step examples and rounding, `max_frames` bound, cue times and `#xywh` across
  several sheets, the last cue ends at the duration, URLs signed with `exp:` under `/media-images/`.
- Queue (sqlite): enqueued only after a ready poster and only for long videos; posters always claimed before
  storyboards; the `slots` limit; lease, retries, `failed`; archived rows never claimed; every reconciliation case;
  `enabled = false`.
- Main success criterion: a worker with a fake extractor processes a newly arrived poster while a storyboard is
  running; a configuration with `slots >= concurrency` refuses to start.
- Real ffmpeg (skipped when ffmpeg is missing): a generated test video gives sheets of the expected size, the tile
  height read from the file, the expected frame count.
- API: 200 `text/vtt`, 202 with `Retry-After`, 409, 422 (image, short video, turned off), 403 outside the post
  scope, 404 archived, `storyboard_status` in media responses. The existing guards catch undeclared problem codes and
  non-problem errors.
- Migration: a fresh install reaches `017`; an upgrade from `016` creates the table.
- Contract: rewritten `openapi.json`; web-kit type check (`tsc`) with the new field.
- Live: `tests/docker/e2e.py` uploads a video, waits for the storyboard, fetches the VTT and one sheet through nginx
  `/media-images/` (needs an image rebuild).
