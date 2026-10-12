"""Certificates move from users to user_certificates: one row per certificate, several per user.

Upgrade (spec section 4):
1. user_certificates and certificate_authority_state, each guarded with an inspector check (both are listed in
   TABLES_OWNED_BY_LATER_REVISIONS of 002_sync_schema), their indexes, and the state row id 1 (crl_number 0).
2. Every user's three hashes and pki_certificate_cn_hash_scheme become a legacy row in one INSERT ... SELECT: label
   empty, no plain values, ca_issued false, export_count 0, created_at = the user's created_at, created_by null. A user
   who has a legacy row already (a rerun) or holds the placeholder of an earlier downgrade (unassigned:<id>, matches
   no certificate) gets none.
3. The indexes of the four users.pki_certificate_* columns are dropped, then the columns. A fresh install (users
   built by 002 from the models) has only the scheme column, which 019 added.

Downgrade restores the columns (the scheme SMALLINT NOT NULL DEFAULT 1, as 019 adds it):
- a user with a legacy row gets its values back exactly (a revoked one too: the older code knows no revocation, the
  output names those users);
- the others get the hashes of their newest active row (not revoked, not expired);
- a user with none, or whose CN or serial hash a user restored before holds already (the columns are unique; legacy
  rows come first, then the other users by id), gets the placeholder unassigned:<user_id> in the three hash columns
  (scheme 1), which matches no certificate; the output names every such user;
then NOT NULL, the unique indexes, and both tables are dropped. Issued and uploaded certificates, revocations and
exports are lost; the output counts them.

Revision ID: 024_user_certificates
Revises: 023_authorization_v2
Create Date: 2026-10-13
"""
from datetime import datetime, timezone
from typing import Dict, List, Optional, Set, Tuple

from alembic import op
import sqlalchemy as sa

revision = "024_user_certificates"
down_revision = "023_authorization_v2"
branch_labels = None
depends_on = None

USERS = "users"
CERTIFICATES = "user_certificates"
STATE = "certificate_authority_state"
CN_HASH = "pki_certificate_cn_hash"
SCHEME = "pki_certificate_cn_hash_scheme"
SERIAL_HASH = "pki_certificate_serial_hash"
FINGERPRINT_HASH = "pki_certificate_fingerprint_hash"
HASH_COLUMNS = (CN_HASH, SERIAL_HASH, FINGERPRINT_HASH)
USER_COLUMNS = (CN_HASH, SCHEME, SERIAL_HASH, FINGERPRINT_HASH)
HASH_LENGTH = 255                     # of the users columns
# The unique indexes the downgrade creates: the name the models gave the first one, and the name 002_sync_schema
# gives a missing unique index (uq_<table>_<column>) for the others
USER_INDEXES = {
    CN_HASH: f"ix_{USERS}_{CN_HASH}",
    SERIAL_HASH: f"uq_{USERS}_{SERIAL_HASH}",
    FINGERPRINT_HASH: f"uq_{USERS}_{FINGERPRINT_HASH}",
}
FINGERPRINT_UNIQUE = "uq_user_certificates_fingerprint_hash"
CERTIFICATE_INDEXES = {
    "ix_user_certificates_user_id": ["user_id"],
    "ix_user_certificates_crl": ["ca_issued", "revoked_at"],
}
LEGACY = "legacy"                     # CertificateSource values (frozen here)
ISSUED = "issued"
PLACEHOLDER = "unassigned:"           # + user id: matches no certificate (no hash has this form)
PLACEHOLDER_SCHEME = 1                # the server default of the scheme column

USER_ROWS = sa.table(
    USERS, sa.column("id", sa.Integer), sa.column("username", sa.String), sa.column("created_at", sa.DateTime),
    sa.column(CN_HASH, sa.String), sa.column(SCHEME, sa.SmallInteger), sa.column(SERIAL_HASH, sa.String),
    sa.column(FINGERPRINT_HASH, sa.String),
)
CERTIFICATE_ROWS = sa.table(
    CERTIFICATES, sa.column("id", sa.Integer), sa.column("user_id", sa.Integer), sa.column("source", sa.String),
    sa.column("label", sa.String), sa.column("not_after", sa.DateTime), sa.column("fingerprint_hash", sa.String),
    sa.column("cn_hash", sa.String), sa.column("cn_hash_scheme", sa.SmallInteger), sa.column("serial_hash", sa.String),
    sa.column("ca_issued", sa.Boolean), sa.column("revoked_at", sa.DateTime), sa.column("export_count", sa.Integer),
    sa.column("created_at", sa.DateTime),
)
STATE_ROWS = sa.table(STATE, sa.column("id", sa.Integer), sa.column("crl_number", sa.Integer),
                      sa.column("revoked_count", sa.Integer))


def _log(message: str) -> None:
    print(f"[024_user_certificates] {message}")


def _tables() -> set:
    return set(sa.inspect(op.get_bind()).get_table_names())


def _user_columns() -> set:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(USERS)}


