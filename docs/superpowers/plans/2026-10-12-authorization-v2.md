# Authorization v2 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the global `admin.true` gate and the home-post / access-row data scope with user types, Azure-style
role assignments on a scope tree (`/` > nested groups > posts) and code-owned built-in roles.

**Architecture:** Additive groundwork first (scope groups, permission levels, user types, built-in roles, a pure scope
evaluator and the frozen conversion function). Then a transitional AuthContext computes the new grants live from the
OLD tables through the conversion function, so every route can move to permission-at-post checks while the existing
test suite keeps proving behaviour is unchanged. Only then the data switches to `role_assignments` (migration 023 runs
the same conversion), followed by the assignment API, user-type behaviour and the auth fields of the web client.

**Tech Stack:** Python 3, FastAPI, SQLModel / SQLAlchemy async, Alembic, pydantic v2, pytest + httpx (in-memory
sqlite), MySQL 8 / MariaDB in production.

**Spec:** `docs/superpowers/specs/2026-10-11-authorization-v2-design.md` (read it before any task; section numbers
below refer to it).

## Global Constraints

- Branch `authorization-v2`. Every commit message ends with `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`.
- ASCII only in code, comments, docs and commit messages; no emoji. Ukrainian text only in user-facing display texts
  (`PERMISSION_META`, built-in role and type display names), as today.
- Read `CLAUDE.md` first. Every path is a member of its group in `app/backend/enums/routes.py` before it is used; no
  `"/api/v1/..."` or `"/"` literal anywhere, tests included (`V1.path(XRoutes.MEMBER, ...)`). Literal paths are
  registered before `/{id}` paths.
- Errors: `ApiError(ErrorCode.X, **params)`, `access_denied(AccessErrorCode.X, required=..., missing=..., **params)`,
  `invalid(FieldErrorCode.X)` in validators, `CodedError` in services; every raised code is declared in the route's
  `responses=problems(...)`. Param names at least 3 characters, never `type`, `title`, `status`, `detail`,
  `instance`, `code`.
- Pydantic v2 only (`field_validator`, `model_validator`); never `@validator`.
- Services, filters, models, workers, enums, `bootstrap/`, `utils/` never import `app.backend.api` or FastAPI.
- Migrations: every table / column / index guarded with inspector checks; new model tables listed in
  `TABLES_OWNED_BY_LATER_REVISIONS` (`002_sync_schema_with_models.py`); every revision has a working downgrade.
- Group depth limit: `MAX_GROUP_DEPTH = 6`. Built-in role keys = user type keys: `viewer`, `operator`,
  `senior_operator`, `analyst`, `post_admin`, `admin`.
- Tests: `venv\Scripts\python -m pytest -q` (whole suite) must pass before every commit; migration tests are
  `@pytest.mark.slow` and run in the whole suite.
- Every task that changes the API or the database adds its lines to `CHANGELOG.md` under a new heading
  `## Authorization v2` (Added / Changed / Removed) and, when the API changed, regenerates the contract on purpose:
  `venv\Scripts\python -m app.backend.cli.api_contract`, then `tests/test_api_contract.py` passes.
- The only hard deletes allowed: `DELETE /roles/{id}` (unchanged) and link rows (`role_assignments`,
  `role_permissions`), always audited.

## Review Focus

Inputs the spec implies that no task would otherwise test; each line has its test in the owning task.

1. Moving a group whose own subtree height plus the new parent's depth exceeds 6 -> 409 `scope_group_depth_exceeded`
   (Task 1, `test_move_subtree_respects_depth_limit`).
2. A delegated administrator assigned at group East assigning a role at `/`, at group West, or to a user whose home
   post is outside East -> 403 `grant_exceeds_own_permissions` / `target_user_more_privileged`, nothing written
   (Task 7, `test_group_admin_cannot_assign_outside_own_subtree`).
3. Editing a custom role (`PATCH /roles/{id}`) so it holds a permission above the ceiling of a user who holds it ->
   409 `role_holders_exceed_user_type`, role unchanged (Task 8, `test_role_update_refused_above_holder_ceiling`).
   This code is not in the spec's error list; it closes the gap "every assignment fits the type ceiling" for role
   edits.
4. A user without a home post (analyst at `/`) read or changed by a group administrator -> 403 `user_self_only`
   for reads, `target_user_more_privileged` for writes (Task 7, `test_group_admin_cannot_touch_users_without_home_post`).
5. The last administrator deleting their own `admin @ /` assignment or downgrading their own type -> 409
   `cannot_remove_own_admin` / `last_admin_required`, nothing changed (Task 7
   `test_last_admin_cannot_drop_own_root_admin`, Task 8 `test_last_admin_cannot_downgrade_self`).

---

## File Map

| File | Responsibility | Task |
|------|----------------|------|
| `app/backend/models/scope_group.py` | `ScopeGroup` table | 1 |
| `app/backend/migrations/versions/021_scope_groups.py` | `scope_groups`, `posts.group_id` | 1 |
| `app/backend/filters/scope_group_filter.py` | `ScopeGroupCriteria`, `ScopeGroupFilterService` | 1 |
| `app/backend/services/scope_groups.py` | parent / depth / cycle / emptiness rules (raise `CodedError`) | 1 |
| `app/backend/api/v1/scope_groups.py` + `models/{request,response,query}/scope_groups.py` | `/scope-groups` | 1 |
| `app/backend/auth/user_types.py` | `UserTypeSpec`, `USER_TYPES`, ceilings | 2 |
| `app/backend/bootstrap/permissions.py` | + `PERMISSION_LEVELS`, `permission_level`, `role_level`, `sync_builtin_roles` | 2 |
| `app/backend/migrations/versions/022_builtin_roles.py` | `roles.builtin_key`, name collisions | 2 |
| `app/backend/api/v1/user_types.py` + `models/response/user_types.py` | `GET /user-types` | 2 |
| `app/backend/auth/scope.py` | pure scope evaluator: `Node`, `ScopeTree`, `Grant` | 3 |
| `app/backend/migrations/support/authz_v2.py` | FROZEN conversion function (used live by Task 3, by 023 and by the report) | 3 |
| `app/backend/auth/legacy_grants.py` | TRANSITIONAL: grants from the old tables (deleted in Task 6) | 3 |
| `app/backend/auth/context.py` | AuthContext grants API | 3, 6 |
| `app/backend/auth/access_control.py` | permission-at-post checks | 4, 5 |
| `app/backend/models/role_assignment.py` | `RoleAssignment` table | 6 |
| `app/backend/migrations/versions/023_authorization_v2.py` | assignments, `users.user_type`, conversion, `*_v1`, log | 6 |
| `app/backend/cli/authz_report.py` | dry-run report | 6 |
| `app/backend/auth/privileges.py` | delegation rules at scope | 6, 7 |
| `app/backend/auth/assignments.py` | create / delete assignment with every rule | 7 |
| `tests/helpers.py` | factories (`make_user` legacy-shaped via the conversion, `assign`, `make_group`) | 6 |

---

### Task 1: Scope groups

**Files:**
- Create: `app/backend/models/scope_group.py`, `app/backend/migrations/versions/021_scope_groups.py`,
  `app/backend/filters/scope_group_filter.py`, `app/backend/services/scope_groups.py`,
  `app/backend/api/v1/scope_groups.py`, `app/backend/api/v1/models/request/scope_groups.py`,
  `app/backend/api/v1/models/response/scope_groups.py`, `app/backend/api/v1/models/query/scope_groups.py`
- Modify: `app/backend/models/post.py`, `app/backend/models/__init__.py`,
  `app/backend/migrations/versions/002_sync_schema_with_models.py` (`TABLES_OWNED_BY_LATER_REVISIONS` += `"scope_groups"`),
  `app/backend/enums/general.py` (`Permissions.SCOPE_GROUP_VIEW/CREATE/UPDATE/ARCHIVE` = `scope_group.view` ...),
  `app/backend/bootstrap/permissions.py` (`PERMISSION_META` Ukrainian texts), `app/backend/enums/routes.py`,
  `app/backend/enums/errors.py`, `app/backend/api/v1/__init__.py`, `app/backend/api/v1/posts.py`,
  `app/backend/api/v1/models/request/posts.py`, `app/backend/api/v1/models/response/posts.py`,
  `tests/test_migrations.py` (`HEAD = "021_scope_groups"`), `nginx-app.conf` if `tests/test_nginx_conf.py` requires
  the new top-level path, `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_scope_groups.py`

