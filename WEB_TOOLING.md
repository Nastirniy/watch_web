# Web tooling: typed API client, contract checks, error translations

How the web project takes the API contract from this backend and checks itself against it with the stock
tools only: openapi-typescript 7, TypeScript 5 (strict) and openapi-fetch 0.17. There is no extra catalog and
no custom checker: everything comes from the OpenAPI document FastAPI generates. Every command below was run
on a clean project. Background on the error format: `API.md`.

## 1. The contract: one file per API version

`api-contract/v1/openapi.json` in this repository is the OpenAPI document of API v1, committed with every
change of the API (the backend tests fail until it matches the code). A running API serves the same
document at `/api/v1/openapi.json`. Every version has its own document (`api-contract/v2/openapi.json`,
`/api/v2/openapi.json` once v2 exists): type names and operation ids are unique within a version, so a new
version never renames a type of v1.

```bash
# in the web project: take the contract of the backend version you build against
curl -o api-contract/v1/openapi.json https://<api-origin>/api/v1/openapi.json
# or copy api-contract/v1/openapi.json from this repository
```

Commit it in the web project too: a diff of `openapi.json` in a pull request is the change of the API it
adopts. To rewrite it in this repository without a server: `python -m app.backend.cli.api_contract`.

When the web client starts using v2 for some resources, generate its types into a second file
(`openapi-typescript ./api-contract/v2/openapi.json --default-non-nullable false -o ./src/api/schema-v2.ts`) and create a second
openapi-fetch client from it; the calls that stay on v1 keep the first client.

## 2. Set up the web project once

```bash
npm install openapi-fetch@0.17
npm install -D openapi-typescript@7 typescript@5
```

Copy from `api-contract/web-kit/` of this repository:

| File | What it does |
|------|--------------|
| `src/api/client.ts` | openapi-fetch client; the `Problem`, `ProblemCode`, `FieldErrorCode` types derived from the generated schema; `problemText()` and `fieldMessages()` (translation with English fallback, form field from `pointer`); `apiFailure()` and `infrastructureText()` (an error the API did not answer, see section 4) |
| `src/api/contract-check.ts` | Compile-time checks of the contract (section 4); not imported by the app |
| `src/i18n/messages.ts` | The `Messages` type a locale is checked against (section 5) |
| `tsconfig.json` | `strict` and `noImplicitReturns` are required by the checks; merge into your own tsconfig |

`package.json` scripts:

```json
{
  "scripts": {
    "api:types": "openapi-typescript ./api-contract/v1/openapi.json --default-non-nullable false -o ./src/api/schema.ts",
    "api:check": "tsc --noEmit -p tsconfig.json",
    "api:all": "npm run api:types && npm run api:check"
  }
}
```

`--default-non-nullable false` is required. openapi-typescript 7 makes every field with a default non-optional, so a
request body would have to send `force` of `PUT /posts/{id}` and every other defaulted field. The API lists every
response field with a default in `required` itself, so with the flag responses stay typed as present and request
fields with a default stay optional. Only members that are left out of a body (`scope` of an event, audit `params`)
are optional in a response.

## 3. Every time the API changes

```bash
# 1. take the new api-contract/v1/openapi.json (section 1)
# 2. then:
npm run api:all
```

- `api:types` regenerates `src/api/schema.ts` (commit it with the contract).
- `api:check` type-checks the whole app against it, translations included: every call site of a changed
  path, body, response or error and every missing or obsolete translation fails here.

## 4. What the type check catches

The generated types are exact: each operation's errors are a union of its problem types, discriminated by `code`.

```ts
const { data, error } = await api.PUT("/api/v1/posts/{post_id}", { params: { path: { post_id: 7 } }, body: { code: "NEW-01" } });
if (error) {
  switch (error.code) {          // only the codes this operation can answer
    case "post_code_taken": ...
    case "validation_error": error.errors ...   // FieldError[] with pointer / parameter
  }
}
```

The types cover what the API answers. An error the API did not answer (nginx: client certificate, request
size, the API not reachable or too slow; API.md, "Errors") has no `application/problem+json` and is not one of
these types, whatever the type of `error` says. Tell them apart first, by the response:

```ts
const { data, error, response } = await api.PUT(...);
if (error) {
  const failure = apiFailure(response, error);
  if (failure.kind === "infrastructure") return infrastructureText(failure, t);   // by status only
  switch (failure.problem.code) { ... }                                            // the API's problem
}
```

A request that got no response at all makes openapi-fetch throw (the `fetch` rejection): catch it and show
`infrastructure.network_error`.

Every problem type is a component schema `<Code>Problem` (`PostNotFoundProblem`). `client.ts` joins them into
the union of all problems with plain TypeScript:

```ts
type Schemas = components["schemas"];
export type Problem = { [K in keyof Schemas]: K extends `${string}Problem` ? Schemas[K] : never }[keyof Schemas];
export type ProblemCode = Problem["code"];
export type FieldErrorCode = Schemas["FieldErrorCode"];
```

`src/api/contract-check.ts` turns the risky changes into build errors:

| Change in the API | Error in `npm run api:check` |
|-------------------|------------------------------|
| A new code for a status the app handles exhaustively (the 401 switch) | `Function lacks ending return statement` |
| A param renamed or retyped (`offset` -> `current_offset`) | `Property 'offset' does not exist on type ...` at every use |
| A code removed from an operation | `Type '"<code>"' is not comparable to type ...` at the `case` that handles it |
| The types degrade to `any` (broken generation) | `Unused '@ts-expect-error' directive`: the lines marked `@ts-expect-error` must stay errors |

Write the handling of important statuses as exhaustive `switch` statements with a declared return type,
the way `onUnauthorized` does, and the compiler tells you about every new code.

Lists and searches answer 422 `validation_error` (item code `extra_forbidden`) for a query parameter they do not
know (API.md, "Lists and filters"): send only parameters of the contract, and no cache-busting `_=` or tracking
parameters. Build the query through the typed client, never by hand, and keep its type, so a removed or misspelt
filter is a build error in `npm run api:check` (excess property) instead of a 422 in production:

```ts
type ObservationQuery = NonNullable<paths["/api/v1/search/observations"]["get"]["parameters"]["query"]>;
const query: ObservationQuery = { status_in: ["active"], last_n_days: 7 };   // a typo here fails tsc
await api.GET("/api/v1/search/observations", { params: { query } });
```

## 5. Translations

Write the locale as a TypeScript module checked against `Messages` (`src/i18n/messages.ts`):

```ts
import type { Messages } from "../i18n/messages";

export default {
  problems: { tag_name_taken: "Тег з назвою '{name}' уже існує", /* every problem code */ },
  field_errors: { username_reserved: "Це ім'я зарезервоване", /* every field error code */ },
  builtin_field_errors: { missing: "Обов'язкове поле", string_too_short: "Щонайменше {min_length} символів" },
  infrastructure: {
    http_error: "The server did not process the request (HTTP {status}). Request id: {request_id}",
    network_error: "No connection to the server",
    by_status: { 413: "The request is too large", 502: "The server is unavailable, try again later" },
  },
} satisfies Messages;
```

| Situation | Result of `npm run api:check` |
|-----------|-------------------------------|
| A problem or field error code without a translation | `TS2741: Property '<code>' is missing ...` |
| A translation of a code the API no longer has | `TS2353: Object literal may only specify known properties ...` |
| pydantic codes (`builtin_field_errors`) without a translation | allowed: the body's English `detail` is shown |

Placeholders `{param}` are the members of the problem body; the component schema of each code lists them
with their types, and `required` tells which are always present (an optional member can be absent, so do not
put it in the text). pydantic's `{expected_plural}` and `{error}` are never sent; write the plural in the
translation instead. A placeholder that is not a member stays visible as `{x}` and shows in your i18n
library's missing-interpolation warning.

## 6. In CI of the web project

```bash
npm ci
npm run api:all
git diff --exit-code src/api/schema.ts   # the committed types match the committed contract
```

## 7. How the backend keeps the contract from changing by accident

Every route declares the problems it answers on its decorator (`responses=problems(...)`), and the backend
tests fail on any problem the app answers that its operation does not declare. `api-contract/v1/openapi.json`
is committed and `tests/api/test_api_contract.py` fails while the code and the file differ, naming the changed
operations and schemas. An intended change is accepted by rewriting the file
(`python -m app.backend.cli.api_contract`), committed with the code, noted in `CHANGELOG.md` and announced to the
web team; the web project's `npm run api:all` then shows exactly what it has to adapt.