# ---------------------------------------------------------------------------------------------------- upgrade
def upgrade() -> None:
    tables = _tables()
    if USERS not in tables:
        return
    _create_tables(tables)
    columns = _user_columns()
    if set(HASH_COLUMNS) <= columns:
        _copy_legacy_rows(SCHEME in columns)
    _drop_user_columns(columns)


def _create_tables(tables: set) -> None:
    if CERTIFICATES not in tables:
        _log(f"creating {CERTIFICATES}")
        op.create_table(
            CERTIFICATES,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("user_id", sa.Integer(), sa.ForeignKey("users.id"), nullable=False),
            sa.Column("source", sa.String(length=16), nullable=False),
            sa.Column("label", sa.String(length=64), nullable=False),
            sa.Column("subject_cn", sa.String(length=255), nullable=True),
            sa.Column("serial", sa.String(length=64), nullable=True),
            sa.Column("not_before", sa.DateTime(), nullable=True),
            sa.Column("not_after", sa.DateTime(), nullable=True),
            sa.Column("fingerprint_hash", sa.String(length=64), nullable=False),
            sa.Column("cn_hash", sa.String(length=64), nullable=False),
            sa.Column("cn_hash_scheme", sa.SmallInteger(), nullable=False),
            sa.Column("serial_hash", sa.String(length=64), nullable=False),
            sa.Column("ca_issued", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("revoked_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.Column("revocation_reason", sa.String(length=32), nullable=True),
            sa.Column("files_dir", sa.String(length=64), nullable=True),
            sa.Column("export_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("last_exported_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False),
            sa.Column("created_by", sa.Integer(), sa.ForeignKey("users.id"), nullable=True),
            sa.UniqueConstraint("fingerprint_hash", name=FINGERPRINT_UNIQUE),
        )
    present = {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(CERTIFICATES)}
    for name, columns in CERTIFICATE_INDEXES.items():
        if name not in present:
            _log(f"creating index {name}")
            op.create_index(name, CERTIFICATES, columns)

    if STATE not in tables:
        _log(f"creating {STATE}")
        op.create_table(
            STATE,
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("crl_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("this_update", sa.DateTime(), nullable=True),
            sa.Column("next_update", sa.DateTime(), nullable=True),
            sa.Column("published_at", sa.DateTime(), nullable=True),
            sa.Column("revoked_count", sa.Integer(), nullable=False, server_default="0"),
        )
    bind = op.get_bind()
    if bind.execute(sa.select(STATE_ROWS.c.id).where(STATE_ROWS.c.id == 1)).first() is None:
        _log(f"inserting the {STATE} row")
        bind.execute(STATE_ROWS.insert().values(id=1, crl_number=0, revoked_count=0))


def _copy_legacy_rows(has_scheme: bool) -> None:
    bind = op.get_bind()
    users = USER_ROWS
    placeholder = users.c[FINGERPRINT_HASH].startswith(PLACEHOLDER)
    skipped = bind.execute(sa.select(sa.func.count()).select_from(users).where(placeholder)).scalar_one()
    if skipped:
        _log(f"{skipped} user(s) hold the placeholder of an earlier downgrade: no legacy row for them")
    # Users copied by an interrupted run already (MySQL commits the rows with the first ALTER). Read first: MySQL
    # does not let INSERT ... SELECT read its target table in a subquery
    copied = set(bind.execute(
        sa.select(CERTIFICATE_ROWS.c.user_id).where(CERTIFICATE_ROWS.c.source == LEGACY)).scalars())
    scheme = users.c[SCHEME] if has_scheme else sa.literal(PLACEHOLDER_SCHEME)
    rows = sa.select(
        users.c.id, sa.literal(LEGACY), sa.literal(""), users.c[FINGERPRINT_HASH], users.c[CN_HASH], scheme,
        users.c[SERIAL_HASH], sa.false(), sa.literal(0), users.c.created_at,
    ).where(users.c[FINGERPRINT_HASH].is_not(None), ~placeholder)
    if copied:
        rows = rows.where(users.c.id.not_in(sorted(copied)))
    target = ["user_id", "source", "label", "fingerprint_hash", "cn_hash", "cn_hash_scheme", "serial_hash",
              "ca_issued", "export_count", "created_at"]
    result = bind.execute(CERTIFICATE_ROWS.insert().from_select(target, rows))
    _log(f"copied the certificate of {result.rowcount} user(s) into {CERTIFICATES} as legacy rows")


def _drop_user_columns(columns: set) -> None:
    present = [name for name in USER_COLUMNS if name in columns]
    if not present:
        return
    indexes = [
        idx["name"] for idx in sa.inspect(op.get_bind()).get_indexes(USERS)
        if idx.get("name") and set(idx.get("column_names") or []) & set(USER_COLUMNS)
    ]
    with op.batch_alter_table(USERS) as batch:
        for name in indexes:
            _log(f"dropping index {name}")
            batch.drop_index(name)
        for name in present:
            _log(f"dropping column {USERS}.{name}")
            batch.drop_column(name)


# ---------------------------------------------------------------------------------------------------- downgrade
def downgrade() -> None:
    tables = _tables()
    if USERS not in tables:
        return
    columns = _user_columns()
    missing = [name for name in USER_COLUMNS if name not in columns]
    if missing:
        with op.batch_alter_table(USERS) as batch:
            for name in missing:
                _log(f"adding column {USERS}.{name}")
                if name == SCHEME:
                    batch.add_column(sa.Column(name, sa.SmallInteger(), nullable=False, server_default="1"))
                else:
                    batch.add_column(sa.Column(name, sa.String(length=HASH_LENGTH), nullable=True))

    _restore_hashes(CERTIFICATES in tables)

    with op.batch_alter_table(USERS) as batch:
        for name in HASH_COLUMNS:
            batch.alter_column(name, existing_type=sa.String(length=HASH_LENGTH), nullable=False)
    covered = _unique_column_sets()
    for column, name in USER_INDEXES.items():
        if frozenset({column}) not in covered:
            _log(f"creating unique index {name}")
            op.create_index(name, USERS, [column], unique=True)

    for table in (CERTIFICATES, STATE):
        if table in tables:
            _log(f"dropping {table}")
            op.drop_table(table)


def _unique_column_sets() -> Set[frozenset]:
    inspector = sa.inspect(op.get_bind())
    found = {frozenset(c for c in uc.get("column_names", []) if c) for uc in inspector.get_unique_constraints(USERS)}
    found |= {frozenset(c for c in idx.get("column_names", []) if c)
              for idx in inspector.get_indexes(USERS) if idx.get("unique")}
    return found


def _naive(value: Optional[datetime]) -> Optional[datetime]:
    if value is None or value.tzinfo is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _restore_hashes(has_certificates: bool) -> None:
    bind = op.get_bind()
    users = bind.execute(sa.select(USER_ROWS.c.id, USER_ROWS.c.username, USER_ROWS.c[FINGERPRINT_HASH])
                         .order_by(USER_ROWS.c.id)).all()
    names = {user.id: user.username for user in users}
    if not has_certificates:
        # An interrupted downgrade dropped the table already: the users it restored keep their values
        for user in users:
            if user[2] is None:
                _restore(user.id, None, names, "no certificate table")
        return

    rows = bind.execute(sa.select(CERTIFICATE_ROWS).order_by(CERTIFICATE_ROWS.c.id)).all()
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    legacy: Dict[int, sa.Row] = {}
    newest: Dict[int, sa.Row] = {}
    for row in rows:
        if row.source == LEGACY:
            legacy.setdefault(row.user_id, row)
        not_after = _naive(row.not_after)
        if row.revoked_at is None and (not_after is None or not_after >= now):
            best = newest.get(row.user_id)
            if best is None or (_naive(row.created_at), row.id) > (_naive(best.created_at), best.id):
                newest[row.user_id] = row

    # Legacy rows first (their values come back exactly), then the newest active row of every other user by id
    candidates: List[Tuple[int, int, Optional[sa.Row]]] = [(0, user_id, row) for user_id, row in legacy.items()]
    candidates += [(1, user.id, newest.get(user.id)) for user in users if user.id not in legacy]
    taken_cn: Set[str] = set()
    taken_serial: Set[str] = set()
    for _, user_id, row in sorted(candidates, key=lambda c: (c[0], c[1])):
        if row is None:
            _restore(user_id, None, names, "no active certificate")
        elif row.cn_hash in taken_cn:
            _restore(user_id, None, names, "the CN hash of its certificate is another user's")
        elif row.serial_hash in taken_serial:
            _restore(user_id, None, names, "the serial hash of its certificate is another user's")
        else:
            taken_cn.add(row.cn_hash)
            taken_serial.add(row.serial_hash)
            _restore(user_id, row, names)
            if row.source == LEGACY and row.revoked_at is not None:
                _log(f"user {user_id} ({names.get(user_id)}): its legacy certificate is revoked, the older code knows "
                     f"no revocation and lets it log in again")

    issued = sum(1 for row in rows if row.source == ISSUED)
    revoked = sum(1 for row in rows if row.revoked_at is not None)
    exports = sum(row.export_count or 0 for row in rows)
    _log(f"dropping {CERTIFICATES}: {len(rows)} certificate row(s), {issued} of them issued, {revoked} revoked, "
         f"{exports} export(s) recorded; only the hashes restored above are kept")


def _restore(user_id: int, row: Optional[sa.Row], names: Dict[int, str], reason: str = "") -> None:
    if row is None:
        value = f"{PLACEHOLDER}{user_id}"
        values = {CN_HASH: value, SCHEME: PLACEHOLDER_SCHEME, SERIAL_HASH: value, FINGERPRINT_HASH: value}
        _log(f"user {user_id} ({names.get(user_id)}): {reason}; placeholder {value}, which matches no certificate "
             f"(give the user a certificate with the older code)")
    else:
        values = {CN_HASH: row.cn_hash, SCHEME: row.cn_hash_scheme, SERIAL_HASH: row.serial_hash,
                  FINGERPRINT_HASH: row.fingerprint_hash}
    op.get_bind().execute(sa.update(USER_ROWS).where(USER_ROWS.c.id == user_id).values(**values))
