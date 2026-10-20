"""One-time conversion of old observations into the unit style (observation_units rows for history).

The rule: every observation (archived ones included) of a post (archived ones included) that is in a corps or a
brigade now, and that has no observation_units row at all, gets one row for every unit the post is in now,
created_at = the observation's. An observation that has any row (corps only, brigade only, or both) is recorded
history and is never given another one, so a second run fills nothing and no pair of units that never existed is
written.

Concurrency (MariaDB with innodb_snapshot_isolation; the API stays live): a plain pre-check per post (is there an
observation without a row, is the post in a unit) ends before any lock, so a post with nothing to fill is never
locked. Then one transaction per chunk of observations of one post, begun with lock_first on the post row (two runs
serialize per post; a move or a create waits for the chunk), plain reads of the units and of the next observations
without a row, then one INSERT of the rows (no INSERT ... SELECT: it would lock observation rows and fail with
error 1020 when one is edited meanwhile). The post lock is the only lock held, so it cannot form a cycle with post
moves or unit writes.
"""
from collections import Counter
from dataclasses import dataclass
from typing import Dict, List

from sqlalchemy import exists, insert
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
    posts_skipped: int           # posts (archived included) in no unit now (a post's observations are not looked at)
    dry_run: bool


def _without_unit(post_id: int, after_id: int):
    """The observations of the post after after_id that have no observation_units row at all (id, created_at)"""
    return (select(Observation.id, Observation.created_at)
            .where(Observation.post_id == post_id, Observation.id > after_id,
                   ~exists().where(ObservationUnit.observation_id == Observation.id))
            .order_by(Observation.id))


async def fill_unit_history(session: AsyncSession, dry_run: bool = False, chunk_size: int = CHUNK_SIZE) -> HistoryFill:
    """Fill the missing observation_units rows of old observations (see the module). Commits per chunk; a database
    error stops the run and the chunks done stay (run it again to continue). dry_run reads the same way, writes and
    locks nothing."""
    post_ids = list((await session.exec(select(Post.id).order_by(Post.id))).all())
    await session.commit()

    per_unit: Counter = Counter()      # unit group id -> observations that got it
    filled = 0                         # observations that got rows (each is handled once per run)
    rows_total = 0
    posts_skipped = 0

    for post_id in post_ids:
        # plain pre-check, no lock: skips a post in no unit and a post with nothing to fill; the read ends here
        post_row = (await session.exec(select(Post.id, Post.group_id).where(Post.id == post_id))).first()
        if post_row is None:                                   # deleted meanwhile
            await session.commit()
            continue
        if not await current_units_of_group(session, post_row[1]):
            posts_skipped += 1
            await session.commit()
            continue
        has_work = (await session.exec(_without_unit(post_id, 0).limit(1))).first() is not None
        await session.commit()
        if not has_work:
            continue

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
            if not units:                                      # moved out of its units meanwhile
                posts_skipped += 1
                await session.commit()
                break
            chunk = (await session.exec(_without_unit(post_id, after_id).limit(chunk_size))).all()
            if not chunk:
                await session.commit()
                break
            new_rows = [{"group_id": unit_id, "created_at": created_at, "observation_id": observation_id}
                        for observation_id, created_at in chunk for unit_id in units.values()]
            if new_rows and not dry_run:
                await session.execute(insert(ObservationUnit), new_rows)
            for unit_id in units.values():
                per_unit[unit_id] += len(chunk)
            filled += len(chunk)
            rows_total += len(new_rows)
            if dry_run:
                await session.rollback()
            else:
                await session.commit()
            if len(chunk) < chunk_size:                        # the last chunk of the post
                break
            after_id = chunk[-1][0]

    if rows_total and not dry_run:
        await create_audit_log(
            session, action=AUDIT_ACTION, resource_type=AUDIT_RESOURCE,
            new_values={"units": {str(unit_id): count for unit_id, count in per_unit.items()},
                        "observations": filled, "rows": rows_total},
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
    return HistoryFill(units=units_filled, observations=filled, rows=rows_total,
                       posts_skipped=posts_skipped, dry_run=dry_run)
