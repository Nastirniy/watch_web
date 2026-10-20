"""One-time conversion of old observations into the unit style (observation_units rows for history).

The rule: for every post (archived ones included) that is in a corps or a brigade now, every observation of the post
(archived ones included) that has no observation_units row of a unit kind K gets the row of the unit of kind K the
post is in now, created_at = the observation's. Rows that exist are never touched, so a second run fills nothing.

Concurrency (MariaDB with innodb_snapshot_isolation; the API stays live): one transaction per chunk of observations
of one post, begun with lock_first on the post row (two runs serialize per post; a move or a create waits for the
chunk), plain reads of the units and of the kinds already saved, then one INSERT of the missing rows (no INSERT ...
SELECT: it would lock observation rows and fail with error 1020 when one is edited meanwhile). The post lock is the
only lock held, so it cannot form a cycle with post moves or unit writes.
"""
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List, Set

from sqlalchemy import insert
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from app.backend.models.observation import Observation
from app.backend.models.observation_unit import ObservationUnit
from app.backend.models.post import Post
from app.backend.models.scope_group import ScopeGroup
from app.backend.services.audit import create_audit_log
from app.backend.services.units import current_units_of_group
from app.backend.utils.row_locks import lock_first

CHUNK_SIZE = 1000

AUDIT_ACTION = "fill_history"
AUDIT_RESOURCE = "observation_units"


@dataclass(frozen=True)
class UnitFill:
    group_id: int
    kind: str            # ScopeGroupKind value: "corps" | "brigade"
    name: str            # today's name
    observations: int    # observations that got this unit


@dataclass(frozen=True)
class HistoryFill:
    units: List[UnitFill]        # sorted by kind ("brigade" before "corps"), then group_id; only units with > 0
    observations: int            # distinct observations that got at least one row
    rows: int                    # rows inserted (dry run: rows that would be inserted)
    posts_skipped: int           # posts (archived included) in no unit now
    dry_run: bool


async def _saved_kinds(session: AsyncSession, observation_ids: List[int]) -> Dict[int, Set[str]]:
    """observation id -> the unit kinds it has a row of (one query)"""
    rows = await session.exec(
        select(ObservationUnit.observation_id, ScopeGroup.kind)
        .join(ScopeGroup, ScopeGroup.id == ObservationUnit.group_id)
        .where(ObservationUnit.observation_id.in_(observation_ids))
    )
    saved: Dict[int, Set[str]] = {}
    for observation_id, kind in rows.all():
        saved.setdefault(observation_id, set()).add(kind)
    return saved


async def fill_unit_history(session: AsyncSession, dry_run: bool = False, chunk_size: int = CHUNK_SIZE) -> HistoryFill:
    """Fill the missing observation_units rows of old observations (see the module). Commits per chunk; a database
    error stops the run and the chunks done stay (run it again to continue). dry_run reads the same way, writes and
    locks nothing."""
    post_ids = list((await session.exec(select(Post.id).order_by(Post.id))).all())
    await session.commit()

    per_unit: Counter = Counter()      # unit group id -> observations that got it
    filled: Set[int] = set()           # observations that got at least one row
    rows_total = 0
    posts_skipped = 0

    for post_id in post_ids:
        after_id = 0
        while True:
            query = select(Post).where(Post.id == post_id)
            if dry_run:
                post = (await session.exec(query.execution_options(populate_existing=True))).first()
            else:
                post = await lock_first(session, query)
            if post is None:                                   # deleted meanwhile
                await session.commit()
                break
            units = await current_units_of_group(session, post.group_id)
            if not units:
                posts_skipped += 1
                await session.commit()
                break
            chunk = (await session.exec(
                select(Observation.id, Observation.created_at)
                .where(Observation.post_id == post_id, Observation.id > after_id)
                .order_by(Observation.id).limit(chunk_size)
            )).all()
            if not chunk:
                await session.commit()
                break
            saved = await _saved_kinds(session, [observation_id for observation_id, _ in chunk])
            new_rows = []
            for observation_id, created_at in chunk:
                have = saved.get(observation_id, set())
                missing = [unit_id for kind, unit_id in units.items() if kind not in have]
                for unit_id in missing:
                    new_rows.append({"group_id": unit_id, "created_at": created_at, "observation_id": observation_id})
                    per_unit[unit_id] += 1
                if missing:
                    filled.add(observation_id)
            if new_rows and not dry_run:
                await session.execute(insert(ObservationUnit), new_rows)
            rows_total += len(new_rows)
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
            after_id = chunk[-1][0]

    if rows_total and not dry_run:
        await create_audit_log(
            session, action=AUDIT_ACTION, resource_type=AUDIT_RESOURCE,
            new_values={"units": {str(unit_id): count for unit_id, count in per_unit.items()},
                        "observations": len(filled), "rows": rows_total},
        )
        await session.commit()

    described: Dict[int, tuple] = {}
    if per_unit:
        found = await session.exec(select(ScopeGroup.id, ScopeGroup.kind, ScopeGroup.name)
                                   .where(ScopeGroup.id.in_(sorted(per_unit))))
        described = {group_id: (kind, name) for group_id, kind, name in found.all()}
        await session.commit()
    units_filled = sorted(
        (UnitFill(group_id=unit_id, kind=described[unit_id][0], name=described[unit_id][1], observations=count)
         for unit_id, count in per_unit.items() if unit_id in described),
        key=lambda fill: (fill.kind, fill.group_id),
    )
    return HistoryFill(units=units_filled, observations=len(filled), rows=rows_total,
                       posts_skipped=posts_skipped, dry_run=dry_run)
