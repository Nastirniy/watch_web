"""utils/row_locks.py: a write begins with its row lock, in a transaction of its own.

What the locks buy (concurrent writes of one row answering 404 / 409 instead of 500, error 1020 on MariaDB 11.6+, and
rules over several rows holding under concurrency) needs a real server and parallel requests: the live race check of
2026-10-19 against MariaDB 11.8. These tests pin what the helper itself promises.
"""
import pytest
from sqlmodel import select

from app.backend.models.observation import Tag
from app.backend.utils.row_locks import lock, lock_first, start_write


@pytest.mark.asyncio
async def test_lock_first_reads_the_row_as_committed(db, session_maker):
    """A copy loaded earlier in the request is replaced by the committed row: the checks after the lock see the
    result of the write that held it before"""
    tag = Tag(name="before")
    db.add(tag)
    await db.commit()
    stale = (await db.exec(select(Tag).where(Tag.id == tag.id))).one()

    async with session_maker() as other:
        row = (await other.exec(select(Tag).where(Tag.id == tag.id))).one()
        row.name = "after"
        other.add(row)
        await other.commit()

    locked = await lock_first(db, select(Tag).where(Tag.id == tag.id))
    assert locked is stale and locked.name == "after"
    assert await lock(db, select(Tag).where(Tag.id == -1)) is None


@pytest.mark.asyncio
async def test_lock_first_ends_the_read_transaction_of_the_request(db):
    await db.exec(select(Tag))                     # the reads of authentication open a transaction
    assert db.in_transaction()
    await start_write(db)
    assert not db.in_transaction(), "the lock must be the first statement of a new transaction"


@pytest.mark.asyncio
async def test_a_write_cannot_begin_after_a_change(db):
    """Committing there would commit half of a write: a route that changes something before its lock is a bug"""
    db.add(Tag(name="pending"))
    with pytest.raises(RuntimeError):
        await lock_first(db, select(Tag))
    await db.rollback()
    assert (await db.exec(select(Tag).where(Tag.name == "pending"))).first() is None


def test_a_shared_lock_is_lock_in_share_mode_on_mysql():
    """Observation create shares a lock on its post row: creates at one post run side by side, an archive of the post
    (or a cascade above it) waits for them, and a create after the archive reads the archived post (409)"""
    from sqlalchemy.dialects import mysql
    from sqlmodel import select
    from app.backend.models.post import Post
    from app.backend.utils.row_locks import locking

    shared = str(locking(select(Post).where(Post.id == 1), shared=True).compile(dialect=mysql.dialect()))
    exclusive = str(locking(select(Post).where(Post.id == 1)).compile(dialect=mysql.dialect()))
    assert shared.rstrip().endswith("LOCK IN SHARE MODE"), shared
    assert exclusive.rstrip().endswith("FOR UPDATE"), exclusive


async def test_observation_create_locks_its_post_first(client, db, headers, monkeypatch):
    from app.backend.api.v1 import V1, observations
    from app.backend.enums.routes import ObservationRoutes
    from tests.helpers import make_admin, make_dictionary, make_post, observation_payload

    calls = []
    real = observations.lock_first

    async def spy(session, query, shared=False):
        calls.append(shared)
        return await real(session, query, shared=shared)

    monkeypatch.setattr(observations, "lock_first", spy)
    h = headers(await make_admin(db))
    ids = await make_dictionary(db)
    post = await make_post(db)
    r = await client.post(V1.path(ObservationRoutes.CREATE), json=observation_payload(post.id, ids["signal_type_id"]), headers=h)
    assert r.status_code == 200, r.text
    assert calls == [True]