**Interfaces:**
- Produces:
  - `ScopeGroup(id, name: str(255), parent_id: Optional[int] FK scope_groups.id, parent_key: int NOT NULL (parent_id or 0, written by the code), description: Optional[str](1000), is_deleted: bool, created_at, updated_at)`, unique `uq_scope_groups_parent_name (parent_key, name)`.
  - `Post.group_id: Optional[int]` FK `scope_groups.id`, index `ix_posts_group_id`.
  - `ScopeGroupRoutes`: `BASE="/scope-groups"`, `ROOT="/"`, `TREE="/tree"`, `GROUP_BY_ID="/{group_id}"`,
    `GROUP_ARCHIVE="/{group_id}/archive"`, `GROUP_RESTORE="/{group_id}/restore"`, `tag()="SCOPE_GROUPS"`.
  - `ErrorCode.SCOPE_GROUP_NOT_FOUND` (404), `SCOPE_GROUP_NOT_ARCHIVED` (409), `SCOPE_GROUP_NOT_EMPTY` (409),
    `SCOPE_GROUP_CYCLE` (409), `SCOPE_GROUP_DEPTH_EXCEEDED` (409, param `max_depth: int`), `SCOPE_GROUP_NAME_TAKEN` (409).
  - `services/scope_groups.py`: `MAX_GROUP_DEPTH = 6`;
    `async def check_new_parent(session, group_id: Optional[int], parent_id: Optional[int]) -> None` (parent exists and
    is not archived, else `SCOPE_GROUP_NOT_FOUND`; not the group itself or a descendant, else `SCOPE_GROUP_CYCLE`;
    depth of parent + height of the moved subtree <= 6, else `SCOPE_GROUP_DEPTH_EXCEEDED`);
    `async def check_empty(session, group_id: int) -> None`; `async def get_group(session, group_id, include_deleted=False) -> ScopeGroup`.
  - Response models: `ScopeGroupResponse(id, name, parent_id, description, is_deleted, created_at, updated_at)`;
    `ScopeTreePost(id, name, code)`; `ScopeTreeGroup(id, name, description, groups: List["ScopeTreeGroup"], posts: List[ScopeTreePost])`;
    `ScopeTreeResponse(groups: List[ScopeTreeGroup], posts: List[ScopeTreePost])` (top level = children of `/`;
    archived groups and posts left out).
  - Requirements in this task (the gate goes in Tasks 4-5): list / get / tree
    `require(all=[SCOPE_GROUP_VIEW], admin=True)`; create `[VIEW, CREATE]`, update `[VIEW, UPDATE]`, archive / restore
    `[VIEW, ARCHIVE]`, all `admin=True`. Visibility is the whole tree for now (Task 7 limits it by scope).
  - Posts: `PostCreate` / `PostUpdate` gain `group_id: Optional[int] = Field(default=None, gt=0)`; an unknown or archived
    group answers 404 `scope_group_not_found`; `PostResponse` gains `group_id`.
  - Audit rows `create` / `update` / `archive` / `restore`, `resource_type="scope_group"`.

- [ ] **Step 1: Write the failing tests** in `tests/test_scope_groups.py` (admin = `make_admin(db)`):

