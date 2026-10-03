# Web tooling: typed API client, contract checks, error translations

How the web project takes the API contract from this backend and checks itself against it with the stock
tools only: openapi-typescript 7, TypeScript 5 (strict) and openapi-fetch 0.17. There is no extra catalog and
no custom checker: everything comes from the OpenAPI document FastAPI generates. Every command below was run
on a clean project. Background on the error format: `API.md`.

## 1. The contract: one file

`api-contract/openapi.json` in this repository is the OpenAPI document of the API, committed with every
change of the API (the backend tests fail until it matches the code). A running API serves the same
document at `/openapi.json`.

```bash
# in the web project: take the contract of the backend version you build against
curl -o api-contract/openapi.json https://<api-origin>/openapi.json
# or copy api-contract/openapi.json from this repository
```

Commit it in the web project too: a diff of `openapi.json` in a pull request is the change of the API it
adopts. To rewrite it in this repository without a server: `python -m app.backend.api_contract`.

## 2. Set up the web project once

```bash
npm install openapi-fetch@0.17
npm install -D openapi-typescript@7 typescript@5
```

Copy from `api-contract/web-kit/` of this repository:

| File | What it does |
|------|--------------|
| `src/api/client.ts` | openapi-fetch client; the `Problem`, `ProblemCode`, `FieldErrorCode` types derived from the generated schema; `problemText()` and `fieldMessages()` (translation with English fallback, form field from `pointer`) |
| `src/api/contract-check.ts` | Compile-time checks of the contract (section 4); not imported by the app |
| `src/i18n/messages.ts` | The `Messages` type a locale is checked against (section 5) |
| `tsconfig.json` | `strict` and `noImplicitReturns` are required by the checks; merge into your own tsconfig |

`package.json` scripts:

```json
{
  "scripts": {
    "api:types": "openapi-typescript ./api-contract/openapi.json -o ./src/api/schema.ts",
    "api:check": "tsc --noEmit -p tsconfig.json",
    "api:all": "npm run api:types && npm run api:check"
  }
}
```

## 3. Every time the API changes

```bash
# 1. take the new api-contract/openapi.json (section 1)
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

## 5. Translations

Write the locale as a TypeScript module checked against `Messages` (`src/i18n/messages.ts`):

```ts
import type { Messages } from "../i18n/messages";

export default {
  problems: { tag_name_taken: "Тег з назвою '{name}' уже існує", /* every problem code */ },
  field_errors: { username_reserved: "Це ім'я зарезервоване", /* every field error code */ },
  builtin_field_errors: { missing: "Обов'язкове поле", string_too_short: "Щонайменше {min_length} символів" },
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
tests fail on any problem the app answers that its operation does not declare. `api-contract/openapi.json`
is committed and `tests/test_api_contract.py` fails while the code and the file differ, naming the changed
operations and schemas. An intended change is accepted by rewriting the file
(`python -m app.backend.api_contract`), committed with the code, noted in `CHANGELOG.md` and announced to the
web team; the web project's `npm run api:all` then shows exactly what it has to adapt.

## 8. Development server: one origin through its proxy

The API sends no CORS headers: in production the web client comes from the same nginx and origin as the API.
In development do the same with the dev server that `npm run dev` (or `npm start`) starts: let it proxy the API
paths, and call the API with relative URLs (`/api/v1/...`), never `http://localhost:8000/...`.

The API paths: `/api`, `/docs`, `/redoc`, `/openapi.json`, `/problems`, `/health`. Point the proxy at the API
started in development mode (`environment = "development"`: the login takes the certificate fields from a JSON
body, no client certificate needed).

Vite (`vite.config.ts`):

```ts
export default defineConfig({
  server: {
    proxy: Object.fromEntries(
      ["/api", "/docs", "/redoc", "/openapi.json", "/problems", "/health"]
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
      context: ["/api", "/docs", "/redoc", "/openapi.json", "/problems", "/health"],
      target: "http://127.0.0.1:60800",
      changeOrigin: true,
    }],
  },
};
```

Other dev servers have the same option (Create React App: `"proxy"` in `package.json`; Angular: `proxy.conf.json`).
The port is `server.port` of the API configuration.
