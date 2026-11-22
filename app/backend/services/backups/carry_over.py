"""What a restore keeps from the data it replaces (spec section 4, "Carry-over").

A rollback of the database must not undo what is final: a revoked certificate stays revoked, a certificate issued
after the backup comes back revoked (so a ca_issued one still goes on the CRL), the CRL number never goes backwards,
the sessions of the backup do not come back to life, and a move that waited in the backup does not run: the uploads it
waited for are gone with the sessions and clients of that moment, and a move nobody is watching any more must not
happen behind a restore. Runs inside the restore's transaction, before the swap.

outgoing / incoming are schema names (None: the default schema of the connection). The tables are reflected from
each schema, not taken from the models: a backup may be at an older migration, and only the columns a table has are
read and written. SQLAlchemy Core, so the same statements run on MariaDB and, in the tests, on SQLite with an
attached database.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

from app.backend.enums.general import MoveStatus, RevocationReason, SessionEndReason, SessionRevokeCause
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import CertificateAuthorityState, UserCertificate

CERTIFICATES = UserCertificate.__tablename__
CA_STATE = CertificateAuthorityState.__tablename__
SESSIONS = UserSession.__tablename__
USERS = User.__tablename__
POSTS = "posts"
MOVES = "pending_moves"
OUTBOX = "outbox_events"


class CarryOverUnsupported(Exception):
    """The backup has no table to carry a final state into (older than migration 024): nothing was changed"""

    def __init__(self, table: str):
        super().__init__(f"the backup has no {table} table")
        self.table = table


@dataclass(frozen=True)
class CarryOverResult:
    revocations: int
    certificates_added: int
    users_added: tuple[tuple[int, str], ...]     # (id, username as inserted): placeholders, archived
    crl_number: int
    sessions_ended: int
    moves_cancelled: int = 0
    events_removed: int = 0


def reflect_table(conn: sa.Connection, name: str, schema: Optional[str]) -> Optional[sa.Table]:
    """The table as the schema has it (a backup may be at an older migration), None when it has none. Public: the
    restore tool reflects audit_logs with it to write its own row (restore.py)."""
    if not sa.inspect(conn).has_table(name, schema=schema):
        return None
    return sa.Table(name, sa.MetaData(), schema=schema, autoload_with=conn)


def known_columns(table: sa.Table, values: dict) -> dict:
    """The values of the columns the table has (the others are left out: an older schema lacks them). Public, like
    reflect_table."""
    return {key: value for key, value in values.items() if key in table.c}


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


USERNAME_MAX = 255


def _name_key(name: str) -> str:
    """How MariaDB's unique index compares usernames (the column collation): case and trailing spaces ignored"""
    return name.casefold().rstrip()


def _free_name(name: str, user_id: int, taken: set) -> str:
    """name, or name~<id> (~<id>-2, ...) cut to the column width, the first one no other user holds"""
    if _name_key(name) not in taken:
        return name
    for n in range(1, 1000):
        suffix = f"~{user_id}" if n == 1 else f"~{user_id}-{n}"
        candidate = name[:USERNAME_MAX - len(suffix)] + suffix
        if _name_key(candidate) not in taken:
            return candidate
    raise ValueError(f"no free username for user {user_id}")


def _add_placeholders(conn: sa.Connection, outgoing: Optional[str], incoming: Optional[str],
                      user_ids: set[int]) -> tuple[tuple[int, str], ...]:
    """Insert the users of user_ids from the outgoing data as archived placeholders (spec 8.2): no roles, no post
    the incoming data lacks, a username taken in the incoming data suffixed with ~<id>"""
    if not user_ids:
        return ()
    out_users, in_users = reflect_table(conn, USERS, outgoing), reflect_table(conn, USERS, incoming)
    posts = reflect_table(conn, POSTS, incoming)
    post_ids = set() if posts is None else set(conn.execute(sa.select(posts.c.id)).scalars())
    taken = {_name_key(name) for name in conn.execute(sa.select(in_users.c.username)).scalars()}
    added = []
    for row in conn.execute(sa.select(out_users).where(out_users.c.id.in_(sorted(user_ids)))
                            .order_by(out_users.c.id)).mappings():
        values = known_columns(in_users, dict(row))
        values.update(known_columns(in_users, {"is_active": False, "is_deleted": True}))
        if values.get("post_id") is not None and values["post_id"] not in post_ids:
            values["post_id"] = None
        values["username"] = _free_name(values["username"], row["id"], taken)
        taken.add(_name_key(values["username"]))
        conn.execute(sa.insert(in_users).values(**values))
        added.append((row["id"], values["username"]))
    return tuple(added)