```python
async def test_create_nested_groups_and_tree(client, db, headers):
    # East > Kharkiv > post A; East > post B; post C at the root
    ...
    tree = (await client.get(V1.path(ScopeGroupRoutes.TREE), headers=headers(admin))).json()
    east = next(g for g in tree["groups"] if g["name"] == "East")
    assert [p["id"] for p in east["posts"]] == [post_b.id]
    assert east["groups"][0]["name"] == "Kharkiv" and [p["id"] for p in east["groups"][0]["posts"]] == [post_a.id]
    assert post_c.id in [p["id"] for p in tree["posts"]]

async def test_name_unique_within_parent(client, db, headers):
    # "Sector 1" under East and under West: both 201; second "Sector 1" under East: 409 scope_group_name_taken;
    # two root groups "North": second 409 scope_group_name_taken
    ...

async def test_move_into_own_subtree_is_a_cycle(client, db, headers):
    r = await client.patch(V1.path(ScopeGroupRoutes.GROUP_BY_ID, group_id=east), json={"parent_id": kharkiv}, headers=h)
    assert r.status_code == 409 and r.json()["code"] == "scope_group_cycle"

async def test_depth_limit(client, db, headers):
    # a chain of 6 nested groups is accepted; the 7th answers 409 scope_group_depth_exceeded with max_depth == 6
    ...

async def test_move_subtree_respects_depth_limit(client, db, headers):   # Review Focus 1
    # chain A1..A5 (A5 at depth 5); group B with child B1 (height 2) at the root;
    # PATCH B parent_id=A5 -> 409 scope_group_depth_exceeded, B still at the root
    ...

async def test_archive_only_empty_groups(client, db, headers):
    # group with a live post -> 409 scope_group_not_empty; with a live subgroup -> 409 scope_group_not_empty;
    # empty -> 200 is_deleted True; PATCH archived -> 404 scope_group_not_found;
    # restore -> 200; restore again -> 409 scope_group_not_archived
    ...

async def test_post_group_id(client, db, headers):
    # PUT post with group_id of an archived group -> 404 scope_group_not_found; with a live group -> 200, group_id set
    ...
```

  and in `tests/test_migrations.py` update `HEAD` and add `"scope_groups"` to the table set of
  `test_migration_chain_on_empty_database`, plus assert the `posts` table has a `group_id` column.

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_scope_groups.py -q`
Expected: FAIL (`ImportError` on `ScopeGroupRoutes`)

- [ ] **Step 3: Implement** the model, migration 021 (same style as `020_post_archive.py`: guarded `create_table`,
  guarded `add_column` in `batch_alter_table`, guarded index; downgrade drops index, column, table), the filter
  (`SORTABLE_FIELDS = ["id", "name", "created_at", "updated_at"]`; criteria `id`, `parent_id`, `name_contains`,
  `include_deleted`; query model with the same names, `include_deleted` guarded by
  `check_include_deleted(auth, params, Permissions.SCOPE_GROUP_ARCHIVE)`), the service, the routes and the post fields.
  The tree is built in memory from two queries (all live groups, all live posts with `group_id`).

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest tests/test_scope_groups.py tests/test_migrations.py -q` then `venv\Scripts\python -m pytest -q`
Expected: PASS (after regenerating the contract and adding the CHANGELOG lines)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Scope groups: nested groups of posts, posts.group_id, /scope-groups"
```

---

### Task 2: Permission levels, user types, built-in roles

**Files:**
- Create: `app/backend/auth/user_types.py`, `app/backend/migrations/versions/022_builtin_roles.py`,
  `app/backend/api/v1/user_types.py`, `app/backend/api/v1/models/response/user_types.py`
- Modify: `app/backend/enums/general.py` (`UserType`, `PermissionLevel`, `Permissions.MEDIA_MONITOR = "media.monitor"`),
  `app/backend/bootstrap/permissions.py`, `app/backend/models/role.py`, `app/backend/enums/routes.py`
  (`UserTypeRoutes`, `RoleRoutes.ROLE_CLONE = "/{role_id}/clone"`), `app/backend/enums/errors.py`,
  `app/backend/api/v1/roles.py`, `app/backend/api/v1/models/{request,response,query}/roles.py`,
  `app/backend/filters/role_filter.py`, `app/backend/api/v1/__init__.py`, `tests/test_migrations.py`
  (`HEAD = "022_builtin_roles"`), `tests/test_roles_soft_delete.py` and any test asserting `admin_role_*` codes,
  `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_builtin_roles.py`, `tests/test_user_types.py`, `tests/test_migration_022_builtin_roles.py`

**Interfaces:**
- Consumes: Task 1 permissions `scope_group.*`.
- Produces:
  - `class UserType(str, Enum)`: `VIEWER="viewer"`, `OPERATOR="operator"`, `SENIOR_OPERATOR="senior_operator"`,
    `ANALYST="analyst"`, `POST_ADMIN="post_admin"`, `ADMIN="admin"` (this order is the catalog order).
  - `class PermissionLevel(str, Enum)`: `SYSTEM="system"`, `SCOPED="scoped"`.
  - `bootstrap/permissions.py`: `PERMISSION_LEVELS: Dict[str, PermissionLevel]` with an entry for EVERY `Permissions`
    value (`system` = `role.create/update/archive/delete`, `category/signal_type/tag.create/update/archive`,
    `media_format.create/update/archive`, `scope_group.create/update/archive`, `media.monitor`, `audit.view`,
    `admin.true`; everything else `scoped`); `def permission_level(name: str) -> PermissionLevel`;
    `def role_level(permission_names: Iterable[str]) -> PermissionLevel` (`SYSTEM` when any is system);
    `async def sync_builtin_roles(session) -> Dict[UserType, Role]` called at the end of `seed_roles_and_permissions`.
  - `auth/user_types.py`: `@dataclass(frozen=True) class UserTypeSpec(key: UserType, display_name: str, description: str, base_permissions: FrozenSet[str], allowed_permissions: FrozenSet[str])`;
    `USER_TYPES: Dict[UserType, UserTypeSpec]` with exactly the sets of spec section 4 (`admin`: every `Permissions`
    value for both); `def excess_permissions(user_type: UserType, permission_names: Iterable[str]) -> FrozenSet[str]`.
  - `Role.builtin_key: Optional[str]` (max 50, unique index `ix_roles_builtin_key`); `RoleResponse` gains
    `is_builtin: bool`, `builtin_key: Optional[str]`, `level: PermissionLevel`; `PermissionResponse` gains `level`;
    `RoleCriteria` / `RoleFilterParams` gain `is_builtin: Optional[bool]`.
  - `ErrorCode.BUILTIN_ROLE_NOT_RENAMABLE`, `BUILTIN_ROLE_PERMISSIONS_FIXED`, `BUILTIN_ROLE_NOT_ARCHIVABLE`,
    `BUILTIN_ROLE_NOT_DELETABLE` replace the four `ADMIN_ROLE_*` members (titles name "built-in roles"); every
    `role.name == ADMIN_ROLE_NAME` check in `roles.py` becomes `role.builtin_key is not None`.
  - `POST /roles/{role_id}/clone`: body `RoleClone(name, display_name, description: Optional)` with the validators of
    `RoleCreate`; `require(all=[ROLE_VIEW, ROLE_CREATE], admin=True)`; `check_grant(auth, source permissions)`;
    201 `RoleResponse`; audit `create` with `details={"cloned_from": source_id}`.
  - `GET /user-types` (`UserTypeRoutes.BASE="/user-types"`, `ROOT="/"`, tag `USER_TYPES`), `require()`, response
    `List[UserTypeResponse(key: UserType, display_name, description, base_role: UserTypeRole(id, name, display_name), base_permissions: List[str], allowed_permissions: List[str])]`, lists sorted.
  - Built-in role display names (Ukrainian): viewer "Перегляд", operator "Оператор посту", senior_operator
    "Старший оператор", analyst "Аналітик", post_admin "Адміністратор посту", admin "Адміністратор".

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_user_types.py
def test_every_permission_declares_a_level():
    assert set(PERMISSION_LEVELS) == {p.value for p in Permissions}

def test_role_level():
    assert role_level(["observation.view"]) == PermissionLevel.SCOPED
    assert role_level(["observation.view", "tag.create"]) == PermissionLevel.SYSTEM

def test_ceiling_contains_base_for_every_type():
    for spec in USER_TYPES.values():
        assert spec.base_permissions <= spec.allowed_permissions

def test_operator_sets_match_spec():
    spec = USER_TYPES[UserType.OPERATOR]
    assert spec.base_permissions == {"post.view", "observation.view", "media.view", "category.view",
        "signal_type.view", "tag.view", "observation.create", "observation.update", "observation.archive",
        "media.create", "media.archive", "media_format.view"}
    assert excess_permissions(UserType.OPERATOR, ["observation.view", "user.update"]) == {"user.update"}

async def test_user_types_catalog(client, db, headers):
    body = (await client.get(V1.path(UserTypeRoutes.ROOT), headers=headers(await make_user(db)))).json()
    assert [t["key"] for t in body] == ["viewer", "operator", "senior_operator", "analyst", "post_admin", "admin"]
    operator = body[1]
    assert operator["base_role"]["name"] == "operator" and set(operator["base_permissions"]) <= set(operator["allowed_permissions"])

# tests/test_builtin_roles.py
async def test_seed_creates_and_resyncs_builtin_roles(db):
    # every UserType has a role with builtin_key == key and exactly its base permissions;
    # add an extra grant to "viewer", drop one from "operator", change a display name; seed again -> all restored

async def test_builtin_roles_refuse_changes(client, db, headers):
    # PATCH name -> 409 builtin_role_not_renamable; PATCH permission_ids -> 409 builtin_role_permissions_fixed;
    # archive -> 409 builtin_role_not_archivable; DELETE -> 409 builtin_role_not_deletable

async def test_clone_builtin_role(client, db, headers):
    # admin clones "operator" as "operator_night" -> 201, is_builtin False, same permissions as operator;
    # a caller with role.view + role.create + admin.true but without observation.create -> 403 grant_exceeds_own_permissions

async def test_role_response_level_and_builtin_filter(client, db, headers):
    # admin role: is_builtin True, level "system"; custom role with only observation.view: level "scoped";
    # list with is_builtin=false contains no built-in role

# tests/test_migration_022_builtin_roles.py (slow, rewind technique of test_migration_010_archive_permissions.py)
def test_custom_role_named_like_a_builtin_is_renamed(tmp_path):
    # at 021: roles "admin" and a custom "operator" -> migrate -> "operator_custom", admin.builtin_key == "admin";
    # migrating again changes nothing
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_user_types.py tests/test_builtin_roles.py -q`
Expected: FAIL (`ImportError`)

- [ ] **Step 3: Implement** the enums, levels, `user_types.py`, migration 022 (guarded unique column; renames
  `<key>` to `<key>_custom` with a numeric suffix when taken, for every key but `admin`; sets
  `builtin_key='admin'` on the role named `admin`; downgrade drops the column and its index only, documented in the
  docstring), `sync_builtin_roles` (find by `builtin_key`, else claim a role with that name, else create; replace the
  permission set by difference like `update_role` does; clear `is_deleted`), the role API changes, clone and
  `GET /user-types`.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS (contract regenerated, CHANGELOG lines added, old `admin_role_*` assertions updated)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Built-in roles for every user type, permission levels, role clone, GET /user-types"