## 8. Development server: one origin through its proxy

The whole backend of a given release on your machine (API, worker, imgproxy, nginx, database in Docker), and the
checklist for publishing to production: WEB_DEV_AND_RELEASE.md. Its stack listens on `http://127.0.0.1:18089`;
use that as the proxy target below instead of a venv API on port 60800.

The API sends no CORS headers: in production the web client comes from the same nginx and origin as the API.
In development do the same with the dev server that `npm run dev` (or `npm start`) starts: let it proxy the API
paths, and call the API with relative URLs (`/api/v1/...`), never `http://localhost:8000/...`.

The API paths: `/api` (the documents included: `/api/v1/docs`), `/problems`, `/health`. Point the proxy at the API
started in development mode (`environment = "development"`: the login takes the certificate fields from a JSON
body, no client certificate needed).

Vite (`vite.config.ts`):

```ts
export default defineConfig({
  server: {
    proxy: Object.fromEntries(
      ["/api", "/problems", "/health"]
        .map((path) => [path, { target: "http://127.0.0.1:60800", changeOrigin: true }]),
    ),
  },
});
```

webpack-dev-server (`webpack.config.js`):

```js
module.exports = {
  devServer: {
    proxy: [{
      context: ["/api", "/problems", "/health"],
      target: "http://127.0.0.1:60800",
      changeOrigin: true,
    }],
  },
};
```

Other dev servers have the same option (Create React App: `"proxy"` in `package.json`; Angular: `proxy.conf.json`).
The port is `server.port` of the API configuration.

## 9. Seek previews in a player

`storyboardTrack(mediaId, token)` in `api/client.ts` fetches `GET /api/v1/media/{media_id}/storyboard` and returns
an object URL of the WebVTT, or `null` while it is not ready (`202`), failed (`409`) or unsupported (`422`). Any
other answer (`401`, `403`, `404`, `5xx`, the infrastructure) comes back as an `ApiFailure`, handled like the
failure of any other request. Use it when `storyboard_status` of the media is `ready` (read the media again while it
is `pending` or `processing`). Revoke the URL with `URL.revokeObjectURL` when the player goes away. The cues hold
absolute sheet URLs, so the track works from a blob URL.

```ts
const track = await storyboardTrack(media.id, token);
if (track !== null && typeof track === "object") {
  return showFailure(track); // an ApiFailure: track.kind is "problem" or "infrastructure"
}
const vtt = track; // the blob URL, or null: no previews

// Plyr
new Plyr(el, { previewThumbnails: { enabled: vtt !== null, src: vtt ?? "" } });

// video.js with videojs-vtt-thumbnails
player.vttThumbnails({ src: vtt });

// Vidstack
<media-player><media-provider /><media-video-layout thumbnails={vtt ?? undefined} /></media-player>
```

## 10. Video in a player

A `<video>` or `<audio>` element cannot send the Bearer token. The starter service worker does it for the stream
route: it sends the element's own `Range` requests to `GET /api/v1/media/{media_id}/stream` again with the token,
so the plain URL works as `src` and seeking works. It touches only the requests of media elements (never a page
load or an iframe of the URL, never `fetch()` calls), and when the token expired it asks only the page that plays
the media, once for every stream refused at that moment. Its tests: `node --test
api-contract/web-kit/tests/stream-sw.test.mjs`.

1. Copy `api-contract/web-kit/public/stream-sw.js` into the static root of the web project (Vite: `public/`); it
   must be served from the origin root, so its scope covers `/api/`.
2. Copy `src/api/streamWorker.ts`, call `registerStreamWorker` once at start-up with a function that returns the
   current access token (refreshed first when `expired` is true), and `sendStreamToken` after every login and
   refresh (`null` at logout).

```ts
await registerStreamWorker(async (expired) => (expired ? await refreshAccessToken() : currentAccessToken()));
// after login / refresh:
sendStreamToken(accessToken);

<video controls preload="metadata" src={`/api/v1/media/${media.id}/stream`} />
```

