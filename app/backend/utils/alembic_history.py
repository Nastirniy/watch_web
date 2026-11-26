"""The alembic history of this code: which revisions it knows, its head, and their order.

Used by the start check (bootstrap/schema_range.py), cli/schema_check.py, the restore tool (a backup at a revision
this code does not know is refused) and GET /database/status. Reads only the migration scripts, never the database.
"""
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Mapping, Optional

ALEMBIC_INI = Path(__file__).resolve().parents[3] / "alembic.ini"


@dataclass(frozen=True)
class History:
    """Revision -> its parents (down_revision), the head, and the baseline of the schema contract (None: the
    contract's own, bootstrap/schema_range.BASELINE)"""
    parents: Mapping[str, tuple[str, ...]]
    head: str
    baseline: Optional[str] = field(default=None)

    def knows(self, revision: Optional[str]) -> bool:
        return revision is not None and revision in self.parents

    def is_at_or_before(self, earlier: str, later: str) -> bool:
        """earlier is later itself or one of its ancestors; False when either is unknown"""
        if not self.knows(earlier) or not self.knows(later):
            return False
        pending, seen = [later], set()
        while pending:
            revision = pending.pop()
            if revision == earlier:
                return True
            if revision not in seen:
                seen.add(revision)
                pending.extend(self.parents.get(revision, ()))
        return False


@lru_cache(maxsize=1)
def _script():
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    return ScriptDirectory.from_config(Config(str(ALEMBIC_INI)))


def _parents(down_revision) -> tuple[str, ...]:
    if down_revision is None:
        return ()
    if isinstance(down_revision, str):
        return (down_revision,)
    return tuple(down_revision)


@lru_cache(maxsize=1)
def history() -> History:
    script = _script()
    parents = {revision.revision: _parents(revision.down_revision) for revision in script.walk_revisions()}
    return History(parents=parents, head=script.get_current_head())


def known_revisions() -> set[str]:
    return set(history().parents)


def code_head() -> str:
    return history().head