```

---

### Task 3: Scope evaluator, frozen conversion, transitional AuthContext

**Files:**
- Create: `app/backend/auth/scope.py`, `app/backend/migrations/support/__init__.py`,
  `app/backend/migrations/support/authz_v2.py`, `app/backend/auth/legacy_grants.py`
- Modify: `app/backend/auth/context.py`
- Test: `tests/test_scope_tree.py`, `tests/test_authz_conversion.py`, `tests/test_auth_context_grants.py`

**Interfaces:**
- Consumes: Task 1 tables, Task 2 `builtin_key`.
- Produces (`auth/scope.py`, pure, no database, no FastAPI):
  - `NodeKind = Literal["root", "group", "post"]`;
    `@dataclass(frozen=True) class Node(kind: NodeKind, id: Optional[int] = None)` with `Node.root()`, `Node.group(id)`,
    `Node.post(id)`, property `key -> str` (`"root"`, `"g:3"`, `"p:7"`), `Node.parse(key) -> Node`; `ROOT = Node.root()`.
  - `@dataclass(frozen=True) class ScopeTree(group_parent: Mapping[int, Optional[int]], archived_groups: FrozenSet[int], post_group: Mapping[int, Optional[int]], archived_posts: FrozenSet[int])`
    with `ancestors(node) -> Tuple[Node, ...]` (the node first, `ROOT` last), `nodes_in_effect_at_post(post_id) -> Tuple[Node, ...]`
    (`ROOT`, every non-archived ancestor group, the post itself when not archived), `posts_under(node) -> FrozenSet[int]`
    (archived posts included), `contains(outer: Node, inner: Node) -> bool`.
  - `@dataclass(frozen=True) class Grant(role_id: int, role_name: str, role_display_name: str, role_is_builtin: bool, node: Node, permissions: FrozenSet[str], assignment_id: Optional[int] = None)`.
- Produces (`migrations/support/authz_v2.py`, FROZEN after Task 6 is merged; stdlib only, own copies of every set):
  - Constants: `ADMIN_TRUE = "admin.true"`, `ACCESS_ALL = "post.access_all"`, `REMOVED`, `SYSTEM` (the system set of
    Task 2 including `admin.true`), `GLOBAL_VIEW = {"role.view", "category.view", "signal_type.view", "tag.view", "media_format.view"}`,
    `ADMIN_RECORD = {"post.view", "user.view", "statistics.view"}`,
    `DEAD_WITHOUT_ADMIN = {"user.create", "user.update", "user.archive", "post.create", "post.update", "post.archive", "role.view", "role.create", "role.update", "role.archive", "role.delete", "category.create", "category.update", "category.archive", "signal_type.create", "signal_type.update", "signal_type.archive", "tag.create", "tag.update", "tag.archive", "media.update", "media_format.create", "media_format.update", "media_format.archive", "export.create", "audit.view", "scope_group.view", "scope_group.create", "scope_group.update", "scope_group.archive", "media.monitor"}`
    (every permission whose state-changing routes carry the admin gate today; read-side uses such as `include_deleted`
    are lost with it, the safe direction), `TYPE_ORDER` and `TYPE_CEILINGS` (copy of Task 2's ceilings).
  - Difference tags: `DIFF_UPLOAD_LIST = "upload_list_follows_manage_any"`, `DIFF_COUNTERS = "observation_counters_follow_view"`,
    `DIFF_ARCHIVED_RECORDS = "archived_records_need_archive"`, `DIFF_ARCHIVED_POST_DATA = "archived_post_observations_need_post_archive"`,
    `DIFF_NO_PERMISSIONS = "no_permissions"`.
  - `@dataclass(frozen=True) class LegacyRole(id: int, name: str, display_name: str, is_deleted: bool, permissions: FrozenSet[str])`;
    `@dataclass(frozen=True) class LegacyUser(id: int, username: str, post_id: Optional[int], role_ids: Tuple[int, ...], access: Tuple[Tuple[int, bool], ...])`;
    `RoleRef = Union[int, str]` (existing role id, or the name of a derived role);
    `@dataclass(frozen=True) class PlannedAssignment(role: RoleRef, node: str)`;
    `@dataclass(frozen=True) class DerivedRole(name: str, display_name: str, source_role_id: int, permissions: FrozenSet[str], is_deleted: bool)`;
    `@dataclass(frozen=True) class UserPlan(user_id: int, user_type: str, assignments: Tuple[PlannedAssignment, ...], dropped: FrozenSet[str], differences: Tuple[str, ...])`;
    `@dataclass(frozen=True) class ConversionPlan(users: Tuple[UserPlan, ...], derived_roles: Tuple[DerivedRole, ...])`.
  - `def plan_conversion(users: Iterable[LegacyUser], roles: Mapping[int, LegacyRole], admin_role_id: Optional[int], taken_role_names: Iterable[str]) -> ConversionPlan`
    implementing spec section 8 rules 1-7, with one addition required by "held anywhere": when a user has neither a home
    post nor access rows and no `post.access_all`, the `GLOBAL_VIEW` permissions of a role go to its root part
    (otherwise a dictionary reader without a post would lose the dictionaries). A role whose post part equals its own
    stored set (`permissions - REMOVED`) is assigned as itself; derived names `<name>_system` / `_posts` / `_view`
    (display names with " (система)" / " (пости)" / " (перегляд)"), deduplicated by exact permission set, made unique
    against `taken_role_names` with `_2`, `_3` ...
- Produces (`auth/legacy_grants.py`, TRANSITIONAL, deleted in Task 6):
  `def legacy_grants(user: User, role_rows, scope_rows, admin_role_id: Optional[int]) -> Tuple[Grant, ...]` - runs
  `plan_conversion` for one user (archived roles are not in `role_rows`) and maps each planned assignment to a `Grant`
  (derived roles: the source role's id and name, the derived display name, the planned permission set).
- Produces (`auth/context.py`): `AuthContext` keeps every current field and method with today's semantics, and gains
  `grants: Tuple[Grant, ...]`, `tree: ScopeTree`, and:
  `held_anywhere -> FrozenSet[str]` (property), `has_anywhere(p) -> bool`, `has_at_root(p) -> bool`,
  `posts_with(p) -> Optional[FrozenSet[int]]` (`None` = every post), `has_at_post(p, post_id: int) -> bool`,
  `covers(p, node: Node) -> bool`, `covers_user(p, user: User) -> bool` (home post, or `/` when none);
  `p` is `Union[Permissions, str]`. `async def load_scope_tree(session) -> ScopeTree` (two queries).
  `load_auth_context` adds `Role.builtin_key` to the role rows and builds `grants` with `legacy_grants`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_scope_tree.py - tree: East(1) > Kharkiv(2) > posts 7, 8; East > post 12; West(3) > post 9; post 20 at /
def test_group_grant_reaches_descendant_posts():
    assert tree.posts_under(Node.group(1)) == {7, 8, 12}
def test_post_added_later_is_covered():          # rebuilt tree with post 13 in Kharkiv: posts_under(East) has 13
def test_archived_post_direct_grant_stops_inherited_stays():
    t = replace(tree, archived_posts=frozenset({7}))
    assert Node.post(7) not in t.nodes_in_effect_at_post(7) and Node.group(1) in t.nodes_in_effect_at_post(7)
def test_archived_group_grant_does_not_apply():   # archived_groups={2}: Node.group(2) not in nodes_in_effect_at_post(8)
def test_contains():
    assert tree.contains(Node.group(1), Node.post(8)) and not tree.contains(Node.group(1), Node.post(9))
    assert tree.contains(ROOT, Node.group(3)) and not tree.contains(Node.group(1), ROOT)
def test_node_key_roundtrip():
    assert Node.parse("g:3") == Node.group(3) and Node.post(7).key == "p:7" and Node.parse("root") == ROOT

# tests/test_authz_conversion.py - one test per rule, pure
def test_builtin_admin_holder_becomes_admin_at_root(): ...
def test_access_all_puts_every_role_at_root(): ...
def test_home_write_and_read_rows():
    # operator role R, home 7, access {8: True, 9: False}
    # -> R @ p:7, R @ p:8, "<R>_view" @ p:9 holding only R's *.view permissions; user_type "operator"
def test_dead_permissions_dropped_without_admin_true():   # tag.create, user.update -> plan.dropped, not assigned
def test_admin_true_role_is_split():
    # custom role with admin.true, post.view, user.view, tag.create, observation.view; home 7
    # -> "<R>_system" {post.view, user.view, tag.create} @ root, "<R>_posts" {observation.view} @ p:7
    #    (a permission of the root part is never repeated in the post part)
def test_user_without_post_keeps_dictionary_views_at_root(): ...
def test_type_inference_order_and_analyst_preference(): ...
def test_archived_role_gives_archived_derived_roles(): ...
def test_derived_roles_deduplicated_and_names_unique():   # "ops_system" taken -> "ops_system_2"
def test_differences_are_reported():   # admin.true + media.manage_any without access_all -> DIFF_UPLOAD_LIST etc.

# tests/test_auth_context_grants.py - DB, legacy factories unchanged
async def test_grants_match_legacy_scope(db):
    user = await make_user(db, perms=[P.OBSERVATION_VIEW, P.OBSERVATION_UPDATE], post_id=a.id, extra_access={b.id: True, c.id: False})
    auth = await load_auth_context(db, user)
    assert auth.posts_with(P.OBSERVATION_VIEW) == auth.readable_post_ids == {a.id, b.id, c.id}
    assert auth.posts_with(P.OBSERVATION_UPDATE) == auth.writable_post_ids == {a.id, b.id}
async def test_admin_sees_every_post_record(db):   # make_admin -> posts_with(POST_VIEW) is None, has_at_root(TAG_CREATE)
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_scope_tree.py tests/test_authz_conversion.py tests/test_auth_context_grants.py -q`
Expected: FAIL (`ModuleNotFoundError`)