def carry_over(conn: sa.Connection, outgoing: Optional[str], incoming: Optional[str],
               now: datetime) -> CarryOverResult:
    stamp = _naive_utc(now)
    out_certs, in_certs = reflect_table(conn, CERTIFICATES, outgoing), reflect_table(conn, CERTIFICATES, incoming)
    outgoing_rows = [] if out_certs is None else [
        dict(row) for row in conn.execute(sa.select(out_certs).order_by(out_certs.c.id)).mappings()]
    if in_certs is None and any(row["revoked_at"] is not None or row.get("ca_issued") for row in outgoing_rows):
        raise CarryOverUnsupported(CERTIFICATES)
    revoke, missing, users_added = [], [], ()
    if in_certs is not None:
        in_users = reflect_table(conn, USERS, incoming)
        incoming_by_fp = {row["fingerprint_hash"]: row for row in conn.execute(sa.select(in_certs)).mappings()}
        incoming_ids = {row["id"] for row in incoming_by_fp.values()}
        user_ids = set(conn.execute(sa.select(in_users.c.id)).scalars())
        revoke = [row for row in outgoing_rows if row["revoked_at"] is not None
                  and row["fingerprint_hash"] in incoming_by_fp
                  and incoming_by_fp[row["fingerprint_hash"]]["revoked_at"] is None]
        missing = [row for row in outgoing_rows if row["fingerprint_hash"] not in incoming_by_fp]
        # users created after the backup come back archived, so their certificates can be carried (revoked)
        users_added = _add_placeholders(conn, outgoing, incoming,
                                        {row["user_id"] for row in missing} - user_ids)
        user_ids |= {user_id for user_id, _ in users_added}

    for row in revoke:
        conn.execute(sa.update(in_certs).where(in_certs.c.fingerprint_hash == row["fingerprint_hash"]).values(
            **known_columns(in_certs, {"revoked_at": row["revoked_at"], "revoked_by": row.get("revoked_by"),
                               "revocation_reason": row.get("revocation_reason")})))
    added = 0
    for row in missing:
        values = dict(row)
        if values["id"] in incoming_ids:
            del values["id"]
        if values["revoked_at"] is None:
            values.update(revoked_at=stamp, revoked_by=None,
                          revocation_reason=RevocationReason.CESSATION_OF_OPERATION.value)
        conn.execute(sa.insert(in_certs).values(**known_columns(in_certs, values)))
        added += 1

    # The CRL state of the newer number, with its dates: /health compares revocations with published_at
    out_ca, in_ca = reflect_table(conn, CA_STATE, outgoing), reflect_table(conn, CA_STATE, incoming)
    out_state = None if out_ca is None else conn.execute(
        sa.select(out_ca).order_by(out_ca.c.crl_number.desc())).mappings().first()
    in_number = None if in_ca is None else conn.execute(sa.select(sa.func.max(in_ca.c.crl_number))).scalar()
    out_number = out_state["crl_number"] if out_state else 0
    crl_number = max(out_number, in_number or 0)
    if in_number is not None and out_number > in_number:
        conn.execute(sa.update(in_ca).values(**known_columns(in_ca, {
            key: out_state[key] for key in ("crl_number", "this_update", "next_update", "published_at",
                                            "revoked_count") if key in out_state})))

    # Sessions of the backup end; a schema from before migration 015 has is_active instead of ended_at
    sessions = reflect_table(conn, SESSIONS, incoming)
    ended = 0
    if sessions is not None and "ended_at" in sessions.c:
        ended = conn.execute(sa.update(sessions).where(sessions.c.ended_at.is_(None)).values(**known_columns(sessions, {
            "ended_at": stamp, "end_reason": SessionEndReason.REVOKED.value,
            "revoke_cause": SessionRevokeCause.DATABASE_RESTORED.value}))).rowcount
    elif sessions is not None and "is_active" in sessions.c:
        ended = conn.execute(sa.update(sessions).where(sessions.c.is_active.is_(True)).values(is_active=False)).rowcount

    # A backup from before migration 035 has no such table
    moves = reflect_table(conn, MOVES, incoming)
    moves_cancelled = 0 if moves is None else conn.execute(
        sa.update(moves).where(moves.c.status == MoveStatus.WAITING.value).values(
            status=MoveStatus.CANCELLED.value, finished_at=stamp)).rowcount

    # Delivery data of the backup: it describes a past the restored rows contradict and must not replay. DELETE, not
    # TRUNCATE: TRUNCATE resets the counter before raise_statements raises it past the replaced data, so ids (and the
    # positions of clients) never go backwards. A backup from before migration 037 has no such table
    events = reflect_table(conn, OUTBOX, incoming)
    events_removed = 0 if events is None else conn.execute(sa.delete(events)).rowcount
    return CarryOverResult(revocations=len(revoke), certificates_added=added, users_added=users_added,
                           crl_number=crl_number, sessions_ended=ended, moves_cancelled=moves_cancelled,
                           events_removed=events_removed)


def user_state_differences(conn: sa.Connection, outgoing: Optional[str],
                           incoming: Optional[str]) -> list[tuple[int, str, str]]:
    """Users whose is_active / is_deleted differ (outgoing -> incoming): reported, never changed"""
    out_users, in_users = reflect_table(conn, USERS, outgoing), reflect_table(conn, USERS, incoming)
    rows = conn.execute(
        sa.select(out_users.c.id, out_users.c.username, out_users.c.is_active, out_users.c.is_deleted,
                  in_users.c.is_active.label("in_active"), in_users.c.is_deleted.label("in_deleted"))
        .join(in_users, in_users.c.id == out_users.c.id).order_by(out_users.c.id)).mappings()
    differences = []
    for row in rows:
        if bool(row["is_active"]) != bool(row["in_active"]):
            differences.append((row["id"], row["username"], f"is_active {bool(row['is_active'])}->{bool(row['in_active'])}"))
        if bool(row["is_deleted"]) != bool(row["in_deleted"]):
            differences.append((row["id"], row["username"], f"is_deleted {bool(row['is_deleted'])}->{bool(row['in_deleted'])}"))
    return differences
