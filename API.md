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

### Lists and filters

- Filters are query parameters, combined with AND. A parameter the list does not know answers 422 (item code
  `extra_forbidden`, `parameter` = its name), so a misspelt filter never widens the result unnoticed. Statuses,
  confidence levels and media types accept only the values of their enum in the OpenAPI document (item code `enum`).
- Send a parameter that takes one value once: a repeated one keeps its last value. Lists (`status_in`, `tag_ids`)
  are repeated: `?status_in=active&status_in=confirmed`.
- Exact text filters (`name`, `code`, `username`, ...) and `*_contains` ignore case (the database collation):
  `name=tag_b` finds `Tag_B`. `*_contains` matches `%`, `_` and `\` literally.
- `last_n_days` (observations) is a period of its own: together with `observed_at_from` / `observed_at_to` it answers
  422 `date_filters_conflict`.

### Authentication (401)

Send `Authorization: Bearer <access token>` from `POST /api/v1/auth/login`.

- `invalid_token` (the access token expired): refresh with `POST /api/v1/auth/refresh`, then repeat the request.
- `session_expired`: the session itself ended (logout, no request for `session_idle_timeout_minutes`, the optional hard limit `session_max_hours`, or revoked by the server, for example because its certificate was revoked: `revoke_cause` `certificate_revoked`); a refresh cannot bring it back: log in again. With a client certificate the login needs no user input, so the client can do it by itself.
- `token_certificate_mismatch`: the token belongs to a session opened with another client certificate (the browser presented a different one, or the token was copied), or the certificate of the session stopped being an active certificate of the user without the session being ended for it (it expired; the session ends now); log in again with a current certificate.
- `credentials_required`, `account_not_found`, `account_deactivated`, `account_deleted`, `invalid_refresh_token`: log in again.
- At login: `certificate_revoked` (the certificate is registered but revoked: the person needs a new one), `certificate_not_recognized` (no user has this certificate, or it expired).

Session lifetime: a session lives while it is used. It ends after `security.session_idle_timeout_minutes` (30) without a request; every request and every refresh moves that end (`expires_at` of the session). `security.session_max_hours` can add a hard limit since the login (0, the default: none). Every refresh issues a new access and refresh token; a refresh token stays usable for the access token lifetime plus the idle timeout, so a client that refreshes when its access token expires never loses an active session. A refresh token works once: the refresh that uses it retires it, and the same token again answers `invalid_refresh_token` (the session goes on with the newest token). Keep only the latest refresh token and never refresh twice in parallel with the same one (two tabs: share the refresh through one place); a client that lost the answer of a refresh logs in again.

A session opened through nginx with a client certificate is bound to it: every request and refresh must present the same certificate, so a copied token is useless elsewhere (`token_certificate_mismatch`). `GET /auth/sessions` lists the caller's live sessions with `status` (`online` / `idle`); holders of `user.view` covering a user see that user's sessions, ended ones with `end_reason` / `revoke_cause`, under `/users/{id}/sessions`. Only the user themself sees the device of a session in clear: for any other caller `ip_address` is masked to its network (CIDR, IPv4 `/16` such as `192.168.0.0/16`, IPv6 `/48`; null when the stored value is no address), `user_agent` is null and `device_masked` is `true`; `id`, `status` and the times stay, so an administrator can still end the session. `GET /auth/sessions` is always in clear.

### Not allowed (403)

The body adds `required` (`{all, any}`) and `missing` (permissions the user lacks). `GET /api/v1/auth/route-access` tells in advance which routes the user may call.

| Code | When |
|------|------|
| `permission_denied` | The route's permissions are held nowhere (no assignment gives them; an assignment on an archived post or at an archived group gives nothing) |
| `post_access_denied` / `post_write_denied` | The permission is held, but not in effect at this post (`post_id`; `*.view` / any other permission) |
| `no_post_assigned` | The permission is held, but in effect at no post at all (for example only at a scope group without posts) |
| `permission_required_for_action` | A permission a part or a target of the request needs (`permission`, `action`): `include_deleted`, the group a post is created in or moved to and the one it leaves (`post.create` / `post.update`, `post.view`), the old and the new parent of a moved scope group or brigade (`post.update`, `post.view`), the parent of a group created, renamed, moved, archived or restored (`scope_group.create` / `.update` / `.archive`), a corps or brigade archived with or without cascade (`post.archive` / `post.update`, `post.view`), the home post of a new or moved user, a media file without an observation (`media.update` at the root) |
| `user_self_only` | A user record outside the caller's `user.view` |
| `target_user_more_privileged` | A change to a user the caller's `user.*` does not cover, or who holds an assignment the caller could not grant |
| `grant_exceeds_own_permissions` | Granting or revoking permissions the caller does not hold (at the node, for an assignment; at every node the role is assigned at, for permissions added to a role and the restore of a role) |

### Server errors (500 `internal_error`)

The body carries `error_id`, the request id of the response (`X-Request-ID`); the server log has it once, with the details. Show it to the user so a report can be matched.

## Authorization: pages, buttons, scope

Access is role assignments: a user holds a role at one node of the scope tree, the root (`/`, the whole system), a
scope group (a group of posts, groups nest) or a post, and it applies to that node and everything below it.
Assignments add up. Every route checks its own permissions where it acts; there is no administrator flag.
A write needs the view permission of the record next to the write permission, both in effect where it acts. Posts:
update, archive and restore need `post.view` and `post.update` / `post.archive` at the post (a post the caller cannot
see answers 403 `post_access_denied`, as `GET` does, before the write permission); a create needs `post.create` and
`post.view` covering the group (the root for a post directly under it), a move `post.update` and `post.view`
covering the old and the new location (403 `permission_required_for_action`, `create_post` / `move_post`).

The login, the refresh and `GET /api/v1/auth/me` carry what the interface is built from:

| Field | Use it for |
|-------|------------|
| `user_type` | Which pages the web shows: a position (`viewer`, `operator`, `senior_operator`, `analyst`, `administrator`, `system_administrator`), never a place. The catalog is `GET /api/v1/user-types/` (`key`, `display_name`, `description`, `base_roles` with `kind` and `at_root`, `tree_boundary`, `system_boundary`: the most a user of the type can ever have in effect). The API never authorizes by type: a page still checks its buttons. |
| `permissions` | Permissions held at some node or through a system role (an assignment on an archived post or at an archived group counts nowhere): the menu (an entry whose route the user can call somewhere). |
| `root_permissions` | Tree permissions held at the root: in effect at every post. |
| `system_permissions` | Permissions of the system roles: administration of the whole system (roles, dictionaries, formats, audit, monitoring, backups, certificate export). |
| `post_permissions` | `{"7": ["observation.create", ...]}`: per post (key: the post id as a string), the permissions that assignments at groups and at the post put in effect there, for every post they reach. |
| `group_permissions` | `{"3": ["scope_group.update", ...]}`: per group (key: the group id as a string), the permissions that assignments at groups put in effect there (the group of the assignment and every group below it, archived ones too), for every group they reach; assignments at the root and at posts are not in it, an assignment at an archived group reaches nothing. In effect at a group = `root_permissions` plus its entry. |
| `assignments` | The user's tree assignments, as `GET /users/{user_id}/assignments` lists them. |
| `system_assignments` | The user's system assignments, as `GET /users/{user_id}/system-assignments` lists them. |
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
- Roles have a `kind` (`RoleResponse.kind`): `tree` roles hold tree permissions and are assigned at a node through the
  routes above; `system` roles hold system permissions and are assigned without a node:
  `GET /api/v1/users/{user_id}/system-assignments` (`[{id, role, created_at}]`),
  `POST /api/v1/users/{user_id}/system-assignments` (201) with `{"role_id": 5}`,
  `DELETE /api/v1/users/{user_id}/system-assignments/{assignment_id}` (204). Each route refuses the other kind (409
  `role_is_system` / `role_is_tree`). `POST /api/v1/roles/` takes `kind` (required, never changed); a role holds
  permissions of its kind's level only (409 `permission_level_mismatch`). `GET /api/v1/roles/?kind=system` lists one
  kind.
- The caller may assign only a role whose permissions they hold at that node, the root for a system role (and
  `user.update` there); a role must fit the boundary of the user's type for its kind (`tree_boundary`,
  `system_boundary`). Offer only roles that pass these checks. A grant outside the boundary never acts, even if some
  other path wrote it.
- A new user (`POST /api/v1/users/`, form fields `user_type` and an optional `post_id`) gets the base roles of the
  type: the tree base role at the home post (at the root without one, or for `system_administrator`), and the system
  base roles; add more with the assignment routes. `PATCH /api/v1/users/{user_id}` with `user_type` changes the type
  and replaces the old base roles with the new ones (tree ones at the same nodes, or at the root for an `at_root`
  base role; system ones added and removed).
- Built-in roles (`is_builtin`, `builtin_key`): tree `viewer`, `operator`, `senior_operator`, `analyst`,
  `administrator` (every tree permission: at a post, the administrator of the post; at a brigade, of the brigade; at
  the root, of all data), system `system_administrator`, `auditor`, `dictionary_editor`, `tag_editor`. They are
  read-only; `POST /api/v1/roles/{role_id}/clone` makes an editable copy (of the same kind).

### Problem codes of authorization and scope

| Code | Status | When |
|------|--------|------|
| `assignment_not_found` | 404 | The assignment is not one of this user's |
| `assignment_exists` | 409 | The user already holds this role at this node |
| `last_assignment_required` | 409 | The user's last assignment (of a live role) cannot be removed; also the permanent delete of a role that is the only assignment of an archived holder |
| `permission_level_mismatch` | 409 | A role gets a permission of the other level (`role_kind`, `permissions`) |
| `role_is_system`, `role_is_tree` | 409 | A system role on the tree assignment routes, a tree role on the system ones (`role_id`) |
| `role_exceeds_user_type` | 409 | The role holds permissions the boundary of the user's type does not allow (`user_type`, `excess_permissions`) |
| `user_type_ceiling_exceeded` | 409 | A type change while other assignments exceed the new type (`user_type`, `assignments`: `kind` (`tree` or `system`: the table of the `id`), `role`, `scope`, `excess_permissions`; each assignment against the boundary of its kind) |
| `role_holders_exceed_user_type` | 409 | Permissions added to a role that the type of a holder does not allow (`users`) |
| `builtin_role_not_renamable`, `builtin_role_permissions_fixed`, `builtin_role_not_archivable`, `builtin_role_not_deletable` | 409 | Built-in roles cannot change (clone one) |
| `scope_group_not_found` | 404 | Unknown, archived or outside the caller's `scope_group.view` |
| `scope_group_not_archived` | 409 | Restore of a group that is not archived |
| `scope_group_not_empty` | 409 | Archive of a group that still has live groups or posts |
| `scope_group_cycle` | 409 | A group moved into its own subtree |
| `scope_group_depth_exceeded` | 409 | Groups nested deeper than `max_depth` (6) |
| `scope_group_name_taken` | 409 | The name is used under the same parent |
| `scope_group_parent_not_allowed` | 409 | A group of this kind cannot sit there (`group_kind`, `parent_kind`; `root` for none) |
| `corps_has_brigades` | 409 | Archive without cascade of a corps that still has active brigades |
| `last_admin_required`, `cannot_remove_own_admin` | 409 | The built-in `system_administrator` system role must stay with an active user and cannot be taken from oneself |

Field codes inside `validation_error`: `scope_id_required` (a group or post scope without `id`),
`scope_id_not_allowed` (the root with an `id`).

### Corps and brigades

Corps > brigade > post. A brigade is optional: a post sits in a brigade, directly in a corps, or outside any corps
(in a plain scope group or under the root). Corps and brigades are scope groups with a `kind` (`corps`, `brigade`;
plain groups are `group`): a role assigned at a corps or brigade (`{"kind": "group", "id": ...}`) covers everything
below it, and `GET /scope-groups/tree` shows them with their `kind`. No new permissions: `scope_group.*` for the
units, `post.*` for what happens to their posts.

Who manages what: `scope_group.create`, `.update` and `.archive` are tree permissions (assignable at the root, a corps, a
brigade or a plain group; the built-in `administrator` role holds them, the operator types may not; a holder at the root manages
every corps, brigade and plain group). A write on a group needs
its permission covering the group's parent, the root for a corps or a root plain group:

| Holder of `scope_group.*` at | Manages |
|---|---|
| the root | every corps, brigade and plain group (as before) |
| a corps | the brigades of that corps: create, rename, archive, restore, move between corps it covers; not the corps itself |
| a brigade | nothing of the structure (a brigade holds no groups); not the brigade itself |
| a plain group | the plain groups inside it; not the group itself |

A move (`PATCH` with a new `corps_id` / `parent_id`) needs `scope_group.view` and `scope_group.update` covering the old
and the new parent (the current `corps_id` / `parent_id` sent back is no move). A rename without a move needs `scope_group.view` at the
parent too (the root for a corps or a root group), checked before the name: 403, never 409. Creating at the root (a corps, a root plain group) or
moving a group to the root also needs `scope_group.view` at the root (X.view AND X.create / X.update where it acts),
checked before the name: a holder of `scope_group.create` at the root who cannot see it gets 403, never 409.
A refusal is 403 `permission_required_for_action` with `action` `create_corps`, `update_corps`, `archive_corps`,
`restore_corps` (the same with `brigade` and `scope_group`), `move_brigade` or `move_scope_group`; a group outside the
caller's `scope_group.view` answers 404 first. Show the buttons of a group when the permission is in effect at its
parent: for a corps (parent: the root) or a plain group directly under the root that is `root_permissions` only (no parent entry), for a
brigade or a plain group inside another group `root_permissions` plus `group_permissions[<parent id>]` (the corps of a
brigade, the group a plain group sits in).

A move is an access change for the people at the destination: whoever holds a role at the new corps, brigade or group
gains it over the moved posts and their home users, and loses nothing it had at the old place except what it held
there; restoring a post or group makes the assignments made at it apply again. Who may move is decided by the
mover's own rights only.

- Where each kind sits: a corps directly under the root, a brigade directly in a corps, a plain group under the root
  or another plain group. The kind is fixed at create (`POST /corps/`, `POST /brigades/`; `POST /scope-groups/`
  makes plain groups). Anything else answers 409 `scope_group_parent_not_allowed` on every route.
- `GET/POST /api/v1/corps/`, `GET/PATCH /api/v1/corps/{corps_id}`, `GET /corps/{corps_id}/brigades`,
  `GET /corps/{corps_id}/posts` (posts at any depth); the same for `/api/v1/brigades/` with `corps_id` in the body
  and as a list filter, and `GET /brigades/{brigade_id}/posts`. Answers: `CorpsResponse` (`brigade_count`,
  `post_count`: active ones the caller sees), `BrigadeResponse` (`corps_id`, `post_count`). An id of another kind,
  an archived or invisible one: 404 `scope_group_not_found`. Names are unique within a parent (corps share the root
  with root plain groups). Names compare by the database collation: case, accents and trailing spaces do not make a
  different name (409 `scope_group_name_taken`).
- Moving a brigade (`PATCH /brigades/{id}` with `corps_id`) takes its posts along and needs `scope_group.update`,
  `post.update` and `post.view` covering the old and the new corps.
- `POST /corps/{id}/archive` and `POST /brigades/{id}/archive` take `?cascade=true|false` (default false):

  | | cascade=false | cascade=true |
  |---|---|---|
  | brigade | its active posts move into its corps (`post.update` and `post.view` at the brigade and the corps) | the brigade and its active posts are archived (`post.archive` and `post.view` at the brigade) |
  | corps | 409 `corps_has_brigades` while it has an active brigade; its active posts leave it, directly under the root (`post.update` and `post.view` at the corps and the root) | the corps, its active brigades and every active post below are archived (`post.archive` and `post.view` at the corps) |

  `scope_group.archive` must cover the parent of the node too (the root for a corps, the corps for a brigade; also
  for restore), and `post.view` wherever posts change (the unit, and the destination of a detach). The post
  permissions are asked only when an active post would change: none for an empty unit (cascade or not), and a corps
  detach counts only the posts directly in the corps (one with an active brigade answers 409 `corps_has_brigades`
  without asking them). A missing permission answers 403 `permission_required_for_action` and nothing changes. Moving posts out changes access: holders of a role at the archived unit lose those posts.
  Restore (`POST /{id}/restore`) brings back the node only; list the archived brigades and posts below with
  `include_deleted=true` and restore them one by one. A brigade whose corps is archived: restore the corps first
  (404 `scope_group_not_found`).
- `PostResponse.corps` and `.brigade` (`{id, name}` or null) are where the post sits now. Posts (lists and search)
  filter by `corps_id` / `brigade_id`; `corps_id` includes the posts of its brigades; an id of another kind gives an
  empty list. A unit filter narrows what the caller sees, never widens it.
- User records carry `home` (the home post's name and code), `post`, `brigade` and `corps`. `post_id`, `brigade_id`
  and `corps_id` on `GET /users/` and `/search/users` match users whose home is the node or below it, or who hold an
  assignment (a tree role, not archived, on a node not archived) at the node or below it; `membership` selects
  `any` (default), `home` or `assigned`. `post_id` therefore no longer means the home post only: for the home post
  alone (what `PostResponse.user_count` counts) pass `membership=home`. The base role of a user sits at its home post
  and stays there when the user is moved, so after a move `post_id=<old post>` still lists the user by default
  (assigned), and not with `membership=home`; in production `assigned` usually includes the home users too.
- `ObservationWithMedia.corps` and `.brigade` are where the post was when the observation was created, recorded at
  create and never changed (a later move of the post or the brigade does not change them); the names are today's.
  An observation created while its post moves may record the place from just before the move.
  An observation that had no saved unit at all (created before its post was put into a unit) has the units the post
  was in when the operator ran `cli.units fill-history` (DEPLOYMENT.md): that run is part of the recorded history,
  and `placement=current` is not affected by it. The command may be run again after later placements; it adds rows
  only to observations that still have none, and existing rows never change.
- Unit names on posts and observations are shown to anyone who sees the post or observation, without
  `scope_group.view`; user records show the home post's name and code and its units the same way.
- Observations and media (lists and search) filter by `corps_id` / `brigade_id` with `placement`: `recorded` (the
  default: where the post was when the observation was created) or `current` (where the post is now; posts archived
  since are left out). `placement` only chooses which unit an observation belongs to; both read today's data, and the
  period is always its own filter (`observed_at_*`, `created_at_*`). Example: a post moved from brigade 10 to
  brigade 20 in June; for March, `brigade_id=10` finds its March observations with `recorded` and not with
  `current`. Access still follows today's tree: a caller with a role at brigade 10 only does not see that post any
  more, whatever the placement.
- `POST /observations/report`: `corps_ids`, `brigade_ids`, `placement`; the `all_fields` report has the columns
  "Корпус" and "Бригада" (as the placement says; a unit archived today is marked " (архівовано)").
- `GET /corps/{id}/observation-stats`, `GET /brigades/{id}/observation-stats` (`observation.view`): the counters of
  `GET /posts/{id}/observation-stats` over the unit (`placement`, the same period parameters), counting the posts
  where `observation.view` is in effect. The unit must lie where the caller holds `scope_group.view` or
  `observation.view`, or have a post under it now where `observation.view` is in effect (so the commander of a brigade
  counts its corps, and the viewer of one post its brigade, each only for the posts they see); otherwise 404
  `scope_group_not_found`, like an unknown id. An archived unit keeps answering its history.
- The stats routes (corps, brigade, post and `GET /statistics`) answer 422 `stats_period_out_of_range` for a period whose
  buckets or default start (`date_to` minus 30 days) would leave the calendar (years 1 to 9999, e.g.
  `date_to=9999-12-31T23:00:00Z`); a future period of ordinary dates is fine. Any
  datetime parameter or body time whose UTC instant leaves the calendar (`0001-01-01T00:00:00+01:00`) is a field error
  `time_out_of_range` (params `earliest`, `latest`), as for `observed_at` out of its bounds.

## Client certificates

People log in with a client certificate the server issues (server side: `PKI.md`). A user may hold several, each
listed and revoked on its own, so a renewal can overlap the old one.

| Operation | Permissions | Purpose |
|-----------|-------------|---------|
| `GET /api/v1/users/{user_id}/certificates` | `certificate.view` | The user's certificates, oldest first; filter `state` |
| `POST /api/v1/users/{user_id}/certificates` | `certificate.view` + `certificate.create` | Issue: `{label, validity_days?}`, 201 with the package |
| `POST /api/v1/users/{user_id}/certificates/upload` | `certificate.view` + `certificate.create` | Register a certificate file (multipart `file` + `label`), 201 with the record |
| `GET /api/v1/users/{user_id}/certificates/{certificate_id}` | `certificate.view` | One certificate |
| `POST /api/v1/users/{user_id}/certificates/{certificate_id}/revoke` | `certificate.view` + `certificate.revoke` | Revoke for good, body `{reason}` optional |
| `POST /api/v1/users/{user_id}/certificates/{certificate_id}/export` | `certificate.view` + `certificate.export` | The stored package again (a system permission: only holders at the root); counted and audited |
| `GET /api/v1/certificates/` | `certificate.view` | Across users (renewals): filters `state`, `source`, `user_id`, `post_id`, `expires_within_days`; sort `id`, `label`, `not_after`, `created_at`; `page`, `page_size` |
| `GET /api/v1/certificates/authority` | any logged-in user | The client CA (`subject`, `not_after`, `fingerprint_sha256`, `certificate_pem`) and the published CRL (`crl.this_update`, `next_update`, `revoked_count`). The PEM is for showing or checking the CA: never offer it for installing, only nginx trusts this CA and the login does not need it on the client |

The routes of one user check, in this order: the user exists and is visible as `GET /users/{user_id}` shows it (404
`user_not_found`, 403 `user_self_only`), `certificate.view` covers the user (403 `permission_denied`: a user does not
see their own certificates here without it), a change also needs its permission covering the user and every
assignment of the user grantable by the caller (403 `target_user_more_privileged`), then the certificate (404
`certificate_not_found`, also for another user's id) and its state. `GET /certificates/` lists users whose home post
is where both `certificate.view` and `user.view` are in effect.

**A record** (`CertificateResponse`): `label` (the readable name: the CN and the name browsers show in the
certificate picker), `source` (`issued` here, `uploaded` file, `legacy` from before the upgrade, `configured` by the
development seed; `legacy` and `configured` have only hashes, so `subject_cn`, `serial`, `not_before`, `not_after`
are null), `state` (`active`, `expired`, `revoked`; computed when read), `ca_issued` (signed by this server's CA:
listed on its CRL once revoked), `has_package` (issued here with a stored package: export can download it again while
active), `revoked_at`, `revoked_by`, `revocation_reason`, `export_count`, `last_exported_at`, `created_at`,
`created_by`.

**Issuing and the `.p12`.** The label is 1 to 64 characters, no control characters; suggest one from the person's
name, post and device (`Petrenko I. - post 2 - laptop`). `validity_days` defaults to the server's 365 and is at most
its `max_validity_days` (730). The answer (201, `Cache-Control: no-store`) is `{certificate, p12_base64, password,
filename}`: the key and the certificate under the label, protected by a password of four groups of five characters.
Save the file and show the password once; never keep either in storage, state that outlives the dialog, or logs:

```ts
const { data, error } = await api.POST("/api/v1/users/{user_id}/certificates", {
  params: { path: { user_id: 7 } },
  body: { label: "Petrenko I. - post 2 - laptop" },
});
if (data) {
  const bytes = Uint8Array.from(atob(data.p12_base64), (c) => c.charCodeAt(0));
  const url = URL.createObjectURL(new Blob([bytes], { type: "application/pkcs12" }));
  Object.assign(document.createElement("a"), { href: url, download: data.filename }).click();
  URL.revokeObjectURL(url);
  showPasswordOnce(data.password);   // the person installs the file with it (PKI.md, section 5)
}
```

Export answers the same JSON from the stored package (same file, same password). Upload registers a certificate
made elsewhere (PEM or DER); an already expired file is accepted and listed as `expired`.

**Revoking** is final: a certificate is never archived, restored or deleted. `reason` is `unspecified` (default),
`key_compromise`, `affiliation_changed`, `superseded` (replaced by a new one) or `cessation_of_operation`. The
sessions opened with it end at once: that device's next request answers 401 `session_expired` (`revoke_cause`
`certificate_revoked` in the session lists), the user's other devices stay logged in. A certificate this server
issued then goes on the CRL, and nginx refuses it within seconds (an infrastructure 400, not a problem). The answer
is 200 once the revocation is stored, even when that CRL cannot be published at once (the server publishes it
later). Revoking a user's last certificate leaves the account in place: they log in again once a new one is issued.

**`GET /auth/me`** carries `certificate`: `id`, `label`, `not_after`, `state` of the certificate the session was
opened with (null for a session without one, the development login). Warn the person when `not_after` is near.

**Users.** `POST /api/v1/users/` takes the certificate file as optional, with an optional `certificate_label`
(default: its CN cut to 64 characters); without one the user exists but cannot log in until a certificate is issued
or uploaded. `PATCH /api/v1/users/{user_id}` takes no certificate: replace one by issuing or uploading a new one and
revoking the old one.

| Code | Status | When |
|------|--------|------|
| `certificate_not_found` | 404 | No such certificate for that user |
| `certificate_authority_not_configured` | 503 | Issue, export or the authority on a server without PKI |
| `certificate_authority_unavailable` | 503 | The signing service did not answer: nothing was issued, try again later |
| `certificate_already_revoked` | 409 | Revoking twice |
| `certificate_not_active` | 409 | Export of a revoked or expired certificate |
| `certificate_not_exportable` | 409 | Export of a certificate this server did not issue (`has_package` false) |
| `certificate_files_missing` | 410 | Export after the package was removed on the server |
| `certificate_taken` | 400 | Upload or `POST /users/` of a certificate already registered for any user |
| `certificate_invalid`, `certificate_file_empty` | 400 | Upload or `POST /users/` of an unusable file |
| `certificate_file_too_large` | 413 | Upload or `POST /users/` of a file above `max_bytes` |
| `certificate_revoked` | 401 | Login with a revoked certificate |

Field codes inside `validation_error`: `certificate_label_invalid` (the label), `validity_days_too_long` (param
`max_days`).

## Database and backups (status only)

A page for administrators. Backups are made, checked and restored on the server (`BACKUPS.md`); the API only
reports. Both permissions are system permissions (held through a system role, `system_administrator` among the
built-in ones); only the `system_administrator` user type may hold them.

| Operation | Permissions | Purpose |
|-----------|-------------|---------|
| `GET /api/v1/database/status` | `database.view` | Server (`product`, `version`, uptime, connections) and schema (`migration_revision` against `code_revision`, `up_to_date`, size, tables largest first with estimated rows) |
| `GET /api/v1/database/backups` | `backup.view` | `summary` (newest good backup, `age_seconds`, `stale`, `last_run`, sizes, free space, off-host `copy`), `backups` newest first (`copied`: has a valid off-host copy, null without one), `previous_states` (what a restore kept for undo) |

- Show a warning when `summary.stale` is true (no good backup for `stale_after_hours`), when `last_run.outcome` is
  `failed` (`last_run.error_code`: `no_space`, `dump_failed`, `timeout`, `copy_failed`, `unsupported_objects`,
  `tools_missing`, `busy`, `error`), and when `summary.copy.configured` and the newest backup has `copied: false`.
- 503 `backups_not_configured`: the server makes no backups this API knows of (hide the page or say so). 503
  `backup_directory_unavailable`: the directory cannot be read (a share is not mounted); retry later.
- Values the database does not give (sqlite in development) are null; `rows_estimate` is InnoDB's estimate, not a
  count.

## Audit log

A page for administrators: `audit.view` (system) reads every entry and every field; `audit.view_scope` (tree, in
the built-in `administrator` role) reads the entries of the nodes where it is in effect, never a system-wide one.

| Operation | Permissions | Purpose |
|-----------|-------------|---------|
| `GET /api/v1/audit-logs/` | `audit.view` or `audit.view_scope` | Entries newest first; the next page with `before_id` = the id of the last entry |
| `GET /api/v1/audit-logs/{audit_id}` | `audit.view` or `audit.view_scope` | One entry; 404 `audit_entry_not_found`, 403 `audit_entry_access_denied` (outside the caller's scope) |
| `GET /api/v1/audit-logs/events` | `audit.view` or `audit.view_scope` | The event catalog: `code`, `category`, `switchable`, English `title` |

- `event` is the key of exactly one message in the localisation package, like a problem `code`: the message takes
  the entry's `params` (typed per event in the OpenAPI document, `<CamelCode>AuditParams`; openapi-typescript
  narrows `params` by `event`). An optional param is absent, never null. The web must ship a message for every
  code of `GET /audit-logs/events` before it shows the page.
- `legacy.unknown` is an entry the catalog cannot describe, every row written before the event catalog among them
  (all rows from before the deploy of migration 031): `params` `{action, resource_type, message?, stored_event?,
  details?}`. `message` is the old English sentence, `stored_event` the code the row holds (only when it is
  another one), `details` the old details object as stored (the facts the row kept, keys in English): show them
  as they are.
- Params whose values come from a fixed list are plain strings (a value may be dropped from the list later, the
  old entries keep it): localise the known values and show an unknown one as it is.

  | Param | Events | Values |
  |-------|--------|--------|
  | `cause` | `session.end` | `account_deactivated`, `account_archived`, `certificate_changed` (old rows only), `ended_by_admin`, `user_type_changed`, `certificate_revoked`, `database_restored` |
  | `reason` | `user_certificate.revoke` | `unspecified`, `key_compromise`, `affiliation_changed`, `superseded`, `cessation_of_operation` |
  | `role_kind` | `role.*` | `tree`, `system` |
  | `group_kind` | `scope_group.*` | `group`, `corps`, `brigade` |
  | `frequency_kind` | `observation_frequency.*` | `rc`, `video` |
  | `user_type` | `user.create` | `viewer`, `operator`, `senior_operator`, `analyst`, `administrator`, `system_administrator` |
  | `media_type` | `media.create`, `upload_session.*` | `image`, `video`, `audio`, `document` |
  | `via` | `user.create`, `user_certificate.issue` | `cli` (made on the server's command line) |
  | `via` | `media.create` | `multipart`, `nginx_offload`, `resumable` (how the file came) |

- Device fields: `ip_address`, `user_agent`, `session_id` and `request_url` identify a device and are null without
  `audit.view` (the `session_id` filter then matches nothing). The param `ended_session_id` of `session.end` is not
  one of them: it is the id of the session that was ended, which holders of `user.view` already see in the
  sessions of the user (`GET /users/{user_id}/sessions`), so every reader of the entry gets it.
- Filters: `event`, `category` (lists), `user_id`, `resource_type`, `resource_id`, `success`, `created_from` /
  `created_to`, `request_id`, `session_id`, `post_id`, `group_id`, `corps_id`, `brigade_id`, `before_id`
  (`page_size` at most 200). A scoped reader's `post_id` / `group_id` / `corps_id` / `brigade_id` outside its scope
  answers 403.

## User guides

The help section: every authenticated user reads `GET /api/v1/guides/` and `GET /api/v1/guides/{guide_id}`
(there is no `guide.view`); create, edit, archive and restore need the system permissions `guide.create`,
`guide.update`, `guide.archive` (`include_deleted` too).

- Who sees a guide: a guide without an `audience` is for everyone. A guide for a node (a corps, a brigade, a post)
  is for the users related to it: their home post, or a node where they hold a tree role, lies on one path with the
  node (inside it or above it). A guide for a brigade is seen at its posts and by the administrators of the
  brigade, of its corps and of the root, not at a sibling brigade; a guide for a post is not seen at the other posts
  of its brigade. System roles do not count, and a user without a home post and tree roles sees only guides
  without an audience. Managers (`guide.update`) see every guide. An archived audience node hides the guide from
  everyone but managers.
- A guide the caller does not see is simply absent: left out of the list, and 404 `guide_not_found` on
  `GET /guides/{guide_id}` and on every file route, the same answer as for a guide that does not exist. Show it as
  "not found", never as "access denied".
- List: `GET /api/v1/guides/` answers a JSON array of `GuideResponse` (no envelope), in the guides' order
  (`sort_order`, then `title`; `sort_by=title` the other way round), `page` / `page_size` (50, at most 200), `q`
  (title contains). Each guide carries its `files`, so the help page needs one call.
- Get: `GET /api/v1/guides/{guide_id}` answers one `GuideResponse` (404 `guide_not_found` as above).
- Download: `GET` the file's `download_url` (`/api/v1/guides/{guide_id}/files/{file_id}/download`) with the token,
  as any download: always `attachment` under `file_name`, `Content-Type` the file's format (text types with their
  charset), `Range` supported. The file's sha256 is `GuideFileResponse.sha256` in the guide listing: behind nginx
  (production) the download carries no `X-File-Hash` and its `ETag` is nginx's own, not the sha256 (only an API
  serving the file itself, without nginx, sends both as the sha256). A file gone or archived is 404
  `guide_file_not_found`.
- Render as text: a guide's text and the content of its text files are shown as text. A `.txt` or `.csv` file is
  shown as text (`textContent`, a `<pre>`) or offered as the download. A Markdown file (`text/markdown`) is rendered
  only through a Markdown renderer with raw HTML off (markdown-it `html: false`) whose output goes through an HTML
  sanitiser (DOMPurify) before it reaches the page, links limited to `http`, `https` and `mailto`; never pass it to
  `v-html` / `innerHTML` as it is. Every other format (PDF, office documents) is only offered as the download.

- A guide's `title` and `description` are untrusted text: render them as text, never as HTML (no `v-html`, no
  `innerHTML`, no markdown-to-HTML without a sanitiser). The API refuses script patterns (`text_script_pattern`)
  and control characters (`text_control_character`) but accepts any other natural text, SQL words included.
- `description` keeps its line breaks (show them with `white-space: pre-line`); an empty one is null.
- Files: `POST /api/v1/guides/{guide_id}/files` (multipart `file`, optional `language`, needs `guide.update`) adds
  one, of a format of media type `document`; refusals are the media upload ones (415 with `Accept-Post`, 413
  `media_file_too_large` with `max_file_size_bytes`), plus 415 `document_text_invalid` for a Markdown, text or CSV
  file that is not UTF-8. `language` is a short tag (`uk`, `en-US`, at most 8 characters) or empty. Each file of
  `GuideResponse.files` has a `download_url`: open it with the token as any download (`attachment`, the file's own
  `Content-Type`, sandboxed). Archived files are left out and answer 404 unless `include_deleted=true`
  (`guide.archive`). The stored `file_name` is the sanitised upload name (ASCII stem, see `sanitize_filename`).

## Media types

Every media type the API names is the IANA-registered one where IANA has one (RFC 6838: lower case, case-insensitive
when compared); where IANA has none, the de facto name (`audio/wav`, `video/webm`). `GET /media/formats` lists each
format's canonical `content_type` and its `aliases`.

- **Stored and served:** a file is stored under its format's canonical name, whatever name the client declared or the
  server's detector reported; `mime_type` in media responses, the base of `playback_type`, and the `Content-Type` of
  `/download` and `/stream` are that name. Files stored before migration 032 under an alias of a default format were
  rewritten to it (when the file's extension belongs to that format; any other row keeps its type).
- **Accepted on upload** (declared `Content-Type`, `content_type` of a resumable upload): the canonical name or an
  alias of the format (table below). `application/octet-stream` (or no `Content-Type`) means "detect from the
  bytes". Administrators may add formats and aliases (`/media-formats`); `application/octet-stream` and
  `binary/octet-stream` are never a format's type or alias (`422` item `content_type_opaque`). A malformed
  `Content-Type` is refused like an unknown one (`media_content_type_not_accepted`, `form_content_type_required`)
  without the `content_type` param: it is present only when the header parsed.
- **Filters:** `mime_type` of `GET /media` and the media search takes any of these names, in any case, with or
  without parameters, and finds the rows stored under that name and under its canonical one (`Video/X-Matroska`
  finds `video/matroska` and `video/x-matroska` rows).
- **Charset:** text types are served with `charset=utf-8` (`text/markdown` with `charset=UTF-8`, which RFC 7763
  requires); `text/vtt` (storyboards) carries no parameter: WebVTT is always UTF-8.
- **Names to use in the web client:** `application/pkcs12` for a `.p12` blob (not `application/x-pkcs12`),
  `video/matroska; codecs="..."` from `playback_type` for `canPlayType()`; resumable chunks are
  `application/offset+octet-stream` (the tus protocol's name, not an IANA registration; `UPLOADS.md`).

| Canonical | Also accepted |
|-----------|---------------|
| `image/jpeg` | `image/pjpeg` |
| `video/mp4` | `video/x-m4v` |
| `video/matroska` (RFC 9559) | `video/x-matroska` (the name before the RFC) |
| `audio/mpeg` | `audio/mp3` |
| `audio/wav` | `audio/x-wav`, `audio/wave`, `audio/vnd.wave` |
| `audio/flac` (RFC 9639) | `audio/x-flac` |
| `audio/ogg` | `audio/opus`, `application/ogg` |
| `audio/mp4` | `audio/x-m4a`, `audio/aac` |
| `application/rtf` | `text/rtf` |
| `text/markdown`, `text/csv` | `text/plain` (what a detector reports for them) |

## Responses that are not JSON

| Operation | Response |
|-----------|----------|
| `GET /api/v1/media/{media_id}/download`, `/stream` | The file in its stored type (the canonical name, see Media types); `206` for a single `Range`; an unsatisfiable one is `416` `range_not_satisfiable` with `Content-Range: bytes */size` |
| `GET /api/v1/media/{media_id}/thumbnail` | The image; `202` with `Retry-After` while a video poster is being extracted |
| `GET /api/v1/media/{media_id}/storyboard` | WebVTT (`text/vtt`) for seek previews; `202` with `Retry-After` and `X-Storyboard-Status` while the worker has not produced it |
| `POST /api/v1/observations/report` | An `.xlsx` file with one row per frequency band; at most `reports.max_rows` rows (20000 by default), a larger result is `422` `report_too_large` with `max_rows`: narrow the filters |
| `PATCH /api/v1/media/uploads/{upload_id}` | `204`, the result is in the `Upload-*` headers |

Resumable uploads step by step: `UPLOADS.md`.

### Video and audio in a player (`/stream`)

A player seeks with many `Range` requests. nginx asks the API about the first request of a token, client certificate
and media, keeps the API's yes for 30 seconds and serves the bytes itself until then; a refusal is always the API's
own problem (`401`, `403`, `404 media_not_found`, ...). What follows for the client:

- Access taken away (logout, a revoked session or certificate, a removed permission, archived media) stops a stream
  within about 30 seconds, not at once (nginx counts whole seconds; measured 30.4 to 31.4 s).
- `If-None-Match` on `/stream` answers `200` with the file, never `304`; `ETag` and `X-File-Hash` are the file hash.
  `/download` behind nginx carries no `X-File-Hash` and nginx's own `ETag` (not the file hash); only an API serving
  the file itself, without nginx, sends both as the sha256 there.
- An unsatisfiable `Range` on `/stream` or `/download` is answered by nginx (`416`, by status, like any
  infrastructure error); the `range_not_satisfiable` problem comes only from an API without nginx in front.
- A `<video>` or `<audio>` element cannot send the Bearer token: register the starter service worker of
  `api-contract/web-kit` (WEB_TOOLING.md, section 10) and use the plain stream URL as `src`.

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