- [ ] **Step 3: Implement** `scope.py`, `authz_v2.py`, `legacy_grants.py` and the AuthContext additions. No route
  changes in this task.

- [ ] **Step 4: Run the tests and the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS (no existing test changes: this task is additive)

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Scope evaluator, frozen authorization v2 conversion, grants on AuthContext"
```

---

### Task 4: Permission-at-post checks for posts, users, statistics, uploads

**Files:**
- Modify: `app/backend/auth/require.py`, `app/backend/auth/access_control.py`, `app/backend/filters/simple_engine.py`,
  `app/backend/filters/post_filter.py`, `app/backend/filters/user_filter.py`, `app/backend/api/v1/posts.py`,
  `app/backend/api/v1/users.py` (read routes only: `list_users`, `get_user`), `app/backend/api/v1/search.py`
  (`search_users`, `search_posts`), `app/backend/api/v1/statistics.py`, `app/backend/api/v1/media_uploads.py`
  (`list_upload_sessions`), `app/backend/services/observation_counts.py` if its scope argument changes, tests that
  assert old behaviour listed in Step 1, `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_scope_access.py`

**Interfaces:**
- Consumes: Task 3 AuthContext grants API.
- Produces:
  - `Require.evaluate` checks `all` / `any` against `auth.held_anywhere` (dead permissions of non-administrators no
    longer count, so removing a gate never widens access); the `admin` flag still works (legacy `is_admin`) for the
    routes Task 5 has not converted yet.
  - `AccessControlService` new static methods (the old ones stay until Task 5 deletes them):
    `scope_for(auth, permission) -> Optional[FrozenSet[int]]`;
    `resolve_scope(auth, permission, requested_post_id: Optional[int]) -> Optional[FrozenSet[int]]`;
    `check_post(auth, permission, post_id: int, resource_type: str = "post") -> None` - 403 `no_post_assigned` when the
    caller holds the permission nowhere; `post_access_denied` (params `resource_type`, `post_id`) for a `*.view`
    permission, `post_write_denied` otherwise; both with `required=Require.of(all=[permission])`, `missing=[permission]`;
    `check_user_view(auth, target: User) -> None` - the caller, or `user.view` covering the target, else 403 `user_self_only`;
    `archived_scope(auth, params, permission) -> Optional[FrozenSet[int]]` - runs `check_include_deleted`, returns
    `posts_with(permission)` (`None` = no restriction).
  - `SimpleFilterEngine._apply_soft_delete(query, params, post_column=None, archived_post_ids: Optional[FrozenSet[int]] = None)`:
    with `include_deleted` and a `post_column`, archived rows are kept only where `post_column` is in
    `archived_post_ids` (`None` = everywhere); without `include_deleted` unchanged.
- Route mapping (each `admin=True` on these routes is removed):

| Handler | New check |
|---------|-----------|
| `posts.list_posts`, `search.search_posts` | `scope_for(POST_VIEW)`; archived rows via `archived_scope(POST_ARCHIVE)` on `Post.id` |
| `posts.get_post` | `check_post(POST_VIEW, post_id)`; an archived post also needs `has_at_post(POST_ARCHIVE)` |
| `posts.create_post` | `covers(POST_CREATE, Node.group(group_id) or ROOT)` else 403 `post_write_denied` |
| `posts.update_post` | `check_post(POST_UPDATE, post_id)`; a `group_id` change also needs `covers(POST_UPDATE, new location)` |
| `posts.archive_post`, `restore_post` | `check_post(POST_ARCHIVE, post_id)` (archived post: inherited grants only) |
| `posts.get_post_observation_stats` | `check_post(OBSERVATION_VIEW, post_id)` (spec: counters follow `observation.view`) |
| `users.list_users`, `search.search_users` | `resolve_scope(USER_VIEW, filters.post_id)`; users without a home post only when the scope is `None`; the caller always included |
| `users.get_user` | `check_user_view`; an archived user also needs `user.archive` covering them |
| `statistics.get_statistics` | `scope_for(STATISTICS_VIEW)` |
| `media_uploads.list_upload_sessions` | other users' sessions over `scope_for(MEDIA_MANAGE_ANY)` |

- [ ] **Step 1: Write the failing tests** in `tests/test_scope_access.py` (legacy factories; group grants arrive in
  Task 7):

```python
async def test_post_records_follow_post_view(client, db, headers):
    # user with post.view at home A and observation.view: GET post B -> 403 post_access_denied with missing ["post.view"]
async def test_observation_stats_follow_observation_view(client, db, headers):
    # make_user(perms=[P.POST_VIEW, P.OBSERVATION_VIEW], post_id=A) (no admin.true): stats of B -> 403
    # make_admin(): stats of any post -> 200
async def test_archived_post_needs_post_archive_at_post(client, db, headers): ...
async def test_users_without_home_post_need_user_view_at_root(client, db, headers):
    # home-post user with user.view at A lists users: sees A's users and themselves, never users without a post;
    # make_admin lists them all
async def test_include_deleted_shows_archived_only_where_archive_is_held(client, db, headers):
    # user.view at A and B (B read-only row), user.archive only where the role is full (A):
    # include_deleted lists archived users of A, not of B
async def test_removed_gate_does_not_widen(client, db, headers):
    # make_user(perms=[POST_VIEW, POST_CREATE]) without admin.true: POST post -> 403 permission_denied (post.create dead)
```

  Update the tests that pin the old administrator exception, each in the direction of the table above:
  `tests/test_observation_counts.py` (admin without the data permission), `tests/test_post_archive.py` (archived
  record visibility), `tests/test_media_uploads.py` / `tests/test_media_listing_scope.py` (upload list),
  `tests/test_require.py` (evaluation on grants).

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_scope_access.py -q`
Expected: FAIL

