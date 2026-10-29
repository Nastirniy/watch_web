"""User guides: the help section (UserGuide rows with their UserGuideFile rows).

Reading is open to every authenticated user, writes need the system permissions guide.create / guide.update /
guide.archive (the routes check them). The audience of a guide is stored as the scope key of a node ("p:<id>" /
"g:<id>", auth/scope.py Node.key; null: everyone): audience_key checks the node exists and is live, guide_views
names it as it is now.

Who sees a guide (plan decision 8): guide_visible is the rule, related_keys the same rule as the set of audience keys
the list filters by in SQL. Both read only the AuthContext of the request (its grants and scope tree): no query.
A guide without an audience is seen by everyone. A guide for node N is seen by
  - a manager: guide.update in effect (a system permission, so a system grant at the root): every guide, also one
    whose node is archived;
  - a caller related to N: N lies on a path through the caller's home post or through the node of one of the
    caller's tree grants in effect (a grant at an archived node applies nowhere and is left out; AuthContext already
    leaves out archived roles), in either direction: N contains it (its brigade, its corps) or lies inside it.
    System grants never widen the audience; a user without a home post and without tree grants sees only guides
    without an audience. Sibling subtrees are not on one path: a guide for one post is not for the other posts of
    its brigade.
An archived N hides the guide from everyone but managers, also when N is on the caller's own path (only N itself
counts: a live post below an archived brigade is live); so does a node the tree does not know (no such row). Every
lookup (get_guide) answers guide_not_found for a guide the caller does not see, exactly as for an unknown one.

The service returns dataclasses (GuideView); each API version builds its response from them. Errors are CodedError;
the route converts them (ApiError.from_coded). No FastAPI.
"""
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, FrozenSet, Iterable, List, Literal, Optional, Sequence, Tuple

from sqlalchemy import or_
from sqlmodel import select
from sqlalchemy.sql.selectable import Select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.auth.context import AuthContext
from app.backend.auth.scope import Node, ScopeTree
from app.backend.enums.errors import ErrorCode
from app.backend.enums.general import Permissions, SortOrder
from app.backend.errors.coded import CodedError
from app.backend.filters.simple_engine import ListCriteria, SimpleFilterEngine
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.models.user_guide import UserGuide, UserGuideFile
from app.backend.utils.row_locks import lock as lock_rows, lock_first

AudienceKind = Literal["post", "group"]
_DESCENDING = (SortOrder.DESCENDING, SortOrder.DESCENDING_NULLS_FIRST, SortOrder.DESCENDING_NULLS_LAST)


# ------------------------------------------------------------------ views

@dataclass(frozen=True)
class GuideAudience:
    """The node whose users the guide is for; name is the node's name now (None only for a node row that is gone)"""
    kind: AudienceKind
    id: int
    name: Optional[str]


@dataclass(frozen=True)
class GuideFileView:
    id: int
    guide_id: int
    file_name: str
    content_type: str
    size_bytes: int
    sha256: str
    language: Optional[str]
    created_at: datetime
    is_deleted: bool


@dataclass(frozen=True)
class GuideView:
    id: int
    title: str
    description: Optional[str]
    sort_order: int
    audience: Optional[GuideAudience]
    files: List[GuideFileView]
    is_deleted: bool
    created_at: datetime
    updated_at: datetime


# ------------------------------------------------------------------ audience

def _is_manager(auth: AuthContext) -> bool:
    """guide.update in effect: a system permission, held through a system grant (at the root)"""
    return auth.has_at_root(Permissions.GUIDE_UPDATE)


def _known(tree: ScopeTree, node: Node) -> bool:
    """A group or a post of the tree (the root is never an audience)"""
    if node.kind == "group":
        return node.id in tree.group_parent
    if node.kind == "post":
        return node.id in tree.post_group
    return False


def _anchors(auth: AuthContext) -> Tuple[Node, ...]:
    """The nodes the caller is related through: the home post, then the node of every tree grant in effect (not at
    an archived node). System grants are left out."""
    tree = auth.tree
    anchors: List[Node] = [Node.post(auth.user.post_id)] if auth.user.post_id is not None else []
    anchors += [grant.node for grant in auth.grants if grant.kind == "tree" and not tree.is_archived(grant.node)]
    return tuple(dict.fromkeys(anchors))


