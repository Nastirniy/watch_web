from datetime import datetime
from decimal import Decimal
from typing import Optional, List
from sqlalchemy import (
    Boolean, DateTime, ForeignKey, Integer, String, Text, 
    func, JSON, LargeBinary, Date, DECIMAL, Index
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.dialects.mysql import LONGBLOB, MEDIUMTEXT


class User(DeclarativeBase):
    """
    User model for PKI certificate-based authentication system.
    Stores user credentials, PKI certificate information, and login tracking.
    """
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    pki_certificate_cn: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pki_certificate_serial: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    pki_certificate_fingerprint: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    username: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    username_alias: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    post_id: Mapped[Optional[int]] = mapped_column(ForeignKey("posts.id"))
    last_record_created_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    is_deleted: Mapped[Optional[bool]] = mapped_column(Boolean)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )

    __table_args__ = (
        Index('idx_users_pki_certificate_cn', 'pki_certificate_cn'),
        Index('idx_users_username', 'username'),
        Index('idx_users_last_login_at', 'last_login_at'),
        Index('idx_users_is_active', 'is_active'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    post: Mapped[Optional["Post"]] = relationship("Post", back_populates="users")
    sessions: Mapped[List["UserSession"]] = relationship("UserSession", back_populates="user", cascade="all, delete-orphan")
    observations: Mapped[List["Observation"]] = relationship("Observation", back_populates="user")
    export_jobs: Mapped[List["ExportJob"]] = relationship("ExportJob", back_populates="user", cascade="all, delete-orphan")
    audit_logs: Mapped[List["AuditLog"]] = relationship("AuditLog", back_populates="user")


class Role(User.registry.generate_base()):
    """
    Role-based access control model.
    Defines user roles with associated permissions and display information.
    """
    __tablename__ = "roles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    permissions: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )

    __table_args__ = (
        Index('idx_roles_name', 'name'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )


class Permission(User.registry.generate_base()):
    """
    Permission model for granular access control.
    Defines individual permissions that can be assigned to roles.
    """
    __tablename__ = "permissions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(MEDIUMTEXT, nullable=False)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )


class UserSession(User.registry.generate_base()):
    """
    User session management model.
    Tracks active user sessions with security and monitoring information.
    """
    __tablename__ = "user_sessions"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    last_activity: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index('idx_user_sessions_user_id', 'user_id'),
        Index('idx_user_sessions_expires_at', 'expires_at'),
        Index('idx_user_sessions_last_activity', 'last_activity'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="sessions")


class Post(User.registry.generate_base()):
    """
    Monitoring post model.
    Represents physical monitoring stations with connection status and metadata.
    """
    __tablename__ = "posts"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    description: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    region: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_connection_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    connection_status: Mapped[str] = mapped_column(String(20), server_default="offline")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )

    __table_args__ = (
        Index('idx_posts_code', 'code'),
        Index('idx_posts_is_active', 'is_active'),
        Index('idx_posts_connection_status', 'connection_status'),
        Index('idx_posts_last_connection_at', 'last_connection_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    users: Mapped[List["User"]] = relationship("User", back_populates="post")
    observations: Mapped[List["Observation"]] = relationship("Observation", back_populates="post")


class Category(User.registry.generate_base()):
    """
    Signal category classification model.
    Provides categorization system for different types of signals.
    """
    __tablename__ = "category"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    alias: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    # Relationships
    signal_types: Mapped[List["SignalType"]] = relationship("SignalType", back_populates="category_ref")


class SignalType(User.registry.generate_base()):
    """
    Signal type definition model.
    Defines various types of signals that can be observed and monitored.
    """
    __tablename__ = "signal_types"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    code: Mapped[str] = mapped_column(String(50), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[Optional[str]] = mapped_column(String(100), ForeignKey("category.name"))
    description: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    sort_order: Mapped[int] = mapped_column(Integer, server_default="0")
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )

    __table_args__ = (
        Index('idx_signal_types_code', 'code'),
        Index('idx_signal_types_category', 'category'),
        Index('idx_signal_types_is_active', 'is_active'),
        Index('idx_signal_types_sort_order', 'sort_order'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    category_ref: Mapped[Optional["Category"]] = relationship("Category", back_populates="signal_types")
    observations: Mapped[List["Observation"]] = relationship("Observation", back_populates="signal_type")


class ModulationType(User.registry.generate_base()):
    """
    Modulation type classification model.
    Defines different signal modulation types (AM, FM, Digital, etc.).
    """
    __tablename__ = "modulation_type"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)

    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    # Relationships
    observations: Mapped[List["Observation"]] = relationship("Observation", back_populates="modulation")


class Observation(User.registry.generate_base()):
    """
    Signal observation model.
    Core model for recording signal observations with frequency, timing, and metadata.
    """
    __tablename__ = "observations"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="RESTRICT"), nullable=False)
    post_id: Mapped[int] = mapped_column(ForeignKey("posts.id", ondelete="RESTRICT"), nullable=False)
    signal_type_id: Mapped[int] = mapped_column(ForeignKey("signal_types.id", ondelete="RESTRICT"), nullable=False)

    frequency_min: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False)
    frequency_max: Mapped[Decimal] = mapped_column(DECIMAL(12, 3), nullable=False)
    frequency_center: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(12, 3))
    bandwidth: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(12, 3))

    observed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    duration_seconds: Mapped[Optional[int]] = mapped_column(Integer)

    signal_strength: Mapped[Optional[Decimal]] = mapped_column(DECIMAL(8, 2))
    modulation_type: Mapped[Optional[int]] = mapped_column(ForeignKey("modulation_type.id"))

    comment: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)

    status: Mapped[str] = mapped_column(String(20), server_default="active")
    confidence_level: Mapped[str] = mapped_column(String(10), server_default="medium")
    is_deleted: Mapped[bool] = mapped_column(Boolean, server_default="0")

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp()
    )
    disappeared_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    disappeared_by: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    __table_args__ = (
        Index('idx_observations_user_id', 'user_id'),
        Index('idx_observations_post_id', 'post_id'),
        Index('idx_observations_signal_type_id', 'signal_type_id'),
        Index('idx_observations_frequency_range', 'frequency_min', 'frequency_max'),
        Index('idx_observations_frequency_center', 'frequency_center'),
        Index('idx_observations_observed_at', 'observed_at'),
        Index('idx_observations_status', 'status'),
        Index('idx_observations_created_at', 'created_at'),
        Index('idx_observations_disappeared_at', 'disappeared_at'),
        Index('idx_observations_post_observed_status', 'post_id', 'observed_at', 'status'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="observations")
    post: Mapped["Post"] = relationship("Post", back_populates="observations")
    signal_type: Mapped["SignalType"] = relationship("SignalType", back_populates="observations")
    modulation: Mapped[Optional["ModulationType"]] = relationship("ModulationType", back_populates="observations")
    media_files: Mapped[List["MediaFile"]] = relationship("MediaFile", back_populates="observation", cascade="all, delete-orphan")


class MediaFile(User.registry.generate_base()):
    """
    Media file storage model.
    Manages uploaded files (images, videos, audio, documents) associated with observations.
    """
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    observation_id: Mapped[int] = mapped_column(ForeignKey("observations.id", ondelete="CASCADE"), nullable=False)

    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_filename: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    file_path: Mapped[str] = mapped_column(String(500), nullable=False)
    file_size: Mapped[int] = mapped_column(Integer, nullable=False)
    mime_type: Mapped[str] = mapped_column(String(100), nullable=False)
    file_extension: Mapped[str] = mapped_column(String(10), nullable=False)

    media_type: Mapped[str] = mapped_column(String(10), nullable=False)
    file_hash: Mapped[str] = mapped_column(String(64), nullable=False)

    width: Mapped[Optional[int]] = mapped_column(Integer)
    height: Mapped[Optional[int]] = mapped_column(Integer)

    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index('idx_media_files_observation_id', 'observation_id'),
        Index('idx_media_files_media_type', 'media_type'),
        Index('idx_media_files_file_hash', 'file_hash'),
        Index('idx_media_files_uploaded_at', 'uploaded_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    observation: Mapped["Observation"] = relationship("Observation", back_populates="media_files")
    file_chunks: Mapped[List["FileChunk"]] = relationship("FileChunk", back_populates="media_file", cascade="all, delete-orphan")


class FileChunk(User.registry.generate_base()):
    """
    File chunk storage model.
    Stores large files in chunks for efficient upload and download handling.
    """
    __tablename__ = "file_chunks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    media_file_id: Mapped[int] = mapped_column(ForeignKey("media_files.id", ondelete="CASCADE"), nullable=False)
    chunk_number: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_size: Mapped[int] = mapped_column(Integer, nullable=False)
    chunk_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    chunk_data: Mapped[bytes] = mapped_column(LONGBLOB, nullable=False)
    uploaded_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index('idx_file_chunks_media_file_chunk', 'media_file_id', 'chunk_number', unique=True),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    media_file: Mapped["MediaFile"] = relationship("MediaFile", back_populates="file_chunks")


class ExportJob(User.registry.generate_base()):
    """
    Export job management model.
    Handles asynchronous export tasks (PDF reports, CSV exports, backups, statistics).
    """
    __tablename__ = "export_jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    job_type: Mapped[str] = mapped_column(String(20), nullable=False)

    date_from: Mapped[Optional[datetime]] = mapped_column(Date)
    date_to: Mapped[Optional[datetime]] = mapped_column(Date)
    post_ids: Mapped[Optional[str]] = mapped_column(JSON)
    signal_type_ids: Mapped[Optional[str]] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(String(15), server_default="pending")
    output_filename: Mapped[Optional[str]] = mapped_column(String(255))
    error_message: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)

    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())
    started_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    completed_at: Mapped[Optional[datetime]] = mapped_column(DateTime)
    expires_at: Mapped[Optional[datetime]] = mapped_column(DateTime)

    __table_args__ = (
        Index('idx_export_jobs_user_id', 'user_id'),
        Index('idx_export_jobs_status', 'status'),
        Index('idx_export_jobs_job_type', 'job_type'),
        Index('idx_export_jobs_created_at', 'created_at'),
        Index('idx_export_jobs_expires_at', 'expires_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    user: Mapped["User"] = relationship("User", back_populates="export_jobs")


class AuditLog(User.registry.generate_base()):
    """
    Audit logging model.
    Comprehensive audit trail for all system actions, changes, and security events.
    """
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[Optional[int]] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    session_id: Mapped[Optional[str]] = mapped_column(String(128))

    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(50), nullable=False)
    resource_id: Mapped[Optional[int]] = mapped_column(Integer)

    old_values: Mapped[Optional[str]] = mapped_column(JSON)
    new_values: Mapped[Optional[str]] = mapped_column(JSON)

    ip_address: Mapped[Optional[str]] = mapped_column(String(45))
    user_agent: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    request_url: Mapped[Optional[str]] = mapped_column(String(500))

    success: Mapped[bool] = mapped_column(Boolean, server_default="1")
    error_message: Mapped[Optional[str]] = mapped_column(MEDIUMTEXT)
    created_at: Mapped[datetime] = mapped_column(DateTime, server_default=func.current_timestamp())

    __table_args__ = (
        Index('idx_audit_logs_user_id', 'user_id'),
        Index('idx_audit_logs_action', 'action'),
        Index('idx_audit_logs_resource', 'resource_type', 'resource_id'),
        Index('idx_audit_logs_created_at', 'created_at'),
        Index('idx_audit_logs_session_id', 'session_id'),
        Index('idx_audit_logs_success', 'success'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    # Relationships
    user: Mapped[Optional["User"]] = relationship("User", back_populates="audit_logs")