- [ ] **Step 3: Implement** the require change, the new `AccessControlService` methods, the filter extension and the
  handlers of the table.

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Permission-at-post checks for posts, users, statistics and uploads; gate removed there"
```

---

### Task 5: Remaining routes, admin gate removed everywhere

**Files:**
- Modify: `app/backend/api/v1/observations.py`, `app/backend/api/v1/media.py`, `app/backend/api/v1/search.py` (the
  rest), `app/backend/api/v1/categories.py`, `signal_types.py`, `tags.py`, `media_formats.py`, `roles.py`, `users.py`
  (write routes: drop `admin=True` only), `scope_groups.py`, `app/backend/api/shared/helpers/observation_helper.py`,
  `app/backend/api/shared/helpers/upload_helper.py`, `app/backend/filters/observation_filter.py`,
  `app/backend/filters/media_filter.py`, `app/backend/auth/require.py`, `app/backend/auth/access_control.py`,
  `app/backend/auth/route_access.py`, `app/backend/enums/errors.py`, `app/backend/errors/openapi.py`,
  `app/backend/api/v1/auth.py`, `app/backend/api/v1/models/response/auth.py`, tests asserting `admin_required`,
  `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_scope_access.py` (extend), `tests/test_route_access.py`, `tests/test_require.py`

**Interfaces:**
- Consumes: Task 4 `AccessControlService` methods.
- Produces:
  - `Require(all, any)` and `require(*, all=(), any=())` without `admin`; `PermissionCheckResult(allowed, missing)`;
    `RequiredPermissions(all, any)`; `PermissionCheckResponse(allowed, missing)`; `AccessErrorCode.ADMIN_REQUIRED` and
    `errors/openapi.ADMIN` removed (every `*ADMIN` becomes `*GUARDED`).
  - `AccessControlService` without `post_scope`, `resolve_post_scope`, `check_post_view`, `check_post_write`,
    `post_record_scope`, `resolve_record_scope`, `check_post_record_view`, `check_user_visibility`;
    `check_observation_ownership` uses `has_at_post(OBSERVATION_MANAGE_ANY, observation.post_id)`;
    `check_media_ownership` uses `has_at_post(MEDIA_MANAGE_ANY, <post of the media's observation>)`.
- Route mapping:

| Handler | New check |
|---------|-----------|
| observations list / search / frequencies list | `resolve_scope(OBSERVATION_VIEW, ...)`, archived via `archived_scope(OBSERVATION_ARCHIVE)` |
| observation create / update / disappeared / frequencies writes (`get_observation_for_write`) | `check_post(OBSERVATION_CREATE or OBSERVATION_UPDATE, post_id)` + ownership |
| `_set_observation_archived` | `check_post(OBSERVATION_ARCHIVE, post_id)` + ownership; `ensure_post_not_archived` skipped only when `has_at_post(POST_ARCHIVE, post_id)` |
| `generate_observation_report` (export) | `require(all=[OBSERVATION_VIEW, EXPORT_CREATE])`; scope = `scope_for(OBSERVATION_VIEW)` intersected with `scope_for(EXPORT_CREATE)` (`None` intersect X = X) |
| media list / search | `scope_for(MEDIA_VIEW)`, archived via `archived_scope(MEDIA_ARCHIVE)` on the observation's post |
| media info / download / stream / thumbnail / storyboard / share / list for observation | `check_post(MEDIA_VIEW, post_id)` |
| media archive / restore | `check_post(MEDIA_ARCHIVE, post_id)` + ownership |
| media regenerate thumbnail, media update | `check_post(MEDIA_UPDATE, post_id)` |
| upload sessions (`load_observation_for_upload`, `_owned_session`) | `check_post(MEDIA_CREATE, post_id)`; other users' sessions with `has_at_post(MEDIA_MANAGE_ANY)` |
| `media.get_storage_statistics`, `get_poster_status` | `require(all=[MEDIA_MONITOR])` |
| dictionaries, media formats, roles, scope groups | same `require(all=[...])` without `admin` |
| users write routes | same `require(all=[...])` without `admin` (privileges unchanged until Task 6) |

- [ ] **Step 1: Write the failing tests**

```python
async def test_export_without_gate(client, db, headers):
    # custom role with observation.view + export.create but no admin.true -> previously 403 admin_required;
    # export.create is dead for non-administrators (conversion), so still 403 permission_denied with missing ["export.create"];
    # make_admin -> 200
async def test_media_monitor_route(client, db, headers):
    # admin -> 200 on storage statistics; a user with media.view only -> 403 permission_denied missing ["media.monitor"]
async def test_manage_any_applies_at_its_posts_only(client, db, headers):
    # observation.manage_any in a role at home A, plain role at B (write row): archive another user's observation in B -> 403 observation_not_owner
def test_route_access_has_no_admin_flag():
    # every RouteAccessEntry.require.as_dict() has exactly the keys {"all", "any"}
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_scope_access.py tests/test_route_access.py -q`
Expected: FAIL

- [ ] **Step 3: Implement** the table, then delete the gate and the old methods; `grep -rn "admin=True\|ADMIN_REQUIRED\|is_admin" app/backend/api` must only find `is_admin` in the auth response (Task 8 removes it).

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Every route checks only its permissions at the post; admin gate and admin_required removed"
```

---

### Task 6: Data switch to role assignments

**Files:**
- Create: `app/backend/models/role_assignment.py`, `app/backend/migrations/versions/023_authorization_v2.py`,
  `app/backend/cli/authz_report.py`
- Delete: `app/backend/models/user_post_access.py`, `app/backend/auth/legacy_grants.py`
- Modify: `app/backend/models/role.py` (`UserRole` removed), `app/backend/models/user.py` (`user_type`, relationship
  `assignments`), `app/backend/models/post.py`, `app/backend/models/__init__.py`, `app/backend/enums/general.py`
  (`IS_ADMIN`, `POST_ACCESS_ALL` removed), `app/backend/bootstrap/permissions.py` (their META and levels removed),
  `app/backend/bootstrap/seed.py`, `app/backend/cli/init_user.py`, `app/backend/auth/context.py`,
  `app/backend/auth/privileges.py`, `app/backend/api/v1/users.py`, `app/backend/api/v1/roles.py`,
  `app/backend/api/v1/models/{request,response}/users.py`, `app/backend/enums/routes.py` (`USER_ROLES`,
  `USER_POST_ACCESS`, `USER_POST_ACCESS_BY_POST` removed), `app/backend/enums/errors.py` (`POST_ACCESS_HOME_POST`,
  `POST_ACCESS_NOT_FOUND` removed), `002_sync_schema_with_models.py` (`TABLES_OWNED_BY_LATER_REVISIONS` += `"role_assignments"`),
  `tests/helpers.py`, every test using `P.IS_ADMIN` / `P.POST_ACCESS_ALL` / `UserRole` / `UserPostAccess` /
  the removed routes, `tests/test_migrations.py` (`HEAD = "023_authorization_v2"`), `CHANGELOG.md`,
  `api-contract/v1/openapi.json`
- Test: `tests/test_migration_023_authz.py`, `tests/test_authz_report.py`, `tests/test_admin_privileges.py` (rewritten)

**Interfaces:**
- Consumes: Task 3 `plan_conversion`, `Node`, `Grant`, `ScopeTree`; Task 2 `UserType`, `USER_TYPES`, built-in roles.
- Produces:
  - `RoleAssignment(id, user_id FK, role_id FK, scope_group_id: Optional FK, scope_post_id: Optional FK, scope_key: str(24), created_at, created_by: Optional[int])`,
    table `role_assignments`, unique `uq_role_assignments_user_role_scope (user_id, role_id, scope_key)`, CHECK
    `ck_role_assignments_one_scope` (not both scope ids set); property `node -> Node`;
    `def scope_columns(node: Node) -> dict` (the three scope fields of a node).
  - `User.user_type: str` (max 32, NOT NULL); `UserCreateForm.user_type: UserType` (required); `role_ids` removed from
    both user forms.
  - `AuthContext(user, grants, tree, session_id)`: `permissions` is now `held_anywhere`; `roles` = distinct roles of the
    grants (`RoleBrief`); `is_admin` = holds the built-in `admin` role at `ROOT` (kept only for the auth response
    until Task 8); `readable_post_ids`, `writable_post_ids`, `has_post_access_all`, `can_view_post`, `can_write_post`,
    `post_scope`, `accessible_post_ids_api` removed (the auth response computes `accessible_post_ids` as
    `scope_for(OBSERVATION_VIEW)` until Task 8 removes the field). One grants query: assignments joined with
    non-archived roles and their permissions.
  - `privileges.py`: `check_grant(auth, permissions)` unchanged (role edits); `check_grant_at(auth, permissions, node: Node) -> None`
    (403 `grant_exceeds_own_permissions`, `missing` = permissions not covered at the node);
    `async def check_target_user(session, auth, target: User, permission=Permissions.USER_UPDATE) -> None` (the caller
    `covers_user(permission, target)` and `check_grant_at` succeeds for every assignment of the target; 403
    `target_user_more_privileged`); `async def check_an_admin_remains(session)` (an active, non-archived user has an
    assignment of the built-in `admin` role with `scope_key == "root"`); `async def check_admin_kept(session, auth)`.
  - `POST /users/` creates the user with `user_type` and the default assignment: the type's base role at the home post,
    or at `ROOT` without a home post or when the base role is `system` level; the caller needs `covers(USER_CREATE, that node)`
    and `check_grant_at(base role permissions, that node)`.
  - Every existing `check_target_user(session, auth, user)` call in `users.py` passes the route's permission:
    `USER_UPDATE` for `PATCH /users/{id}` and the session routes, `USER_ARCHIVE` for archive / restore.
    `PATCH /users/{id}` with a new `post_id` also needs `covers(USER_UPDATE, Node.post(new_post_id))` (spec section 6
    rule 2: covering both the old and the new home post).
  - Migration 023 (spec section 8): guarded `create_table("role_assignments")`; guarded nullable `users.user_type`;
    loads legacy rows (tolerates a missing `user_roles` table on fresh installs); `plan_conversion`; inserts derived
    roles with their grants, assignments, `user_type`; one `audit_logs` row per user (`action="migrate"`,
    `resource_type="user"`, `details` = the user plan); table `authz_v2_migration_log(id, kind, ref_id, data JSON)`
    recording removed permission rows and grants, derived role ids; deletes the `admin.true` / `post.access_all`
    grants and rows; renames `user_roles` -> `user_roles_v1`, `user_post_access` -> `user_post_access_v1`; sets
    `user_type` NOT NULL. Downgrade restores exactly from the log and the `*_v1` tables.
  - `python -m app.backend.cli.authz_report [--json] [--url URL]` (default `settings.database.url`): reads a database at
    revision 022 read-only and prints the plan (`user`, `type`, `before`, `after`, `dropped`, `differences`, then the
    derived roles); at 023 or later prints `already converted` and exits 0.
  - `tests/helpers.py`: `ADMIN_TRUE = "admin.true"`, `ACCESS_ALL = "post.access_all"`;
    `make_user(db, perms=(), post_id=None, extra_access=None, role=None, username=None, user_type: Optional[UserType] = None)`
    builds a `LegacyUser` and writes what `plan_conversion` plans (so every existing test keeps its meaning); `user_type`
    overrides the planned type when given;
    `make_admin(db, post_id=None)` -> type `admin`, built-in `admin` role at `ROOT`;
    `async def assign(db, user, role, node: Node) -> RoleAssignment`;
    `async def make_group(db, name=None, parent_id=None) -> ScopeGroup`; `make_post(db, name=None, group_id=None)`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_migration_023_authz.py (slow): migrate a fresh db to head, downgrade to 022_builtin_roles,
# insert legacy data, migrate again
def test_conversion_writes_assignments_types_and_log(tmp_path):
    # admin user -> user_type "admin", admin @ root; operator with home 7, write row 8, read row 9
    # -> assignments (R, p:7), (R, p:8), (R_view, p:9); admin.true rows gone from permissions and role_permissions;
    # tables user_roles_v1 and user_post_access_v1 exist; one audit row action "migrate" per user
def test_downgrade_restores_exactly(tmp_path):
    # downgrade to 022 -> user_roles and user_post_access rows equal the inserted ones, admin.true grants back,
    # derived roles and role_assignments gone
def test_fresh_install_chain(tmp_path):   # empty db to head succeeds; role_assignments exists; no user_roles table

# tests/test_authz_report.py (slow)
def test_report_matches_migration(tmp_path):
    # db at 022 with legacy data -> authz_report --json --url <db> lists the same type and assignments per user
    # as the audit rows written by the migration afterwards

# tests/test_admin_privileges.py (rewritten around assignments; keep every existing scenario's intent)
async def test_last_admin_is_the_builtin_admin_at_root(db): ...
async def test_create_user_makes_default_assignment(client, db, headers):
    # POST /users/ with user_type "operator", post_id A -> one assignment: built-in operator @ p:A
    # without post_id -> operator @ root; user_type "admin" with post_id -> admin @ root
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_migration_023_authz.py tests/test_admin_privileges.py -q`
Expected: FAIL

- [ ] **Step 3: Implement** in this order: model and migration, factories, AuthContext loading, privileges,
  users and roles routes (`delete_role` and `role_permission_names` holders now come from `role_assignments`), seed
  and `init_user` (dev admin: type `admin`, `admin @ /`), the report CLI; delete `legacy_grants.py` and
  `user_post_access.py`; replace `P.IS_ADMIN` / `P.POST_ACCESS_ALL` in tests with `ADMIN_TRUE` / `ACCESS_ALL`;
  delete `tests/test_post_access.py` scenarios whose routes are gone (their access semantics are covered by
  `extra_access` in the factory and by Task 7 tests).

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Role assignments replace user_roles and post access; migration 023 with report and exact downgrade"
```

---

### Task 7: Assignment API, delegation at scope, group visibility

**Files:**
- Create: `app/backend/auth/assignments.py`, `app/backend/api/v1/models/request/assignments.py`,
  `app/backend/api/v1/models/response/assignments.py`
- Modify: `app/backend/enums/routes.py` (`UserRoutes.USER_ASSIGNMENTS = "/{user_id}/assignments"`,
  `USER_ASSIGNMENT_BY_ID = "/{user_id}/assignments/{assignment_id}"`), `app/backend/enums/errors.py`,
  `app/backend/api/v1/users.py`, `app/backend/api/v1/scope_groups.py`, `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_assignments.py`, `tests/test_scope_groups.py` (visibility)

**Interfaces:**
- Consumes: Task 6 `RoleAssignment`, `check_grant_at`, `check_target_user`, `check_an_admin_remains`, `check_admin_kept`;
  Task 2 `role_level`, `excess_permissions`.
- Produces:
  - `ErrorCode.ASSIGNMENT_NOT_FOUND` (404), `ASSIGNMENT_EXISTS` (409), `LAST_ASSIGNMENT_REQUIRED` (409),
    `ROLE_REQUIRES_ROOT_SCOPE` (409, `system_permissions: List[str]`), `ROLE_EXCEEDS_USER_TYPE` (409,
    `user_type: str`, `excess_permissions: List[str]`); `FieldErrorCode.SCOPE_ID_REQUIRED`, `SCOPE_ID_NOT_ALLOWED`.
  - Request `ScopeRef(kind: Literal["root", "group", "post"], id: Optional[PositiveInt] = None)` (model validator:
    `root` without id, others with id); `AssignmentCreate(role_id: PositiveInt, scope: ScopeRef)`.
  - Response `AssignmentResponse(id, role: AssignmentRole(id, name, display_name, is_builtin), scope: ScopeResponse(kind, id: Optional[int], name: str), created_at)`
    (`name` of `/` is the application name from `settings.app.name`).
  - `auth/assignments.py`: `async def create_assignment(session, auth, target: User, role: Role, node: Node) -> RoleAssignment`
    and `async def delete_assignment(session, auth, target: User, assignment: RoleAssignment) -> None`, applying in
    order: node exists and is live (404 `scope_group_not_found` / `post_not_found`), `check_target_user`, role
    archived -> 404 `role_not_found`, level (`ROLE_REQUIRES_ROOT_SCOPE`), ceiling (`ROLE_EXCEEDS_USER_TYPE`),
    `check_grant_at`, duplicate (`ASSIGNMENT_EXISTS`, also from the unique index via `unique_violation_as`); delete:
    last one (`LAST_ASSIGNMENT_REQUIRED`), then `check_admin_kept` when it is `admin @ /`. Audit rows `create` /
    `delete`, `resource_type="role_assignment"`.
  - Routes: `GET /users/{user_id}/assignments` `require(all=[USER_VIEW])` + `check_user_view`; `POST` and `DELETE`
    `require(all=[USER_VIEW, USER_UPDATE])`.
  - `/scope-groups` list, get and tree show only groups inside nodes where `scope_group.view` is held, and in the tree
    only posts with `post.view` in effect.

- [ ] **Step 1: Write the failing tests** (`tests/test_assignments.py`; tree East > Kharkiv > A; West > B; group admin
  = type `post_admin`, built-in `post_admin` @ East)

```python
async def test_group_admin_assigns_inside_own_subtree(client, db, headers):
    # group admin assigns built-in operator to a user with home post A at Kharkiv -> 201; GET lists it with scope name "Kharkiv"
async def test_group_admin_cannot_assign_outside_own_subtree(client, db, headers):   # Review Focus 2
    # same target, scope root -> 403 grant_exceeds_own_permissions; scope West -> 403;
    # target with home post B -> 403 target_user_more_privileged; no row written in any case
async def test_group_admin_cannot_touch_users_without_home_post(client, db, headers):   # Review Focus 4
    # analyst without home post: GET user -> 403 user_self_only; POST assignment -> 403 target_user_more_privileged
async def test_system_role_only_at_root(client, db, headers):
    # admin assigns a custom role holding tag.create at post A -> 409 role_requires_root_scope with system_permissions ["tag.create"]
async def test_role_above_type_ceiling(client, db, headers):
    # operator-type user, role with user.update -> 409 role_exceeds_user_type, excess_permissions ["user.update"]
async def test_duplicate_and_last_assignment(client, db, headers):   # second identical -> 409 assignment_exists; delete the only one -> 409 last_assignment_required
async def test_last_admin_cannot_drop_own_root_admin(client, db, headers):   # Review Focus 5 -> 409 cannot_remove_own_admin
async def test_assignment_to_archived_group(client, db, headers):   # -> 404 scope_group_not_found
async def test_group_grant_reaches_posts_added_later(client, db, headers):
    # operator @ Kharkiv; a new post created in Kharkiv; the operator creates an observation there -> 201

# tests/test_scope_groups.py
async def test_tree_limited_to_scope(client, db, headers):   # group admin of East sees East's subtree only, no West
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_assignments.py -q`
Expected: FAIL

- [ ] **Step 3: Implement** `auth/assignments.py`, the request / response models, the three routes, scope group
  visibility.

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Assignment API with delegation at scope, ceiling and level checks; scope groups limited by scope"
```

---

### Task 8: User type behaviour and the web client's auth fields

**Files:**
- Modify: `app/backend/enums/general.py` (`SessionRevokeCause.USER_TYPE_CHANGED = "user_type_changed"`),
  `app/backend/enums/errors.py`, `app/backend/api/v1/users.py`, `app/backend/api/v1/models/request/users.py`,
  `app/backend/api/v1/models/response/users.py`, `app/backend/api/v1/models/query/users.py`,
  `app/backend/filters/user_filter.py`, `app/backend/api/v1/roles.py`, `app/backend/api/v1/auth.py`,
  `app/backend/api/v1/models/response/auth.py`, `app/backend/api/v1/models/request/auth.py` (check-permission body),
  `app/backend/auth/context.py` (`is_admin` removed), `CHANGELOG.md`, `api-contract/v1/openapi.json`
- Test: `tests/test_user_types.py` (behaviour), `tests/test_auth_contract.py`

**Interfaces:**
- Consumes: Task 7 rules; Task 2 `USER_TYPES`, `excess_permissions`.
- Produces:
  - `ErrorCode.USER_TYPE_CEILING_EXCEEDED` (409, `user_type: str`, `assignments: List[dict]` each `{id, role, scope, excess_permissions}`);
    `ErrorCode.ROLE_HOLDERS_EXCEED_USER_TYPE` (409, `users: List[dict]` each `{id, username, user_type, excess_permissions}`).
  - `UserUpdateForm.user_type: Optional[UserType]`: rule 4 of spec section 6; every assignment of the old base role
    becomes the new base role at the same node; sessions ended with `USER_TYPE_CHANGED` in the same transaction;
    then `check_admin_kept`.
  - `PATCH /roles/{role_id}` adding permissions refuses with `ROLE_HOLDERS_EXCEED_USER_TYPE` when a holder's type
    ceiling does not allow them, and with `ROLE_REQUIRES_ROOT_SCOPE` when it adds a `system` permission to a role
    assigned below `/`.
  - `UserResponse.user_type: UserType`; `UserCriteria` / `UserFilterParams.user_type: Optional[UserType]`.
  - `AuthorizationInfo` (login and `/auth/me`): `post_id`, `user_type: UserType`, `permissions: List[str]` (held
    anywhere), `assignments: List[AssignmentResponse]`, `root_permissions: List[str]`, `post_permissions: Dict[str, List[str]]`
    (post id -> permissions from group and post grants, for every post they reach); `is_admin`, `accessible_post_ids`
    and `roles` removed.
  - `POST /auth/check-permission` body gains `post_id: Optional[PositiveInt]` (allowed = in effect at that post).

- [ ] **Step 1: Write the failing tests**

```python
async def test_type_change_swaps_base_role_and_ends_sessions(client, db, headers):
    # operator with built-in operator @ A; admin PATCH user_type "senior_operator" -> 200, assignment now senior_operator @ A,
    # the operator's old token -> 401 session revoked; session row revoke_cause "user_type_changed"
async def test_downgrade_above_ceiling_refused(client, db, headers):
    # senior_operator with an extra custom role holding media.update; PATCH user_type "operator" ->
    # 409 user_type_ceiling_exceeded, assignments[0]["excess_permissions"] == ["media.update"], nothing changed
async def test_role_update_refused_above_holder_ceiling(client, db, headers):   # Review Focus 3
    # custom role held by a viewer; PATCH adds observation.update -> 409 role_holders_exceed_user_type, role unchanged
async def test_last_admin_cannot_downgrade_self(client, db, headers):   # Review Focus 5 -> 409, type unchanged
async def test_me_carries_type_and_post_permissions(client, db, headers):
    # operator @ Kharkiv (posts A, A2) and viewer @ B: user_type "operator", "is_admin" not in body,
    # post_permissions[str(A)] contains "observation.update", post_permissions[str(B)] does not, root_permissions == []
async def test_check_permission_at_post(client, db, headers):   # observation.update at A True, at B False
async def test_user_list_filters_by_type(client, db, headers): ...
```

- [ ] **Step 2: Run them to verify they fail**

Run: `venv\Scripts\python -m pytest tests/test_user_types.py tests/test_auth_contract.py -q`
Expected: FAIL

- [ ] **Step 3: Implement** the type change, the role-edit checks, the filter, the auth fields; remove
  `AuthContext.is_admin`.

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "User type change with ceiling, role edits bounded by holder types, auth fields for pages and buttons"
```

---

### Task 9: Documents, contract, live migration check

**Files:**
- Modify: `CLAUDE.md` (section Authorization and "Observation counters" exactly as spec section 9),
  `API.md` (pages by `user_type`, buttons by `root_permissions` + `post_permissions`, scope picker, new problem codes),
  `CHANGELOG.md` (migration guide for the web client: removed fields and routes, their replacements), `DEPLOYMENT.md`
  (rollout: `authz_report` on a database copy before the upgrade), memory files
  `C:\Users\AdMi\.claude\projects\E--Develop-pypr-watch-web\memory\authorization-v2.md` (status: implemented) and
  `permission-model-decisions.md` (marked superseded)
- Test: none new; the whole suite and the live run below

- [ ] **Step 1: Write the documents** (ASCII, no emoji; CLAUDE.md bullets as in spec section 9).

- [ ] **Step 2: Verify the contract is current**

Run: `venv\Scripts\python -m pytest tests/test_api_contract.py tests/test_openapi.py tests/test_error_contract.py tests/test_route_registry.py -q`
Expected: PASS

- [ ] **Step 3: Live migration check** on throwaway MySQL 8 and MariaDB containers (as for 018 - 020, see memory
  `nginx-live-check.md` / `bash-heredoc-backslashes.md` for docker usage): load a 020 schema with legacy data
  (an admin, a custom admin.true role, a read-only access row), run `authz_report`, migrate to head, compare with the
  report, downgrade to 020, upgrade again.
Expected: report equals audit rows; downgrade restores the legacy rows; no errors.

- [ ] **Step 4: Run the whole suite**

Run: `venv\Scripts\python -m pytest -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "Authorization v2 documents: CLAUDE.md, API.md, CHANGELOG migration guide, DEPLOYMENT rollout"
```

---

## After the last task

A final fresh validator subagent (clean context: this plan, the spec, `git diff main...authorization-v2`) reviews the
whole branch for logic, security (delegation, escalation, scope leaks), contract and migration correctness before the
merge. Then superpowers:finishing-a-development-branch.