def guide_visible(auth: AuthContext, audience: Optional[Node]) -> bool:
    """Whether the caller sees a guide for the audience node (None: a guide for everyone); the rule is in the module
    docstring (decision 8). Pure: reads only the AuthContext."""
    if audience is None or _is_manager(auth):
        return True
    tree = auth.tree
    if tree.is_archived(audience) or not _known(tree, audience):
        return False
    return any(tree.contains(anchor, audience) or tree.contains(audience, anchor) for anchor in _anchors(auth))


def related_keys(auth: AuthContext) -> Optional[FrozenSet[str]]:
    """The audience keys of the guides the caller sees (guide_visible as a set, for the list's SQL): the ancestors
    and the descendants of every anchor, live known nodes only. None for a manager: every guide."""
    if _is_manager(auth):
        return None
    tree = auth.tree
    related = set()
    for anchor in _anchors(auth):
        related.update(tree.ancestors(anchor))
        related.update(Node.group(group_id) for group_id in tree.groups_under(anchor))
        related.update(Node.post(post_id) for post_id in tree.posts_under(anchor))
    return frozenset(node.key for node in related if _known(tree, node) and not tree.is_archived(node))


def audience_node(guide: UserGuide) -> Optional[Node]:
    return Node.parse(guide.audience_key) if guide.audience_key else None


# ------------------------------------------------------------------ criteria

@dataclass(frozen=True, kw_only=True)
class GuideCriteria(ListCriteria):
    """What the guide list is filtered by; each API version converts its query model into it (the same names).
    q: the title contains it (case-insensitive). include_deleted: archived guides too (and their archived files)."""
    q: Optional[str] = None
    include_deleted: bool = False


class GuideFilterService(SimpleFilterEngine):
    """The guide list: in its order (sort_order, then title, then id); sort_by moves title or sort_order first"""

    MODEL = UserGuide
    CRITERIA = GuideCriteria
    SORTABLE_FIELDS = ["sort_order", "title"]

    @classmethod
    def apply_filters(cls, query: Select, params: GuideCriteria) -> Select:
        params = cls.criteria(params)
        query = cls._apply_soft_delete(query, params)
        query = cls._apply_contains(query, UserGuide.title, params.q)
        query = cls._apply_sorting(query, params)
        return cls._apply_pagination(query, params)

    @classmethod
    def _apply_sorting(cls, query: Select, params: ListCriteria) -> Select:
        """The requested field (sort_order without one) in the requested direction, then the other one ascending,
        then the id: pages neither repeat nor skip rows. Both columns are NOT NULL: the nulls variants of SortOrder
        only give the direction."""
        first = params.sort_by if params.sort_by in cls.SORTABLE_FIELDS else "sort_order"
        second = "title" if first == "sort_order" else "sort_order"
        column = getattr(UserGuide, first)
        leading = column.desc() if params.sort_order in _DESCENDING else column.asc()
        return query.order_by(leading, getattr(UserGuide, second).asc(), UserGuide.id.asc())


# ------------------------------------------------------------------ reads

async def list_guides(session: AsyncSession, criteria: GuideCriteria, auth: AuthContext) -> List[GuideView]:
    """One page of the guides the caller sees (related_keys, applied before the page is cut) with their files
    (archived files only with include_deleted)"""
    query = select(UserGuide)
    keys = related_keys(auth)
    if keys is not None:
        no_audience = UserGuide.audience_key.is_(None)
        query = query.where(or_(no_audience, UserGuide.audience_key.in_(sorted(keys))) if keys else no_audience)
    rows = (await session.exec(GuideFilterService.apply_filters(query, criteria))).all()
    return await guide_views(session, rows, include_deleted_files=GuideFilterService.criteria(criteria).include_deleted)


async def get_guide(session: AsyncSession, guide_id: int, auth: AuthContext, include_deleted: bool = False,
                    lock: bool = False) -> UserGuide:
    """The guide (archived only with include_deleted) when the caller sees it (guide_visible); lock=True locks it
    as the first statement of the write (utils/row_locks.py). Unknown, archived without include_deleted, or not
    for the caller: guide_not_found, one answer for all three."""
    query = select(UserGuide).where(UserGuide.id == guide_id)
    if not include_deleted:
        query = query.where(UserGuide.is_deleted == False)  # noqa: E712
    guide = await lock_first(session, query) if lock else (await session.exec(query)).first()
    if guide is None or not guide_visible(auth, audience_node(guide)):
        raise CodedError(ErrorCode.GUIDE_NOT_FOUND, guide_id=guide_id)
    return guide


async def guide_view(session: AsyncSession, guide: UserGuide, include_deleted_files: bool = False) -> GuideView:
    (view,) = await guide_views(session, [guide], include_deleted_files)
    return view


