"""What a restore keeps from the data it replaces (spec section 4, "Carry-over").

A rollback of the database must not undo what is final: a revoked certificate stays revoked, a certificate issued
after the backup comes back revoked (so a ca_issued one still goes on the CRL), the CRL number never goes backwards,
and the sessions of the backup do not come back to life. Runs inside the restore's transaction, before the swap.

outgoing / incoming are schema names (None: the default schema of the connection). SQLAlchemy Core on
schema-qualified copies of the model tables, so the same statements run on MariaDB and, in the tests, on SQLite with
an attached database.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

import sqlalchemy as sa

from app.backend.enums.general import RevocationReason, SessionEndReason, SessionRevokeCause
from app.backend.models.user import User, UserSession
from app.backend.models.user_certificate import CertificateAuthorityState, UserCertificate


class OrphanCertificates(Exception):
    """Certificates created after the backup whose user is missing from it: nothing was changed"""

    def __init__(self, serials: tuple[str, ...]):
        super().__init__(f"{len(serials)} certificate(s) of users missing from the backup")
        self.serials = serials


@dataclass(frozen=True)
class CarryOverResult:
    revocations: int
    certificates_added: int
    orphan_serials: tuple[str, ...]
    crl_number: int
    sessions_ended: int


def _table(model, schema: Optional[str]) -> sa.Table:
    return model.__table__.to_metadata(sa.MetaData(), schema=schema)


def _naive_utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _serial(row) -> str:
    return row["serial"] or f"#{row['id']}"


def carry_over(conn: sa.Connection, outgoing: Optional[str], incoming: Optional[str], now: datetime, *,
               skip_orphans: bool) -> CarryOverResult:
    stamp = _naive_utc(now)
    out_certs, in_certs = _table(UserCertificate, outgoing), _table(UserCertificate, incoming)
    in_users = _table(User, incoming)

    outgoing_rows = conn.execute(sa.select(out_certs).order_by(out_certs.c.id)).mappings().all()
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
            revoked_at=row["revoked_at"], revoked_by=row["revoked_by"], revocation_reason=row["revocation_reason"]))
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
        conn.execute(sa.insert(in_certs).values(**values))
        added += 1

    out_ca, in_ca = _table(CertificateAuthorityState, outgoing), _table(CertificateAuthorityState, incoming)
    out_number = conn.execute(sa.select(sa.func.max(out_ca.c.crl_number))).scalar() or 0
    in_number = conn.execute(sa.select(sa.func.max(in_ca.c.crl_number))).scalar()
    crl_number = max(out_number, in_number or 0)
    if in_number is not None and out_number > in_number:
        conn.execute(sa.update(in_ca).values(crl_number=out_number))

    sessions = _table(UserSession, incoming)
    ended = conn.execute(sa.update(sessions).where(sessions.c.ended_at.is_(None)).values(
        ended_at=stamp, end_reason=SessionEndReason.REVOKED.value,
        revoke_cause=SessionRevokeCause.DATABASE_RESTORED.value)).rowcount
    return CarryOverResult(revocations=len(revoke), certificates_added=added,
                           orphan_serials=orphans if skip_orphans else (), crl_number=crl_number,
                           sessions_ended=ended)


def user_state_differences(conn: sa.Connection, outgoing: Optional[str],
                           incoming: Optional[str]) -> list[tuple[int, str, str]]:
    """Users whose is_active / is_deleted differ (outgoing -> incoming): reported, never changed"""
    out_users, in_users = _table(User, outgoing), _table(User, incoming)
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
