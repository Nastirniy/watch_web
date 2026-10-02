# API for the web client

Everything below is generated from the code of the API, so it is always current for the deployment you open.

| URL | What |
|-----|------|
| `/docs` | Swagger UI: every operation, its request, every success response and every error it can answer |
| `/redoc` | The same, as a reading-friendly page |
| `/openapi.json` | OpenAPI 3.1 document for generators |
| `/problems` | Every error type (code, status, title, params) and the operations that answer it |

## Typed client

Setup, contract checks and translation checks step by step: `WEB_TOOLING.md` (starter files in `api-contract/web-kit/`).

```bash
npx openapi-typescript https://<api-origin>/openapi.json -o src/api/schema.ts
```

With [openapi-fetch](https://openapi-ts.dev/openapi-fetch/) the paths, bodies, success responses and errors are typed:

```ts
import createClient from "openapi-fetch";
import type { paths, components } from "./api/schema";

type Problem = components["schemas"]["Problem"];

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

Every error, whatever the status, answers `Content-Type: application/problem+json`:

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

- `components.schemas.ProblemCode` in `/openapi.json`: every problem code;
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

- `invalid_token` or `session_expired`: refresh with `POST /api/v1/auth/refresh`, then repeat the request.
- `credentials_required`, `account_not_found`, `account_deactivated`, `account_deleted`, `invalid_refresh_token`: log in again.

### Not allowed (403)

The body adds `required` (`{all, any, admin}`) and `missing` (permissions the user lacks). `GET /api/v1/auth/route-access` tells in advance which routes the user may call.

### Server errors (500 `internal_error`)

The body carries `error_id`; the server log has the same id with the details. Show it to the user so a report can be matched.

## Responses that are not JSON

| Operation | Response |
|-----------|----------|
| `GET /api/v1/media/{media_id}/download`, `/stream` | The file in its own Content-Type; `206` for a `Range` request |
| `GET /api/v1/media/{media_id}/thumbnail` | The image; `202` with `Retry-After` while a video poster is being extracted |
| `POST /api/v1/observations/report` | An `.xlsx` file |
| `PATCH /api/v1/media/uploads/{upload_id}` | `204`, the result is in the `Upload-*` headers |

Resumable uploads step by step: `UPLOADS.md`.
