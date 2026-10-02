# Uploading media

Three ways to get a file into the system. Pick by connection quality and file size.

| Route | Best for | On a dropped connection |
|-------|----------|------------------------|
| `POST /media/uploads` + chunks (resumable) | Anything over a few MB, mobile and field links, files above 100 MB | Ask the server for the offset, continue from there. Nothing is lost. |
| `POST /media/upload` (multipart) | Small files on a good link | The whole request fails; the partial body is discarded; start over. |
| `POST /media/nginx-upload` (raw body via nginx) | Server-to-server or LAN transfers of big files | Same as multipart. |

All three need `media.view` + `media.create` and write access to the observation's post, apply the same format registry (`GET /media/formats`) and the same per-format size limit, and end with the same response (`media_file_id`, `thumbnail_status`, `thumbnail_urls`, metadata).

Every error is an RFC 9457 problem (`application/problem+json`, see `API.md`). Decide by `code` (it names exactly one message, use it as the translation key); `detail` is English fallback text; the other members are the values below (`offset`, `session_status`, `accepted_extensions`, ...), at the top level of the body. The tables list the codes.

## Resumable upload, step by step

### 1. Open a session

```http
POST /api/v1/media/uploads
Authorization: Bearer <token>
Content-Type: application/json

{
  "observation_id": 17,
  "media_type": "video",
  "original_filename": "patrol_2026-09-10.mp4",
  "expected_size": 734003200,
  "content_type": "video/mp4",
  "sha256": "9f2c...e1"          // optional, hex of the whole file
}
```

Everything that can be refused is refused here, before a single byte is sent:

| Status | Meaning | What the client does |
|--------|---------|----------------------|
| 201 | Session open. Body: `id`, `received_bytes` (0), `expected_size`, `max_chunk_bytes`, `expires_at`, `urls`. Headers: `Upload-Offset: 0`, `Upload-Length`, `Location`. | Start sending chunks. |
| 415 | Format not accepted: `media_type_unknown`, `media_extension_not_accepted` (`extension`), `media_content_type_not_accepted` or `media_content_type_extension_mismatch` (`content_type`, `expected_content_type`). `Accept-Post` header, `accepted_content_types` and `accepted_extensions` say what is. | Tell the user; do not retry. |
| 413 | `media_file_too_large`: `expected_size` above the format's `max_file_size_bytes` (`max_file_size_bytes`, `content_type`). | Tell the user; do not retry. |
| 404 / 403 | Observation missing, or no write access to its post. | Tell the user; do not retry. |

Store `id` locally (for example in `localStorage` keyed by file name and size) so the upload can continue after a page reload.

### 2. Send chunks

```http
PATCH /api/v1/media/uploads/{id}
Authorization: Bearer <token>
Content-Type: application/offset+octet-stream
Upload-Offset: 0
Content-Length: 8388608

<raw bytes 0 .. 8388607>
```

- Chunk size: `max_chunk_bytes` (64 MB by default) is the hard limit; 4 to 16 MB is the sweet spot. On a bad link use smaller chunks so a failure costs less; on a good link bigger chunks cost fewer round trips.
- Send chunks **sequentially**. `Upload-Offset` must equal the server's `received_bytes`. The server takes one chunk at a time per session.
- The body is raw bytes, not multipart and not base64.

| Status | Meaning | What the client does |
|--------|---------|----------------------|
| 204 | Stored. `Upload-Offset` header is the new offset. | Send the next chunk from that offset. |
| 409 | `upload_offset_mismatch`: `Upload-Offset` does not match. The header `Upload-Offset` and `offset` carry the real offset (`sent_offset` is what you sent). `upload_writer_busy`: another chunk is still being written. | Mismatch: continue from the offset in the header. Busy: wait one second and ask `GET` for the offset. |
| 413 | `upload_chunk_too_large` (`max_chunk_bytes`) or `upload_chunk_overrun` (`expected_size`). Nothing was written. | Fix the chunking. |
| 410 | `upload_session_closed`: session is `completed`, `aborted` or `expired` (`session_status`). | Completed: you are done, `media_file_id` is the file. Otherwise open a new session. |
| 400 | `upload_offset_required` (header missing), `upload_chunk_empty`, or `upload_chunk_not_stored` (the transfer broke mid-chunk; the file was truncated back to `offset`). | Ask `GET` for the offset and resend. |
| 415 | `upload_chunk_content_type`: the body is not `application/offset+octet-stream`. | Fix the request. |
| 401 | Token expired during a long upload. | Refresh the token and resend the same chunk. The session is not affected. |

