# Web tooling: typed API client, contract checks, error translations

How the web project gets the API contract from this backend and checks itself against it with
openapi-typescript 7, TypeScript 5 (strict) and openapi-fetch 0.17. Every command below was run on a
clean project. Background on the error format: `API.md`.

## 1. What the backend exports

```bash
# in this repository (no server needed; uses app/config.toml like the app)
python -m app.backend.api_contract export --out <folder>
```

| File | Content | Used for |
|------|---------|----------|
| `openapi.json` | The full OpenAPI document, the same as `GET /openapi.json` | Types of every path, body, response and error |
| `problems.json` | `problems` (every problem code: status, title, detail, params with type and whether always present), `field_errors` (codes of invalid values), `builtin_field_errors` (pydantic's codes and params) | Translations of the errors |

A running API serves the same `openapi.json`: `curl -o api-contract/openapi.json https://<api-origin>/openapi.json`.
`problems.json` comes only from the export command; `/problems` shows the same catalog as a page.

Put both files in the web project, e.g. `api-contract/`, and commit them there: a diff of `openapi.json`
in a pull request of the web project is the change of the API it adopts.

## 2. Set up the web project once

```bash
npm install openapi-fetch@0.17
npm install -D openapi-typescript@7 typescript@5
```

Copy from `api-contract/web-kit/` of this repository:

| File | What it does |
|------|--------------|
| `src/api/client.ts` | openapi-fetch client plus `problemText()` and `fieldMessages()` (translation with English fallback, form field from `pointer`) |
| `src/api/contract-check.ts` | Compile-time checks of the contract (section 4); not imported by the app |
| `scripts/check-i18n.mjs` | Translation check against `problems.json` (section 5) |
| `tsconfig.json` | `strict` and `noImplicitReturns` are required by the checks; merge into your own tsconfig |

`package.json` scripts:

```json
{
  "scripts": {
    "api:types": "openapi-typescript ./api-contract/openapi.json -o ./src/api/schema.ts",
    "api:check": "tsc --noEmit -p tsconfig.json",
    "i18n:check": "node scripts/check-i18n.mjs --catalog api-contract/problems.json --locale src/locales/uk.json",
    "api:all": "npm run api:types && npm run api:check && npm run i18n:check"
  }
}
```

## 3. Every time the API changes

```bash
# 1. take the new contract (export or curl, section 1) into api-contract/
# 2. then:
npm run api:all
```

- `api:types` regenerates `src/api/schema.ts` (commit it with the contract).
- `api:check` type-checks the whole app against it: every call site of a changed path, body, response or error fails here.
- `i18n:check` fails on a missing translation or a wrong placeholder.

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

Locale layout (placeholders `{param}` are the members of the error body):

```json
{
  "problems":            { "tag_name_taken": "Тег з назвою '{name}' уже існує" },
  "field_errors":        { "username_reserved": "Це ім'я зарезервоване" },
  "builtin_field_errors": { "missing": "Обов'язкове поле", "string_too_short": "Щонайменше {min_length} символів" }
}
```

```bash
# add every missing API code with its English text, then translate the new entries
node scripts/check-i18n.mjs --catalog api-contract/problems.json --locale src/locales/uk.json --fill
npm run i18n:check
```

| Situation | Result |
|-----------|--------|
| A problem or field error code without a translation | error, exit 1 |
| A placeholder that is not a param of its code (`{tag}` in `tag_name_taken`) | error, exit 1 |
| A translation of a code the API no longer has | warning |
| pydantic codes without a translation | one warning line (`--verbose` lists them; `--strict-builtin` makes them errors). The body's English `detail` is shown instead. |

Options: `--placeholder "\{\{(\w+)\}\}"` for i18next style `{{param}}`. Files saved with a BOM are fine.

Rules for the texts: a param marked `"required": false` in `problems.json` can be absent from the body,
so do not put it in the text; pydantic's `{expected_plural}` and `{error}` are never sent, write the plural
in the translation instead.

## 6. In CI of the web project

```bash
npm ci
npm run api:all
git diff --exit-code src/api/schema.ts   # the committed types match the committed contract
```

## 7. How the backend keeps the contract from changing by accident

The list of errors per operation is derived from the backend code. A refactoring that renames a
code, renames or retypes a param, or makes a code disappear from an operation would change the contract
silently, so the backend also locks it: `api-contract/problems.lock.json` lists every code, its status and
params, and the codes of every operation (by operationId), never Python names. Moving or renaming Python
code that keeps the same codes changes nothing.

Any difference fails the backend tests with a report such as:

```
BREAKING for the web client (localization keys, params or handled codes):
  - tag_name_taken: param removed or renamed: name
  - update_post: no longer answers 404 post_not_found (...)
Additions the web client must pick up:
  - problem code added: tag_name_reserved (needs a translation)
```

An intended change is accepted with `python -m app.backend.api_contract lock`, committed together with
the code, noted in `CHANGELOG.md` and announced to the web team; breaking lines need a web release
that adopts the new contract. `python -m app.backend.api_contract check` prints the same report.