async def guide_views(session: AsyncSession, guides: Sequence[UserGuide],
                      include_deleted_files: bool = False) -> List[GuideView]:
    """The views of guides, in their order: up to three queries whatever the count (the files; the names of the
    audience posts and of the audience groups, each only when some guide has one)"""
    if not guides:
        return []
    files = await _files_of(session, [guide.id for guide in guides], include_deleted_files)
    audiences = await _audiences(session, [guide.audience_key for guide in guides if guide.audience_key])
    return [GuideView(
        id=guide.id, title=guide.title, description=guide.description, sort_order=guide.sort_order,
        audience=audiences.get(guide.audience_key) if guide.audience_key else None,
        files=files.get(guide.id, []), is_deleted=guide.is_deleted, created_at=guide.created_at,
        updated_at=guide.updated_at,
    ) for guide in guides]


def file_view(row: UserGuideFile) -> GuideFileView:
    return GuideFileView(
        id=row.id, guide_id=row.guide_id, file_name=row.file_name, content_type=row.content_type,
        size_bytes=row.size_bytes, sha256=row.sha256, language=row.language, created_at=row.created_at,
        is_deleted=row.is_deleted)


async def _files_of(session: AsyncSession, guide_ids: List[int],
                    include_deleted: bool) -> Dict[int, List[GuideFileView]]:
    query = select(UserGuideFile).where(UserGuideFile.guide_id.in_(sorted(set(guide_ids))))
    if not include_deleted:
        query = query.where(UserGuideFile.is_deleted == False)  # noqa: E712
    found: Dict[int, List[GuideFileView]] = {}
    for row in (await session.exec(query.order_by(UserGuideFile.id))).all():
        found.setdefault(row.guide_id, []).append(file_view(row))
    return found


async def get_guide_file(session: AsyncSession, guide_id: int, file_id: int, include_deleted: bool = False,
                         lock: bool = False) -> UserGuideFile:
    """A file of the guide (archived only with include_deleted); lock=True locks it, inside a write that began with
    the guide's lock (lock_first: the guide row first). Unknown, of another guide, or archived without
    include_deleted: guide_file_not_found."""
    query = select(UserGuideFile).where(UserGuideFile.id == file_id, UserGuideFile.guide_id == guide_id)
    if not include_deleted:
        query = query.where(UserGuideFile.is_deleted == False)  # noqa: E712
    row = await lock_rows(session, query) if lock else (await session.exec(query)).first()
    if row is None:
        raise CodedError(ErrorCode.GUIDE_FILE_NOT_FOUND, file_id=file_id)
    return row


async def _audiences(session: AsyncSession, keys: Iterable[str]) -> Dict[str, GuideAudience]:
    """The audience of each key, named as the node is now (archived nodes included)"""
    nodes = [Node.parse(key) for key in set(keys)]
    post_ids = sorted(node.id for node in nodes if node.kind == "post")
    group_ids = sorted(node.id for node in nodes if node.kind == "group")
    post_names = dict((await session.exec(select(Post.id, Post.name).where(Post.id.in_(post_ids)))).all()) \
        if post_ids else {}
    group_names = dict((await session.exec(select(ScopeGroup.id, ScopeGroup.name).where(
        ScopeGroup.id.in_(group_ids)))).all()) if group_ids else {}
    return {node.key: GuideAudience(kind=node.kind, id=node.id,
                                    name=(post_names if node.kind == "post" else group_names).get(node.id))
            for node in nodes if node.kind in ("post", "group")}


# ------------------------------------------------------------------ writes

async def audience_key(session: AsyncSession, kind: AudienceKind, node_id: int) -> str:
    """The stored key of an audience node; the node must exist and be live, as for an assignment (auth/assignments.py
    rule 7): an unknown or archived post is post_not_found, a group scope_group_not_found"""
    if kind == "post":
        found = (await session.exec(select(Post.id).where(Post.id == node_id, Post.is_deleted == False))).first()  # noqa: E712
        if found is None:
            raise CodedError(ErrorCode.POST_NOT_FOUND, post_id=node_id)
        return Node.post(node_id).key
    found = (await session.exec(
        select(ScopeGroup.id).where(ScopeGroup.id == node_id, ScopeGroup.is_deleted == False)  # noqa: E712
    )).first()
    if found is None:
        raise CodedError(ErrorCode.SCOPE_GROUP_NOT_FOUND)
    return Node.group(node_id).key