Refresh tokens work once: `refreshAccessToken` must be single-flight across the app's tabs as well (one refresh at a
time, the others wait for its tokens, e.g. with `navigator.locks.request`), or a second tab's refresh fails with
`invalid_refresh_token`. Never retry with a refresh token that refreshed already: after
`security.refresh_reuse_grace_seconds` (30) of that refresh, or with an older token, the API ends the whole session
(`refresh_token_reused`, API.md "Authentication").

On the first visit the worker controls the page only after a reload (`navigator.serviceWorker.controller` is
`null` until then); where workers are unavailable (`registerStreamWorker` returns `null`), fetch the media with
the token and play a blob URL instead, without seeking before it is loaded.

nginx keeps the API's permission for a stream 30 seconds (API.md, "Video and audio in a player"): access taken
away stops a stream within about 30 s. `ETag` is the file's sha256 on `/stream` and `/download`, and `If-None-Match`
with it answers `304` on both (API.md, "Stored files").

## 11. Events

The stream of events (API.md, "Real-time events") with the starter `src/api/events.ts`: no dependency beyond the
generated types. It opens only on HTTP/2, reads with `fetch`, parses the blocks, keeps the position, skips repeats,
reconnects with its back-off and watchdog, and spreads refetches over `refetch_within_ms`. Its tests: `node --test
api-contract/web-kit/tests/events.test.mjs` (Node 22.18 or later runs the `.ts` file directly).

1. Copy `api-contract/web-kit/src/api/events.ts` next to `schema.ts`.
2. Start it once per tab after login, stop it at logout:

```ts
import { BACKGROUND, startEvents, type StreamMessage } from "./api/events";

const events = startEvents({
  url: "/api/v1/events/stream",
  token: () => currentAccessToken(),
  refresh: () => refreshAccessToken({ headers: BACKGROUND }).then(() => true, () => false),   // single-flight, section 10
  onEvent: (event, refetch) => {
    switch (event.type) {
      case "move.changed": refetch(() => reloadMove(event.data.move_id, { headers: BACKGROUND })); break;
      case "scope.changed": refetch(() => reloadTreeAndMe({ headers: BACKGROUND })); break;
      case "security.certificate_mismatch": notifySecurity(event.data.audit_log_id); break;     // audit.view holders
      // the administration (API.md, "The administration"): refetch only what is on screen
      case "user.changed": refetch(() => reloadUser(event.data.subject_user_id, { headers: BACKGROUND })); break;
      case "certificate.changed": refetch(() => reloadCertificates(event.data.subject_user_id, { headers: BACKGROUND })); break;
      case "session.changed": refetch(() => reloadSessions(event.data.subject_user_id, { headers: BACKGROUND })); break;
      case "role.changed": refetch(() => reloadRole(event.data.role_id, { headers: BACKGROUND })); break;
      case "dictionary.changed": refetch(() => reloadDictionary(event.data.dictionary, { headers: BACKGROUND })); break;
      // the work screens (API.md, "Observations and media"): an id the list does not show is new
      case "observation.changed": refetch(() => reloadObservation(event.data.observation_id, { headers: BACKGROUND })); break;
      case "media.changed": refetch(() => reloadMedia(event.data.media_id, event.data.observation_id, { headers: BACKGROUND })); break;   // observation_id may be absent
      default: { const unhandled: never = event; return unhandled; }      // a new event type fails here
    }
  },
  onReload: (refetch) => refetch(() => reloadWhatIsOnScreen({ headers: BACKGROUND })),
  onRightsChanged: (refetch) => refetch(() => reloadMe({ headers: BACKGROUND })),        // GET /auth/me: menu, buttons
  onVisible: (run) => document.addEventListener("visibilitychange", () => { if (!document.hidden) run(); }),
});
// logout: events.stop()
```

The type of every message is `components["schemas"]["StreamMessage"]`; after `npm run api:all` a new event type in
the contract is a compile error in that `switch` until it is handled. Send `X-Activity: background` with every request
an event causes, and with pollers: those requests do not keep the session alive.

Development server: the paths are proxied already (section 8), but a dev server that compresses responses holds
blocks back (webpack-dev-server `compress: true`, some proxy middlewares): turn compression off for `/api` and check
in the browser's network panel that a block arrives as it is written (a heartbeat within 20 s).