**On any network error** (timeout, reset, no response): do not assume anything about the chunk. Ask the server:

```http
HEAD /api/v1/media/uploads/{id}     -> Upload-Offset: 41943040, Upload-Status: open
```

or `GET` for the JSON. Then continue from that offset. The server truncates a partially received chunk, so the offset is always a byte boundary the server has durably stored.

Retry policy that works well: on failure wait 1 s, 2 s, 4 s, up to 30 s (with jitter), re-read the offset before each retry, give up after a configurable number of attempts and keep the session `id` for later. The session stays open for `expires_at` (24 hours from creation by default).

### 3. Complete

```http
POST /api/v1/media/uploads/{id}/complete
```

| Status | Meaning | What the client does |
|--------|---------|----------------------|
| 200 | File stored. Same body as the other uploads plus `upload_id`. A video starts with `thumbnail_status: pending` and gets its poster from the worker shortly. | Done. |
| 409 | `upload_incomplete`: bytes still missing (`missing_bytes`, `offset`, `expected_size`). | Send the rest, then complete again. |
| 422 | `upload_checksum_mismatch` (`expected_sha256`, `actual_sha256`). The session is aborted and the bytes deleted. | Open a new session and send again. |
| 415 / 413 | The content did not match the declared format (same 415 codes as on open, plus `media_extension_dangerous`) or `media_file_too_large`. Session aborted. | Tell the user. |
| 400 | `media_file_empty` or `media_content_invalid` (the file header does not match its type). Session aborted. | Tell the user. |
| 410 | `upload_session_closed`, or `upload_bytes_missing` (the stored bytes are gone; the session is aborted). | Open a new session. |
| 500 | `upload_complete_failed` or `media_store_failed`: storage failed. | Retry `complete` later; if the session was aborted, open a new one. |
| 503 | `media_uploads_busy`: too many uploads are being stored at once. | Retry `complete` after a few seconds. |

Completing an already completed session returns 200 with the same `media_file_id`, so a lost response to the first `complete` is harmless.

### 4. Give up

`POST /api/v1/media/uploads/{id}/abort` deletes the received bytes and marks the session `aborted`. Sessions that are neither completed nor aborted expire after `expires_at`; the media worker removes their bytes.

### After a page reload

`GET /api/v1/media/uploads?status=open` lists the caller's open sessions with `received_bytes`, `expected_size`, `original_filename` and `observation_id`. Match them to the files the user picks again and continue.

## What the server guarantees

- `received_bytes` only grows, only by fully written chunks, and only through one writer at a time. A duplicated or retried request cannot corrupt the file; the worst case is a 409 with the right offset.
- A chunk that fails mid-transfer is truncated away; the offset the server reports is always safe to continue from.
- Format, declared type and size are checked when the session opens, content and hash when it completes. A file that is refused at completion was already refused for a reason the client could not have fixed by resending.
- Sessions belong to the user who opened them. `media.manage_any` may inspect and complete any session. Post access is re-checked on every call.
- Every open, abort and completion is audited.

## Sizes and timeouts to keep in mind

| Item | Value | Where |
|------|-------|-------|
| Max chunk | 64 MB | `storage.upload.max_chunk_bytes`, nginx `client_max_body_size` on the uploads location |
| Recommended chunk | 8 MB | `storage.upload.recommended_chunk_bytes` |
| Session lifetime | 24 h since creation | `storage.upload.session_ttl_hours` |
| Per-format file limit | see `GET /media/formats` | `media_formats.max_file_size_bytes` |
| nginx request timeout for a chunk | 300 s | `proxy_read_timeout` on the uploads location |

## The one-shot routes

`POST /media/upload` (multipart, fields `observation_id`, `media_type`, `file`) and `POST /media/nginx-upload` (raw body, query `observation_id`, `media_type`, header `X-Original-Filename`) are single requests. The API streams the body to disk and never holds it in memory, and nginx allows ten minutes without traffic before it closes the connection, so a slow link works as long as it stays up. If the connection drops the request fails, the partial data is discarded and the client starts again. Use them for small files or reliable links; use sessions for everything else.
