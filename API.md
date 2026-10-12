# API for the web client

Everything below is generated from the code of the API, so it is always current for the deployment you open.

| URL | What |
|-----|------|
| `/api/v1/docs` | Swagger UI of v1: every operation, its request, every success response and every error it can answer |
| `/api/v1/redoc` | The same, as a reading-friendly page |
| `/api/v1/openapi.json` | OpenAPI 3.1 document of v1 for generators |
| `/problems` | Every error type (code, status, title, params) and the operations that answer it |

Every API version has its own document next to its routes (`/api/v2/openapi.json` once v2 exists): a type
name is unique within a version, so a new version never renames a type of v1.

## Typed client

Setup, contract checks and translation checks step by step: `WEB_TOOLING.md` (starter files in `api-contract/web-kit/`).

```bash
npx openapi-typescript https://<api-origin>/api/v1/openapi.json -o src/api/schema.ts
```

With [openapi-fetch](https://openapi-ts.dev/openapi-fetch/) the paths, bodies, success responses and errors are typed:

```ts
import createClient from "openapi-fetch";
import type { paths, components } from "./api/schema";

type Schemas = components["schemas"];
// Every problem type is a component schema <Code>Problem; together they are the union of all problems
type Problem = { [K in keyof Schemas]: K extends `${string}Problem` ? Schemas[K] : never }[keyof Schemas];

const api = createClient<paths>({ baseUrl: "https://<api-origin>" });

const { data, error } = await api.PUT("/api/v1/posts/{post_id}", {
  params: { path: { post_id: 7 } },
  body: { code: "NEW-01" },
});
if (error) {
  switch (error.code) {                  // narrowed to the problems of this operation
    case "post_code_taken": /* ... */ break;
    case "validation_error": showFieldErrors(error.errors); break;
  }
}
```

Operation ids are the endpoint names (`update_post`, `create_user`, ...), so generators that name functions
after them (orval, openapi-generator) produce readable names.

## Errors: RFC 9457 Problem Details

Two kinds of error reach the client, told apart by `Content-Type`:

| Response | Who answered | Handle it by |
|----------|--------------|--------------|
| `Content-Type: application/problem+json` | the API | its `code` and params (below) |
| any other content type with a 4xx / 5xx status | the infrastructure in front of the API (nginx: TLS client certificate, request size, the API not reachable or too slow, file serving) | the HTTP status only (RFC 9110): a generic message per status; never parse the body |
| no response (the request failed) | the network | a generic connection message |

Every error the API makes is a problem; the API never answers an error in another format, and the infrastructure never answers in this one. Where nginx and the API check the same thing (body size, whether a file exists), the API decides, so the client gets the problem with its params.

Every response carries `X-Request-ID`, the infrastructure's own errors included: one id per request, in the nginx access log and in the API log. Show it with any error so a report can be matched; for a 500 it is also the `error_id` of the body.

A problem looks like this:

```json
{
  "type": "https://<api-origin>/problems/post_not_found",
  "title": "Post not found",
  "status": 404,
  "detail": "Post not found",
  "instance": "/api/v1/posts/7",
  "code": "post_not_found",
  "post_id": 7
}
```

| Member | Use it for |
|--------|------------|
| `code` | The translation key. One code is exactly one message, never a group of messages. |
| other members | Values for the translation (`post_id`, `name`, `offset`, `max_file_size_bytes`, ...) and for client logic. Typed per code in the schema; an optional member is left out, never `null`. |
| `status` | Same as the HTTP status. |
| `title` | English summary, fixed per code. |
| `detail` | English text of this occurrence (`title` with the values filled in). Fallback when a translation is missing. |
| `type` | Link to the documentation of the code. |
| `instance` | Path of the request that failed. |

The localization package needs one entry per code:

- every problem code: the `code` constant of each component schema `<Code>Problem` in `/api/v1/openapi.json` (`Problem["code"]` in TypeScript, see `WEB_TOOLING.md`);
- `components.schemas.FieldErrorCode`: codes of invalid values inside `validation_error`;
- pydantic's codes for built-in checks inside `validation_error` (`missing`, `string_too_short`, `string_too_long`, `int_parsing`, `enum`, `json_invalid`, ...; see https://docs.pydantic.dev/latest/errors/validation_errors/). Their params (`min_length`, `max_length`, `expected`, ...) are members of the item.

### Invalid input (422 `validation_error`)

```json
{
  "type": "https://<api-origin>/problems/validation_error",
  "title": "Request validation failed",
  "status": 422,
  "detail": "The request has 1 invalid value(s)",
  "instance": "/api/v1/users/",
  "code": "validation_error",
  "error_count": 1,
  "errors": [
    {"code": "string_too_short", "detail": "String should have at least 3 characters", "pointer": "#/username", "min_length": 3}
  ]
}
```

- `pointer` is a JSON Pointer into the body: `#/username` is the `username` field of the form, `#/frequencies/0/frequency_min` a nested one. Map it to the form field that shows the message.
- Query, path, header and cookie values come as `parameter` + `location` instead (`{"parameter": "page", "location": "query"}`).
- The submitted value is never echoed back.

### Authentication (401)

Send `Authorization: Bearer <access token>` from `POST /api/v1/auth/login`.

- `invalid_token` (the access token expired): refresh with `POST /api/v1/auth/refresh`, then repeat the request.
- `session_expired`: the session itself ended (logout, no request for `session_idle_timeout_minutes`, the optional hard limit `session_max_hours`, or revoked by the server); a refresh cannot bring it back: log in again. With a client certificate the login needs no user input, so the client can do it by itself.
- `token_certificate_mismatch`: the token belongs to a session opened with another client certificate (the browser presented a different one, or the token was copied), or the certificate of the session is no longer the user's (it was replaced; the session ends); log in again with the current certificate.
- `credentials_required`, `account_not_found`, `account_deactivated`, `account_deleted`, `invalid_refresh_token`: log in again.

Session lifetime: a session lives while it is used. It ends after `security.session_idle_timeout_minutes` (30) without a request; every request and every refresh moves that end (`expires_at` of the session). `security.session_max_hours` can add a hard limit since the login (0, the default: none). Every refresh issues a new access and refresh token; a refresh token stays usable for the access token lifetime plus the idle timeout, so a client that refreshes when its access token expires never loses an active session.

A session opened through nginx with a client certificate is bound to it: every request and refresh must present the same certificate, so a copied token is useless elsewhere (`token_certificate_mismatch`). `GET /auth/sessions` lists the caller's live sessions with `status` (`online` / `idle`); holders of `user.view` covering a user see that user's sessions, ended ones with `end_reason` / `revoke_cause`, under `/users/{id}/sessions`.

### Not allowed (403)

The body adds `required` (`{all, any}`) and `missing` (permissions the user lacks). `GET /api/v1/auth/route-access` tells in advance which routes the user may call.

| Code | When |
|------|------|
| `permission_denied` | The route's permissions are held nowhere (no assignment gives them; an assignment on an archived post or at an archived group gives nothing) |
| `post_access_denied` / `post_write_denied` | The permission is held, but not in effect at this post (`post_id`; `*.view` / any other permission) |
| `no_post_assigned` | The permission is held, but in effect at no post at all (for example only at a scope group without posts) |
| `permission_required_for_action` | A permission a part or a target of the request needs (`permission`, `action`): `include_deleted`, the group a post is created in or moved to, the old and the new parent of a moved scope group (`post.update`), the home post of a new or moved user, a media file without an observation (`media.update` at the root) |
| `user_self_only` | A user record outside the caller's `user.view` |
| `target_user_more_privileged` | A change to a user the caller's `user.*` does not cover, or who holds an assignment the caller could not grant |
| `grant_exceeds_own_permissions` | Granting or revoking permissions the caller does not hold (at the node, for an assignment; at every node the role is assigned at, for permissions added to a role and the restore of a role) |

### Server errors (500 `internal_error`)

The body carries `error_id`, the request id of the response (`X-Request-ID`); the server log has it once, with the details. Show it to the user so a report can be matched.

## Authorization: pages, buttons, scope

Access is role assignments: a user holds a role at one node of the scope tree, the root (`/`, the whole system), a
scope group (a group of posts, groups nest) or a post, and it applies to that node and everything below it.
Assignments add up. Every route checks its own permissions where it acts; there is no administrator flag.

The login, the refresh and `GET /api/v1/auth/me` carry what the interface is built from:

| Field | Use it for |
|-------|------------|
| `user_type` | Which pages the web shows. The catalog is `GET /api/v1/user-types/` (`key`, `display_name`, `description`, `base_role`, `base_permissions`, `allowed_permissions` = the ceiling of the type). The API never authorizes by type: a page still checks its buttons. |
| `permissions` | Permissions held at some node (an assignment on an archived post or at an archived group counts nowhere): the menu (an entry whose route the user can call somewhere). |
| `root_permissions` | Permissions held at the root: in effect at every post. |
| `post_permissions` | `{"7": ["observation.create", ...]}`: per post (key: the post id as a string), the permissions that assignments at groups and at the post put in effect there, for every post they reach. |
| `assignments` | The user's assignments, as `GET /users/{user_id}/assignments` lists them. |
| `post_id` | Home post: membership (who manages the user); it grants nothing by itself. |

Buttons: the permissions in effect at post 7 are `root_permissions` plus `post_permissions["7"]` (a post missing from
`post_permissions` has `root_permissions` only). Show an action on the data of a post (create an observation, upload,
archive) when its permission is in effect there. Lists need no filtering in the client: the API answers only the
posts and rows where the permission is in effect. `POST /api/v1/auth/check-permission` with `{all, any, post_id}`
answers the same question on the server (`allowed` when in effect at that post; without `post_id`: held at some node).

An assignment change applies to the user's next request; the fields above are as fresh as the last login, refresh or
`/auth/me`, so read `/auth/me` again after changing the current user's access. A type change ends every session of
the user (`session_expired`, `revoke_cause` `user_type_changed`): the new pages come with the next login.

### Scope picker and assignments

- `GET /api/v1/scope-groups/tree` (`scope_group.view`): the part of the tree the caller sees, for choosing a node:
  `{groups: [{id, name, description, groups: [...], posts: [{id, name, code}]}], posts: [...]}`; posts only where
  `post.view` is in effect; archived groups and posts, and everything below an archived group, are left out. The
  root itself is not in the tree: offer it when the caller holds the permissions at the root (`root_permissions`).
- `GET /api/v1/users/{user_id}/assignments`: `[{id, role: {id, name, display_name, is_builtin, is_deleted}, scope:
  {kind: "root" | "group" | "post", id, name}, created_at}]` (`id` is null at the root; an assignment of an archived
  role, `role.is_deleted`, grants nothing).
- `POST /api/v1/users/{user_id}/assignments` (201): `{"role_id": 5, "scope": {"kind": "post", "id": 7}}`; `id` is
  left out for `{"kind": "root"}`. `DELETE /api/v1/users/{user_id}/assignments/{assignment_id}` (204).
- The caller may assign only a role whose permissions they hold at that node (and `user.update` there). A role with a
  `system` permission (`RoleResponse.level` `system`) is assignable only at the root; a role must fit the ceiling of
  the user's type (`allowed_permissions`). Offer only roles that pass these checks.
- A new user (`POST /api/v1/users/`, form fields `user_type` and an optional `post_id`) gets the built-in role of the
  type at the home post (at the root without one, or for the `admin` type); add more with the assignment routes.
  `PATCH /api/v1/users/{user_id}` with `user_type` changes the type and replaces the old built-in role with the new
  one at the same nodes, or at the root when the new built-in role holds system permissions (the `admin` type).
- Built-in roles (`is_builtin`, `builtin_key`: one per type) are read-only; `POST /api/v1/roles/{role_id}/clone`
  makes an editable copy.

### Problem codes of authorization and scope

| Code | Status | When |
|------|--------|------|
| `assignment_not_found` | 404 | The assignment is not one of this user's |
| `assignment_exists` | 409 | The user already holds this role at this node |
| `last_assignment_required` | 409 | The user's last assignment (of a live role) cannot be removed; also the permanent delete of a role that is the only assignment of an archived holder |
| `role_requires_root_scope` | 409 | A role with system permissions below the root (`system_permissions`) |
| `role_exceeds_user_type` | 409 | The role holds permissions the user's type does not allow (`user_type`, `excess_permissions`) |
| `user_type_ceiling_exceeded` | 409 | A type change while other assignments exceed the new type (`user_type`, `assignments` with their `excess_permissions`) |
| `role_holders_exceed_user_type` | 409 | Permissions added to a role that the type of a holder does not allow (`users`) |
| `builtin_role_not_renamable`, `builtin_role_permissions_fixed`, `builtin_role_not_archivable`, `builtin_role_not_deletable` | 409 | Built-in roles cannot change (clone one) |
| `scope_group_not_found` | 404 | Unknown, archived or outside the caller's `scope_group.view` |
| `scope_group_not_archived` | 409 | Restore of a group that is not archived |
| `scope_group_not_empty` | 409 | Archive of a group that still has live groups or posts |
| `scope_group_cycle` | 409 | A group moved into its own subtree |
| `scope_group_depth_exceeded` | 409 | Groups nested deeper than `max_depth` (6) |
| `scope_group_name_taken` | 409 | The name is used under the same parent |
| `last_admin_required`, `cannot_remove_own_admin` | 409 | The built-in `admin` role at the root must stay with an active user and cannot be taken from oneself |

Field codes inside `validation_error`: `scope_id_required` (a group or post scope without `id`),
`scope_id_not_allowed` (the root with an `id`).

## Responses that are not JSON

| Operation | Response |
|-----------|----------|
| `GET /api/v1/media/{media_id}/download`, `/stream` | The file in its own Content-Type; `206` for a single `Range`; an unsatisfiable one is `416` `range_not_satisfiable` with `Content-Range: bytes */size` |
| `GET /api/v1/media/{media_id}/thumbnail` | The image; `202` with `Retry-After` while a video poster is being extracted |
| `GET /api/v1/media/{media_id}/storyboard` | WebVTT (`text/vtt`) for seek previews; `202` with `Retry-After` and `X-Storyboard-Status` while the worker has not produced it |
| `POST /api/v1/observations/report` | An `.xlsx` file |
| `PATCH /api/v1/media/uploads/{upload_id}` | `204`, the result is in the `Upload-*` headers |

Resumable uploads step by step: `UPLOADS.md`.

### Thumbnails

Every media response carries `thumbnail_status` (`ready`, `pending`, `processing`, `failed`, `unsupported`) and,
while it is `ready`, signed `thumbnail_urls`. `GET /media/{id}/thumbnail` and `POST /media/{id}/share` answer
`unsupported` with a `422` problem that says why:

| Code | When |
|------|------|
| `thumbnails_unsupported` | Audio: there is nothing to show |
| `image_too_large_for_thumbnails` | An image above the renderer's resolution limit (params `megapixels`, `max_megapixels`); the original is stored and downloads as usual |
| `no_video_stream` | A video upload with no video stream (only audio); also the answer of the storyboard route |

`width` and `height` are the size as displayed: a vertical phone video or an EXIF-rotated photo reports the
portrait size, so a player or an `<img>` box can be sized from them.

### Seek previews (storyboards)

Every media response (`MediaFileInfo`) carries `storyboard_status`: `ready`, `pending`, `processing`, `failed` or
`unsupported` (not a video, shorter than `min_duration_seconds`, or storyboards are turned off). Ask for the
track when it is `ready`; the route answers the same states:

| Status | Answer |
|--------|--------|
| ready | `200` WebVTT, one cue per frame, each cue an absolute signed `https://<origin>/media-images/...jpg#xywh=x,y,w,h` URL of a sheet (`public_base_url`, else the origin of the request) |
| pending, processing | `202`, `Retry-After`, `X-Storyboard-Status`, empty body |
| failed | `409` problem `storyboard_failed` (param `attempts`) |
| unsupported | `422` problem `storyboard_unsupported` (param `min_duration_seconds`), or `no_video_stream` for a video file with only audio |

The permission is `media.view` in effect at the post of the media. The sheet URLs need no token (like thumbnails) and expire:
`Cache-Control: private, max-age` of the track is no longer than their lifetime, so fetch the track again after
it. Every problem code here needs a text in the web translations.
