"""What a restore keeps from the data it replaces (spec section 4, "Carry-over").

A rollback of the database must not undo what is final: a revoked certificate stays revoked, a certificate issued
after the backup comes back revoked (so a ca_issued one still goes on the CRL), the CRL number never goes backwards,
and the sessions of the backup do not come back to life. Runs inside the restore's transaction, before the swap.

outgoing / incoming are schema names (None: the default schema of the connection). The tables are reflected from
each schema, not taken from the models: a backup may be at an older migration, and only the columns a table has are
read and written. SQLAlchemy Core, so the same statements run on MariaDB and, in the tests, on SQLite with an
attached database.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

from app.backend.enums.general import RevocationReason, SessionEndReason, SessionRevokeCause
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import CertificateAuthorityState, UserCertificate

CERTIFICATES = UserCertificate.__tablename__
CA_STATE = CertificateAuthorityState.__tablename__
SESSIONS = UserSession.__tablename__
USERS = User.__tablename__


class OrphanCertificates(Exception):
    """Certificates created after the backup whose user is missing from it: nothing was changed"""

    def __init__(self, serials: tuple[str, ...]):
        super().__init__(f"{len(serials)} certificate(s) of users missing from the backup")
        self.serials = serials


class CarryOverUnsupported(Exception):
    """The backup has no table to carry a final state into (older than migration 024): nothing was changed"""

    def __init__(self, table: str):
        super().__init__(f"the backup has no {table} table")
        self.table = table


@dataclass(frozen=True)
class CarryOverResult:
    revocations: int
    certificates_added: int
    orphan_serials: tuple[str, ...]
    crl_number: int
    sessions_ended: int


def _reflect(conn: sa.Connection, name: str, schema: Optional[str]) -> Optional[sa.Table]:
    """The table as the schema has it (a backup may be at an older migration), None when it has none"""
    if not sa.inspect(conn).has_table(name, schema=schema):
        return None
    return sa.Table(name, sa.MetaData(), schema=schema, autoload_with=conn)


def _only(table: sa.Table, values: dict) -> dict:
    """The values of the columns the table has"""
    return {key: value for key, value in values.items() if key in table.c}


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _serial(row) -> str:
    return row.get("serial") or f"#{row['id']}"


def carry_over(conn: sa.Connection, outgoing: Optional[str], incoming: Optional[str], now: datetime, *,
               skip_orphans: bool) -> CarryOverResult:
    stamp = _naive_utc(now)
    out_certs, in_certs = _reflect(conn, CERTIFICATES, outgoing), _reflect(conn, CERTIFICATES, incoming)
    outgoing_rows = [] if out_certs is None else [
        dict(row) for row in conn.execute(sa.select(out_certs).order_by(out_certs.c.id)).mappings()]
    if in_certs is None and any(row["revoked_at"] is not None or row.get("ca_issued") for row in outgoing_rows):
        raise CarryOverUnsupported(CERTIFICATES)
    revoke, missing, orphans = [], [], ()
    if in_certs is not None:
        in_users = _reflect(conn, USERS, incoming)
        incoming_by_fp = {row["fingerprint_hash"]: row for row in conn.execute(sa.select(in_certs)).mappings()}
        incoming_ids = {row["id"] for row in incoming_by_fp.values()}
        user_ids = set(conn.execute(sa.select(in_users.c.id)).scalars())
        revoke = [row for row in outgoing_rows if row["revoked_at"] is not None
                  and row["fingerprint_hash"] in incoming_by_fp
                  and incoming_by_fp[row["fingerprint_hash"]]["revoked_at"] is None]
        missing = [row for row in outgoing_rows if row["fingerprint_hash"] not in incoming_by_fp]
        orphans = tuple(_serial(row) for row in missing if row["user_id"] not in user_ids)
        if orphans and not skip_orphans:
            raise OrphanCertificates(orphans)

    for row in revoke:
        conn.execute(sa.update(in_certs).where(in_certs.c.fingerprint_hash == row["fingerprint_hash"]).values(
            **_only(in_certs, {"revoked_at": row["revoked_at"], "revoked_by": row.get("revoked_by"),
                               "revocation_reason": row.get("revocation_reason")})))
    added = 0
    for row in missing:
        if row["user_id"] not in user_ids:
            continue
        values = dict(row)
        if values["id"] in incoming_ids:
            del values["id"]
        if values["revoked_at"] is None:
            values.update(revoked_at=stamp, revoked_by=None,
                          revocation_reason=RevocationReason.CESSATION_OF_OPERATION.value)
        conn.execute(sa.insert(in_certs).values(**_only(in_certs, values)))
        added += 1

    # The CRL state of the newer number, with its dates: /health compares revocations with published_at
    out_ca, in_ca = _reflect(conn, CA_STATE, outgoing), _reflect(conn, CA_STATE, incoming)
    out_state = None if out_ca is None else conn.execute(
        sa.select(out_ca).order_by(out_ca.c.crl_number.desc())).mappings().first()
    in_number = None if in_ca is None else conn.execute(sa.select(sa.func.max(in_ca.c.crl_number))).scalar()
    out_number = out_state["crl_number"] if out_state else 0
    crl_number = max(out_number, in_number or 0)
    if in_number is not None and out_number > in_number:
        conn.execute(sa.update(in_ca).values(**_only(in_ca, {
            key: out_state[key] for key in ("crl_number", "this_update", "next_update", "published_at",
                                            "revoked_count") if key in out_state})))

    # Sessions of the backup end; a schema from before migration 015 has is_active instead of ended_at
    sessions = _reflect(conn, SESSIONS, incoming)
    ended = 0
    if sessions is not None and "ended_at" in sessions.c:
        ended = conn.execute(sa.update(sessions).where(sessions.c.ended_at.is_(None)).values(**_only(sessions, {
            "ended_at": stamp, "end_reason": SessionEndReason.REVOKED.value,
            "revoke_cause": SessionRevokeCause.DATABASE_RESTORED.value}))).rowcount
    elif sessions is not None and "is_active" in sessions.c:
        ended = conn.execute(sa.update(sessions).where(sessions.c.is_active.is_(True)).values(is_active=False)).rowcount
    return CarryOverResult(revocations=len(revoke), certificates_added=added,
                           orphan_serials=orphans if skip_orphans else (), crl_number=crl_number,
                           sessions_ended=ended)


def user_state_differences(conn: sa.Connection, outgoing: Optional[str],
                           incoming: Optional[str]) -> list[tuple[int, str, str]]:
    """Users whose is_active / is_deleted differ (outgoing -> incoming): reported, never changed"""
    out_users, in_users = _reflect(conn, USERS, outgoing), _reflect(conn, USERS, incoming)
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
