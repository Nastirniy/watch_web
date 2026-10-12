from typing import Optional
from datetime import datetime, timezone
from sqlalchemy import Index, SmallInteger, UniqueConstraint, false
from sqlmodel import Field, SQLModel


class UserCertificate(SQLModel, table=True):
    """A client certificate a user logs in with (migration 024). A user may have several; each is revoked on its
    own and revoking is final (no archive, restore or delete). The state (CertificateState) is computed when the row
    is read (services/certificates.py), never stored.

    Login matches the three hashes (auth/certificates.py) of every row; legacy and configured rows have only them,
    issued and uploaded rows also the plain values of the certificate."""
    __tablename__ = "user_certificates"
    __table_args__ = (
        UniqueConstraint("fingerprint_hash", name="uq_user_certificates_fingerprint_hash"),
        # The CRL: every revoked certificate our client CA signed
        Index("ix_user_certificates_crl", "ca_issued", "revoked_at"),
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    source: str = Field(max_length=16)   # CertificateSource; no DB CHECK, a new source is a new enum value
    # The readable name (CN and .p12 friendly name); empty for legacy and configured rows
    label: str = Field(default="", max_length=64)
    # Plain values of the certificate; empty for legacy and configured rows, which have only the hashes
    subject_cn: Optional[str] = Field(default=None, max_length=255)
    serial: Optional[str] = Field(default=None, max_length=64)   # lower-case hex
    not_before: Optional[datetime] = Field(default=None)
    not_after: Optional[datetime] = Field(default=None)
    # What the login matches (auth/certificates.py); cn_hash_scheme is the rule that made cn_hash (1 = hash_data,
    # rows from before 019; 2 = hash_cn)
    fingerprint_hash: str = Field(max_length=64)
    cn_hash: str = Field(max_length=64)
    cn_hash_scheme: int = Field(sa_type=SmallInteger, sa_column_kwargs={"nullable": False})
    serial_hash: str = Field(max_length=64)
    # Signed by our client CA (issued, or an uploaded file that verifies against settings.pki.ca_cert_path): it goes
    # on our CRL when revoked
    ca_issued: bool = Field(default=False, sa_column_kwargs={"server_default": false(), "nullable": False})
    revoked_at: Optional[datetime] = Field(default=None)
    revoked_by: Optional[int] = Field(default=None, foreign_key="users.id")
    revocation_reason: Optional[str] = Field(default=None, max_length=32)   # RevocationReason
    # Directory of the issued package, relative to settings.pki.store_dir; only for issued rows
    files_dir: Optional[str] = Field(default=None, max_length=64)
    export_count: int = Field(default=0, sa_column_kwargs={"server_default": "0", "nullable": False})
    last_exported_at: Optional[datetime] = Field(default=None)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    # The user who registered it; null for the migration, the seed and the CLI
    created_by: Optional[int] = Field(default=None, foreign_key="users.id")


class CertificateAuthorityState(SQLModel, table=True):
    """One row (id 1, inserted by migration 024): the number and dates of the last CRL the API published"""
    __tablename__ = "certificate_authority_state"

    id: Optional[int] = Field(default=None, primary_key=True)
    crl_number: int = Field(default=0, sa_column_kwargs={"server_default": "0", "nullable": False})
    this_update: Optional[datetime] = Field(default=None)
    next_update: Optional[datetime] = Field(default=None)
    published_at: Optional[datetime] = Field(default=None)
    revoked_count: int = Field(default=0, sa_column_kwargs={"server_default": "0", "nullable": False})
