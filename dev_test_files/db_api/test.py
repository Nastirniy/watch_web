from datetime import datetime, timedelta
from decimal import Decimal
from typing import Optional, List, Dict, Any, Union
from enum import Enum
import hashlib
import uuid
import json

from fastapi import FastAPI, Depends, HTTPException, status, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy import create_engine, select, and_, or_, func, Index
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.exc import IntegrityError
from sqlalchemy.dialects.mysql import LONGBLOB, MEDIUMTEXT
from sqlmodel import SQLModel, Field, Relationship, Column, String, DateTime, Boolean, Integer, DECIMAL, JSON, \
    LargeBinary, Date

# Database setup
DATABASE_URL = "mysql+pymysql://user:password@localhost/dbname"
engine = create_engine(DATABASE_URL, echo=True)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

# Security
security = HTTPBearer()


# Enums
class ConnectionStatus(str, Enum):
    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    ERROR = "error"


class ObservationStatus(str, Enum):
    ACTIVE = "active"
    DISAPPEARED = "disappeared"
    CONFIRMED = "confirmed"
    FALSE_POSITIVE = "false_positive"
    UNDER_REVIEW = "under_review"


class ConfidenceLevel(str, Enum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class MediaType(str, Enum):
    IMAGE = "image"
    VIDEO = "video"
    AUDIO = "audio"
    DOCUMENT = "document"


class JobType(str, Enum):
    PDF_REPORT = "pdf_report"
    CSV_EXPORT = "csv_export"
    DATABASE_BACKUP = "database_backup"
    STATISTICS_REPORT = "statistics_report"


class JobStatus(str, Enum):
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


# SQLModel Models (Database + API schemas combined)
class UserBase(SQLModel):
    pki_certificate_cn: str = Field(max_length=255, unique=True, index=True)
    pki_certificate_serial: str = Field(max_length=255, unique=True)
    pki_certificate_fingerprint: str = Field(max_length=255, unique=True)
    username: str = Field(max_length=255, unique=True, index=True)
    username_alias: str = Field(max_length=255)
    is_active: bool = Field(default=True, index=True)
    post_id: Optional[int] = Field(default=None, foreign_key="post.id")


class User(UserBase, table=True):
    __tablename__ = "users"
    __table_args__ = (
        Index('idx_users_pki_certificate_cn', 'pki_certificate_cn'),
        Index('idx_users_username', 'username'),
        Index('idx_users_last_login_at', 'last_login_at'),
        Index('idx_users_is_active', 'is_active'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    last_login_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, index=True)
    )
    last_record_created_at: Optional[datetime] = Field(default=None)
    is_deleted: Optional[bool] = Field(default=None)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp())
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
    )

    # Relationships
    post: Optional["Post"] = Relationship(back_populates="users")
    sessions: List["UserSession"] = Relationship(back_populates="user", cascade_delete=True)
    observations: List["Observation"] = Relationship(back_populates="user")
    export_jobs: List["ExportJob"] = Relationship(back_populates="user", cascade_delete=True)
    audit_logs: List["AuditLog"] = Relationship(back_populates="user")


class UserCreate(UserBase):
    pass


class UserUpdate(SQLModel):
    username_alias: Optional[str] = Field(None, max_length=255)
    is_active: Optional[bool] = None
    post_id: Optional[int] = None


class UserRead(UserBase):
    id: int
    last_login_at: Optional[datetime] = None
    last_record_created_at: Optional[datetime] = None
    is_deleted: Optional[bool] = None
    created_at: datetime
    updated_at: datetime


class PostBase(SQLModel):
    name: str = Field(max_length=255)
    code: str = Field(max_length=50, unique=True, index=True)
    description: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    region: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    is_active: bool = Field(default=True, index=True)
    connection_status: ConnectionStatus = Field(default=ConnectionStatus.OFFLINE, index=True)


class Post(PostBase, table=True):
    __tablename__ = "posts"
    __table_args__ = (
        Index('idx_posts_code', 'code'),
        Index('idx_posts_is_active', 'is_active'),
        Index('idx_posts_connection_status', 'connection_status'),
        Index('idx_posts_last_connection_at', 'last_connection_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    last_connection_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, index=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp())
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
    )

    # Relationships
    users: List["User"] = Relationship(back_populates="post")
    observations: List["Observation"] = Relationship(back_populates="post")


class PostCreate(PostBase):
    pass


class PostUpdate(SQLModel):
    name: Optional[str] = Field(None, max_length=255)
    description: Optional[str] = None
    region: Optional[str] = None
    is_active: Optional[bool] = None
    connection_status: Optional[ConnectionStatus] = None


class PostRead(PostBase):
    id: int
    last_connection_at: Optional[datetime] = None
    created_at: datetime
    updated_at: datetime


class CategoryBase(SQLModel):
    name: str = Field(max_length=255)
    alias: str = Field(max_length=255)


class Category(CategoryBase, table=True):
    __tablename__ = "category"
    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Relationships
    signal_types: List["SignalType"] = Relationship(back_populates="category_ref")


class SignalTypeBase(SQLModel):
    code: str = Field(max_length=50, unique=True, index=True)
    name: str = Field(max_length=255)
    category: Optional[str] = Field(default=None, max_length=100, foreign_key="category.name", index=True)
    description: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    is_active: bool = Field(default=True, index=True)
    sort_order: int = Field(default=0, index=True)


class SignalType(SignalTypeBase, table=True):
    __tablename__ = "signal_types"
    __table_args__ = (
        Index('idx_signal_types_code', 'code'),
        Index('idx_signal_types_category', 'category'),
        Index('idx_signal_types_is_active', 'is_active'),
        Index('idx_signal_types_sort_order', 'sort_order'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp())
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
    )

    # Relationships
    category_ref: Optional["Category"] = Relationship(back_populates="signal_types")
    observations: List["Observation"] = Relationship(back_populates="signal_type")


class SignalTypeCreate(SignalTypeBase):
    pass


class SignalTypeUpdate(SQLModel):
    name: Optional[str] = Field(None, max_length=255)
    category: Optional[str] = Field(None, max_length=100)
    description: Optional[str] = None
    is_active: Optional[bool] = None
    sort_order: Optional[int] = None


class SignalTypeRead(SignalTypeBase):
    id: int
    created_at: datetime
    updated_at: datetime


class ModulationTypeBase(SQLModel):
    name: str = Field(max_length=255)


class ModulationType(ModulationTypeBase, table=True):
    __tablename__ = "modulation_type"
    __table_args__ = (
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'},
    )

    id: Optional[int] = Field(default=None, primary_key=True)

    # Relationships
    observations: List["Observation"] = Relationship(back_populates="modulation")


class UserSessionBase(SQLModel):
    user_id: int = Field(foreign_key="users.id", index=True)
    ip_address: Optional[str] = Field(default=None, max_length=45)
    user_agent: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    expires_at: datetime = Field(index=True)


class UserSession(UserSessionBase, table=True):
    __tablename__ = "user_sessions"
    __table_args__ = (
        Index('idx_user_sessions_user_id', 'user_id'),
        Index('idx_user_sessions_expires_at', 'expires_at'),
        Index('idx_user_sessions_last_activity', 'last_activity'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: str = Field(primary_key=True, max_length=128)
    last_activity: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp(),
                         index=True)
    )
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp())
    )

    # Relationships
    user: "User" = Relationship(back_populates="sessions")


class ObservationBase(SQLModel):
    post_id: int = Field(foreign_key="posts.id", index=True)
    signal_type_id: int = Field(foreign_key="signal_types.id", index=True)
    frequency_min: Decimal = Field(sa_column=Column(DECIMAL(12, 3), nullable=False, index=True))
    frequency_max: Decimal = Field(sa_column=Column(DECIMAL(12, 3), nullable=False, index=True))
    frequency_center: Optional[Decimal] = Field(default=None, sa_column=Column(DECIMAL(12, 3), index=True))
    bandwidth: Optional[Decimal] = Field(default=None, sa_column=Column(DECIMAL(12, 3)))
    observed_at: datetime = Field(index=True)
    duration_seconds: Optional[int] = None
    signal_strength: Optional[Decimal] = Field(default=None, sa_column=Column(DECIMAL(8, 2)))
    modulation_type: Optional[int] = Field(default=None, foreign_key="modulation_type.id")
    comment: Optional[str] = Field(default=None, max_length=1000, sa_column=Column(MEDIUMTEXT))
    status: ObservationStatus = Field(default=ObservationStatus.ACTIVE, index=True)
    confidence_level: ConfidenceLevel = Field(default=ConfidenceLevel.MEDIUM)


class Observation(ObservationBase, table=True):
    __tablename__ = "observations"
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

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    is_deleted: bool = Field(default=False)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), index=True)
    )
    updated_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), onupdate=func.current_timestamp())
    )
    disappeared_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, index=True)
    )
    disappeared_by: Optional[int] = Field(default=None, foreign_key="users.id")

    # Relationships
    user: "User" = Relationship(back_populates="observations")
    post: "Post" = Relationship(back_populates="observations")
    signal_type: "SignalType" = Relationship(back_populates="observations")
    modulation: Optional["ModulationType"] = Relationship(back_populates="observations")
    media_files: List["MediaFile"] = Relationship(back_populates="observation", cascade_delete=True)


class ObservationCreate(ObservationBase):
    pass


class ObservationUpdate(SQLModel):
    frequency_min: Optional[Decimal] = None
    frequency_max: Optional[Decimal] = None
    frequency_center: Optional[Decimal] = None
    bandwidth: Optional[Decimal] = None
    duration_seconds: Optional[int] = None
    signal_strength: Optional[Decimal] = None
    modulation_type: Optional[int] = None
    comment: Optional[str] = Field(None, max_length=1000)
    status: Optional[ObservationStatus] = None
    confidence_level: Optional[ConfidenceLevel] = None


class ObservationRead(ObservationBase):
    id: int
    user_id: int
    is_deleted: bool
    created_at: datetime
    updated_at: datetime
    disappeared_at: Optional[datetime] = None
    disappeared_by: Optional[int] = None


class MediaFileBase(SQLModel):
    observation_id: int = Field(foreign_key="observations.id", index=True)
    original_filename: str = Field(max_length=255)
    media_type: MediaType = Field(index=True)
    file_size: int
    mime_type: str = Field(max_length=100)


class MediaFile(MediaFileBase, table=True):
    __tablename__ = "media_files"
    __table_args__ = (
        Index('idx_media_files_observation_id', 'observation_id'),
        Index('idx_media_files_media_type', 'media_type'),
        Index('idx_media_files_file_hash', 'file_hash'),
        Index('idx_media_files_uploaded_at', 'uploaded_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    stored_filename: str = Field(max_length=255, unique=True)
    file_path: str = Field(max_length=500)
    file_extension: str = Field(max_length=10)
    file_hash: str = Field(max_length=64, index=True)
    width: Optional[int] = None
    height: Optional[int] = None
    uploaded_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), index=True)
    )

    # Relationships
    observation: "Observation" = Relationship(back_populates="media_files")
    file_chunks: List["FileChunk"] = Relationship(back_populates="media_file", cascade_delete=True)


class MediaFileCreate(MediaFileBase):
    pass


class MediaFileRead(MediaFileBase):
    id: int
    stored_filename: str
    file_path: str
    file_extension: str
    file_hash: str
    width: Optional[int] = None
    height: Optional[int] = None
    uploaded_at: datetime


class FileChunkBase(SQLModel):
    media_file_id: int = Field(foreign_key="media_files.id", index=True)
    chunk_number: int
    chunk_size: int
    chunk_hash: str = Field(max_length=64)


class FileChunk(FileChunkBase, table=True):
    __tablename__ = "file_chunks"
    __table_args__ = (
        Index('idx_file_chunks_media_file_chunk', 'media_file_id', 'chunk_number', unique=True),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    chunk_data: bytes = Field(sa_column=Column(LONGBLOB, nullable=False))
    uploaded_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp())
    )

    # Relationships
    media_file: "MediaFile" = Relationship(back_populates="file_chunks")


class ExportJobBase(SQLModel):
    job_type: JobType = Field(index=True)
    date_from: Optional[datetime] = Field(default=None, sa_column=Column(Date))
    date_to: Optional[datetime] = Field(default=None, sa_column=Column(Date))
    post_ids: Optional[List[int]] = Field(default=None, sa_column=Column(JSON))
    signal_type_ids: Optional[List[int]] = Field(default=None, sa_column=Column(JSON))


class ExportJob(ExportJobBase, table=True):
    __tablename__ = "export_jobs"
    __table_args__ = (
        Index('idx_export_jobs_user_id', 'user_id'),
        Index('idx_export_jobs_status', 'status'),
        Index('idx_export_jobs_job_type', 'job_type'),
        Index('idx_export_jobs_created_at', 'created_at'),
        Index('idx_export_jobs_expires_at', 'expires_at'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    status: JobStatus = Field(default=JobStatus.PENDING, index=True)
    output_filename: Optional[str] = Field(default=None, max_length=255)
    error_message: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), index=True)
    )
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = Field(
        default=None,
        sa_column=Column(DateTime, index=True)
    )

    # Relationships
    user: "User" = Relationship(back_populates="export_jobs")


class ExportJobCreate(ExportJobBase):
    pass


class ExportJobRead(ExportJobBase):
    id: int
    user_id: int
    status: JobStatus
    output_filename: Optional[str] = None
    error_message: Optional[str] = None
    created_at: datetime
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None
    expires_at: Optional[datetime] = None


class AuditLogBase(SQLModel):
    action: str = Field(max_length=100, index=True)
    resource_type: str = Field(max_length=50, index=True)
    resource_id: Optional[int] = Field(default=None, index=True)
    old_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    new_values: Optional[Dict[str, Any]] = Field(default=None, sa_column=Column(JSON))
    ip_address: Optional[str] = Field(default=None, max_length=45)
    user_agent: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))
    request_url: Optional[str] = Field(default=None, max_length=500)
    success: bool = Field(default=True, index=True)
    error_message: Optional[str] = Field(default=None, sa_column=Column(MEDIUMTEXT))


class AuditLog(AuditLogBase, table=True):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index('idx_audit_logs_user_id', 'user_id'),
        Index('idx_audit_logs_action', 'action'),
        Index('idx_audit_logs_resource', 'resource_type', 'resource_id'),
        Index('idx_audit_logs_created_at', 'created_at'),
        Index('idx_audit_logs_session_id', 'session_id'),
        Index('idx_audit_logs_success', 'success'),
        {'mysql_engine': 'InnoDB', 'mysql_charset': 'utf8mb4', 'mysql_collate': 'utf8mb4_unicode_ci'}
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: Optional[int] = Field(default=None, foreign_key="users.id", index=True)
    session_id: Optional[str] = Field(default=None, max_length=128, index=True)
    created_at: datetime = Field(
        default_factory=datetime.utcnow,
        sa_column=Column(DateTime, server_default=func.current_timestamp(), index=True)
    )

    # Relationships
    user: Optional["User"] = Relationship(back_populates="audit_logs")


# Dependency to get database session
def get_db() -> Session:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


# Authentication and Authorization
class AuthService:
    @staticmethod
    def verify_session_token(token: str, db: Session) -> Optional[User]:
        """Verify session token and return user if valid"""
        stmt = select(UserSession).where(
            and_(
                UserSession.id == token,
                UserSession.expires_at > datetime.utcnow()
            )
        )
        session = db.exec(stmt).first()

        if not session:
            return None

        # Update last activity
        session.last_activity = datetime.utcnow()
        db.add(session)
        db.commit()

        # Get user
        user_stmt = select(User).where(User.id == session.user_id)
        return db.exec(user_stmt).first()

    @staticmethod
    def has_permission(user: User, action: str, resource: str) -> bool:
        """Check if user has permission for action on resource"""
        if not user.is_active:
            return False
        # Add your permission logic based on roles and permissions
        return True

    @staticmethod
    def create_session(user: User, ip_address: str, user_agent: str, db: Session) -> str:
        """Create new user session"""
        session_id = str(uuid.uuid4())
        expires_at = datetime.utcnow() + timedelta(hours=24)

        session = UserSession(
            id=session_id,
            user_id=user.id,
            ip_address=ip_address,
            user_agent=user_agent,
            expires_at=expires_at
        )

        db.add(session)
        db.commit()

        # Update user last login
        user.last_login_at = datetime.utcnow()
        db.add(user)
        db.commit()

        return session_id


# Dependency to get current user
async def get_current_user(
        credentials: HTTPAuthorizationCredentials = Depends(security),
        db: Session = Depends(get_db)
) -> User:
    """Get current authenticated user"""
    token = credentials.credentials
    user = AuthService.verify_session_token(token, db)

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token"
        )

    return user


# Permission checker dependency
def require_permission(action: str, resource: str):
    """Dependency factory for checking permissions"""

    def permission_checker(user: User = Depends(get_current_user)) -> User:
        if not AuthService.has_permission(user, action, resource):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Insufficient permissions for {action} on {resource}"
            )
        return user

    return permission_checker


# Audit logging
class AuditService:
    @staticmethod
    def log_action(
            db: Session,
            user_id: Optional[int],
            session_id: Optional[str],
            action: str,
            resource_type: str,
            resource_id: Optional[int] = None,
            old_values: Optional[Dict[str, Any]] = None,
            new_values: Optional[Dict[str, Any]] = None,
            ip_address: Optional[str] = None,
            user_agent: Optional[str] = None,
            request_url: Optional[str] = None,
            success: bool = True,
            error_message: Optional[str] = None
    ):
        """Log audit event"""
        audit_log = AuditLog(
            user_id=user_id,
            session_id=session_id,
            action=action,
            resource_type=resource_type,
            resource_id=resource_id,
            old_values=old_values,
            new_values=new_values,
            ip_address=ip_address,
            user_agent=user_agent,
            request_url=request_url,
            success=success,
            error_message=error_message
        )

        db.add(audit_log)
        db.commit()


# FastAPI app
app = FastAPI(
    title="Signal Monitoring API",
    description="REST API for Signal Monitoring System with SQLModel",
    version="2.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure appropriately for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Authentication middleware
@app.middleware("http")
async def audit_middleware(request: Request, call_next):
    """Middleware for request auditing"""
    start_time = datetime.utcnow()

    # Get client info
    client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "")

    response = await call_next(request)

    # Log API access (simplified - you might want to be more selective)
    if hasattr(request.state, "user"):
        user = request.state.user
        # Log successful API calls
        # You can implement more sophisticated logging here

    return response


# Authentication endpoints
@app.post("/auth/login")
async def login(
        pki_cert_data: Dict[str, str],
        request: Request,
        db: Session = Depends(get_db)
):
    """Authenticate user with PKI certificate"""
    cn = pki_cert_data.get("cn")
    serial = pki_cert_data.get("serial")
    fingerprint = pki_cert_data.get("fingerprint")

    if not all([cn, serial, fingerprint]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Missing PKI certificate data"
        )

    # Find user by PKI certificate
    stmt = select(User).where(
        and_(
            User.pki_certificate_cn == cn,
            User.pki_certificate_serial == serial,
            User.pki_certificate_fingerprint == fingerprint,
            User.is_active == True
        )
    )
    user = db.exec(stmt).first()

    if not user:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid PKI certificate"
        )

    # Create session
    client_ip = request.client.host
    user_agent = request.headers.get("user-agent", "")
    session_token = AuthService.create_session(user, client_ip, user_agent, db)

    # Log login
    AuditService.log_action(
        db=db,
        user_id=user.id,
        session_id=session_token,
        action="login",
        resource_type="user",
        resource_id=user.id,
        ip_address=client_ip,
        user_agent=user_agent,
        request_url=str(request.url)
    )

    return {"access_token": session_token, "token_type": "bearer"}


@app.post("/auth/logout")
async def logout(
        request: Request,
        user: User = Depends(get_current_user),
        db: Session = Depends(get_db)
):
    """Logout current user"""
    # Get session token from header
    auth_header = request.headers.get("authorization", "")
    token = auth_header.replace("Bearer ", "") if auth_header.startswith("Bearer ") else ""

    if token:
        # Delete session
        stmt = select(UserSession).where(UserSession.id == token)
        session = db.exec(stmt).first()
        if session:
            db.delete(session)
            db.commit()

    # Log logout
    AuditService.log_action(
        db=db,
        user_id=user.id,
        session_id=token,
        action="logout",
        resource_type="user",
        resource_id=user.id,
        ip_address=request.client.host,
        user_agent=request.headers.get("user-agent", ""),
        request_url=str(request.url)
    )

    return {"message": "Successfully logged out"}


# User endpoints
@app.get("/users", response_model=List[UserRead])
async def get_users(
        skip: int = 0,
        limit: int = 100,
        user: User = Depends(require_permission("read", "user")),
        db: Session = Depends(get_db)
):
    """Get all users"""
    stmt = select(User).offset(skip).limit(limit)
    users = db.exec(stmt).all()
    return users


@app.get("/users/{user_id}", response_model=UserRead)
async def get_user(
        user_id: int,
        user: User = Depends(require_permission("read", "user")),
        db: Session = Depends(get_db)
):
    """Get user by ID"""
    stmt = select(User).where(User.id == user_id)
    db_user = db.exec(stmt).first()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    return db_user


@app.post("/users", response_model=UserRead)
async def create_user(
        user_data: UserCreate,
        current_user: User = Depends(require_permission("create", "user")),
        db: Session = Depends(get_db)
):
    """Create new user"""
    try:
        db_user = User.model_validate(user_data)
        db.add(db_user)
        db.commit()
        db.refresh(db_user)

        # Log creation
        AuditService.log_action(
            db=db,
            user_id=current_user.id,
            action="create",
            resource_type="user",
            resource_id=db_user.id,
            new_values=user_data.model_dump()
        )

        return db_user
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="User with this certificate or username already exists"
        )


@app.put("/users/{user_id}", response_model=UserRead)
async def update_user(
        user_id: int,
        user_data: UserUpdate,
        current_user: User = Depends(require_permission("update", "user")),
        db: Session = Depends(get_db)
):
    """Update user"""
    stmt = select(User).where(User.id == user_id)
    db_user = db.exec(stmt).first()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    # Store old values for audit
    old_values = {
        "username_alias": db_user.username_alias,
        "is_active": db_user.is_active,
        "post_id": db_user.post_id
    }

    # Update fields
    update_data = user_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_user, field, value)

    db.add(db_user)
    db.commit()
    db.refresh(db_user)

    # Log update
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="update",
        resource_type="user",
        resource_id=db_user.id,
        old_values=old_values,
        new_values=update_data
    )

    return db_user


@app.delete("/users/{user_id}")
async def delete_user(
        user_id: int,
        current_user: User = Depends(require_permission("delete", "user")),
        db: Session = Depends(get_db)
):
    """Delete user (soft delete)"""
    stmt = select(User).where(User.id == user_id)
    db_user = db.exec(stmt).first()

    if not db_user:
        raise HTTPException(status_code=404, detail="User not found")

    db_user.is_deleted = True
    db_user.is_active = False
    db.add(db_user)
    db.commit()

    # Log deletion
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="delete",
        resource_type="user",
        resource_id=db_user.id
    )

    return {"message": "User deleted successfully"}


# Post endpoints
@app.get("/posts", response_model=List[PostRead])
async def get_posts(
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        user: User = Depends(require_permission("read", "post")),
        db: Session = Depends(get_db)
):
    """Get all posts"""
    stmt = select(Post)

    if is_active is not None:
        stmt = stmt.where(Post.is_active == is_active)

    stmt = stmt.offset(skip).limit(limit)
    posts = db.exec(stmt).all()
    return posts


@app.get("/posts/{post_id}", response_model=PostRead)
async def get_post(
        post_id: int,
        user: User = Depends(require_permission("read", "post")),
        db: Session = Depends(get_db)
):
    """Get post by ID"""
    stmt = select(Post).where(Post.id == post_id)
    post = db.exec(stmt).first()

    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    return post


@app.post("/posts", response_model=PostRead)
async def create_post(
        post_data: PostCreate,
        current_user: User = Depends(require_permission("create", "post")),
        db: Session = Depends(get_db)
):
    """Create new post"""
    try:
        db_post = Post.model_validate(post_data)
        db.add(db_post)
        db.commit()
        db.refresh(db_post)

        # Log creation
        AuditService.log_action(
            db=db,
            user_id=current_user.id,
            action="create",
            resource_type="post",
            resource_id=db_post.id,
            new_values=post_data.model_dump()
        )

        return db_post
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Post with this code already exists"
        )


@app.put("/posts/{post_id}", response_model=PostRead)
async def update_post(
        post_id: int,
        post_data: PostUpdate,
        current_user: User = Depends(require_permission("update", "post")),
        db: Session = Depends(get_db)
):
    """Update post"""
    stmt = select(Post).where(Post.id == post_id)
    db_post = db.exec(stmt).first()

    if not db_post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Store old values for audit
    old_values = {
        "name": db_post.name,
        "description": db_post.description,
        "region": db_post.region,
        "is_active": db_post.is_active,
        "connection_status": db_post.connection_status
    }

    # Update fields
    update_data = post_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_post, field, value)

    db.add(db_post)
    db.commit()
    db.refresh(db_post)

    # Log update
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="update",
        resource_type="post",
        resource_id=db_post.id,
        old_values=old_values,
        new_values=update_data
    )

    return db_post


@app.delete("/posts/{post_id}")
async def delete_post(
        post_id: int,
        current_user: User = Depends(require_permission("delete", "post")),
        db: Session = Depends(get_db)
):
    """Delete post (soft delete)"""
    stmt = select(Post).where(Post.id == post_id)
    db_post = db.exec(stmt).first()

    if not db_post:
        raise HTTPException(status_code=404, detail="Post not found")

    # Check if post has active observations
    active_obs_stmt = select(func.count(Observation.id)).where(
        and_(
            Observation.post_id == post_id,
            Observation.is_deleted == False,
            Observation.status == ObservationStatus.ACTIVE
        )
    )
    active_count = db.exec(active_obs_stmt).one() or 0

    if active_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete post with {active_count} active observations. Please handle observations first."
        )

    # Soft delete by setting is_active to False
    db_post.is_active = False
    db_post.connection_status = ConnectionStatus.OFFLINE
    db.add(db_post)
    db.commit()

    # Log deletion
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="delete",
        resource_type="post",
        resource_id=db_post.id,
        old_values={"is_active": True, "connection_status": db_post.connection_status}
    )

    return {"message": "Post deleted successfully"}


# Signal Type endpoints
@app.get("/signal-types", response_model=List[SignalTypeRead])
async def get_signal_types(
        skip: int = 0,
        limit: int = 100,
        is_active: Optional[bool] = None,
        category: Optional[str] = None,
        user: User = Depends(require_permission("read", "signal_type")),
        db: Session = Depends(get_db)
):
    """Get all signal types"""
    stmt = select(SignalType)

    if is_active is not None:
        stmt = stmt.where(SignalType.is_active == is_active)

    if category:
        stmt = stmt.where(SignalType.category == category)

    stmt = stmt.order_by(SignalType.sort_order).offset(skip).limit(limit)
    signal_types = db.exec(stmt).all()
    return signal_types


@app.get("/signal-types/{signal_type_id}", response_model=SignalTypeRead)
async def get_signal_type(
        signal_type_id: int,
        user: User = Depends(require_permission("read", "signal_type")),
        db: Session = Depends(get_db)
):
    """Get signal type by ID"""
    stmt = select(SignalType).where(SignalType.id == signal_type_id)
    signal_type = db.exec(stmt).first()

    if not signal_type:
        raise HTTPException(status_code=404, detail="Signal type not found")

    return signal_type


@app.post("/signal-types", response_model=SignalTypeRead)
async def create_signal_type(
        signal_type_data: SignalTypeCreate,
        current_user: User = Depends(require_permission("create", "signal_type")),
        db: Session = Depends(get_db)
):
    """Create new signal type"""
    try:
        db_signal_type = SignalType.model_validate(signal_type_data)
        db.add(db_signal_type)
        db.commit()
        db.refresh(db_signal_type)

        # Log creation
        AuditService.log_action(
            db=db,
            user_id=current_user.id,
            action="create",
            resource_type="signal_type",
            resource_id=db_signal_type.id,
            new_values=signal_type_data.model_dump()
        )

        return db_signal_type
    except IntegrityError:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Signal type with this code already exists"
        )


@app.put("/signal-types/{signal_type_id}", response_model=SignalTypeRead)
async def update_signal_type(
        signal_type_id: int,
        signal_type_data: SignalTypeUpdate,
        current_user: User = Depends(require_permission("update", "signal_type")),
        db: Session = Depends(get_db)
):
    """Update signal type"""
    stmt = select(SignalType).where(SignalType.id == signal_type_id)
    db_signal_type = db.exec(stmt).first()

    if not db_signal_type:
        raise HTTPException(status_code=404, detail="Signal type not found")

    # Store old values for audit
    old_values = {
        "name": db_signal_type.name,
        "category": db_signal_type.category,
        "description": db_signal_type.description,
        "is_active": db_signal_type.is_active,
        "sort_order": db_signal_type.sort_order
    }

    # Update fields
    update_data = signal_type_data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(db_signal_type, field, value)

    db.add(db_signal_type)
    db.commit()
    db.refresh(db_signal_type)

    # Log update
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="update",
        resource_type="signal_type",
        resource_id=db_signal_type.id,
        old_values=old_values,
        new_values=update_data
    )

    return db_signal_type


@app.delete("/signal-types/{signal_type_id}")
async def delete_signal_type(
        signal_type_id: int,
        current_user: User = Depends(require_permission("delete", "signal_type")),
        db: Session = Depends(get_db)
):
    """Delete signal type (soft delete)"""
    stmt = select(SignalType).where(SignalType.id == signal_type_id)
    db_signal_type = db.exec(stmt).first()

    if not db_signal_type:
        raise HTTPException(status_code=404, detail="Signal type not found")

    # Check if signal type is used in active observations
    active_obs_stmt = select(func.count(Observation.id)).where(
        and_(
            Observation.signal_type_id == signal_type_id,
            Observation.is_deleted == False,
            Observation.status == ObservationStatus.ACTIVE
        )
    )
    active_count = db.exec(active_obs_stmt).one() or 0

    if active_count > 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Cannot delete signal type with {active_count} active observations. Please handle observations first."
        )

    # Soft delete by setting is_active to False
    db_signal_type.is_active = False
    db.add(db_signal_type)
    db.commit()

    # Log deletion
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="delete",
        resource_type="signal_type",
        resource_id=db_signal_type.id,
        old_values={"is_active": True}
    )

    return {"message": "Signal type deleted successfully"}


# Observation endpoints
@app.get("/observations", response_model=List[ObservationRead])
async def get_observations(
        skip: int = 0,
        limit: int = 100,
        post_id: Optional[int] = None,
        signal_type_id: Optional[int] = None,
        status: Optional[ObservationStatus] = None,
        date_from: Optional[datetime] = None,
        date_to: Optional[datetime] = None,
        user: User = Depends(require_permission("read", "observation")),
        db: Session = Depends(get_db)
):
    """Get observations with filters"""
    stmt = select(Observation).where(Observation.is_deleted == False)

    if post_id:
        stmt = stmt.where(Observation.post_id == post_id)

    if signal_type_id:
        stmt = stmt.where(Observation.signal_type_id == signal_type_id)

    if status:
        stmt = stmt.where(Observation.status == status)

    if date_from:
        stmt = stmt.where(Observation.observed_at >= date_from)

    if date_to:
        stmt = stmt.where(Observation.observed_at <= date_to)

    stmt = stmt.order_by(Observation.observed_at.desc()).offset(skip).limit(limit)
    observations = db.exec(stmt).all()
    return observations


@app.post("/observations", response_model=ObservationRead)
async def create_observation(
        observation_data: ObservationCreate,
        current_user: User = Depends(require_permission("create", "observation")),
        db: Session = Depends(get_db)
):
    """Create new observation"""
    # Calculate center frequency and bandwidth if not provided
    obs_dict = observation_data.model_dump()
    if not obs_dict.get("frequency_center"):
        obs_dict["frequency_center"] = (obs_dict["frequency_min"] + obs_dict["frequency_max"]) / 2

    if not obs_dict.get("bandwidth"):
        obs_dict["bandwidth"] = obs_dict["frequency_max"] - obs_dict["frequency_min"]

    obs_dict["user_id"] = current_user.id

    db_observation = Observation(**obs_dict)
    db.add(db_observation)
    db.commit()
    db.refresh(db_observation)

    # Update user's last record created timestamp
    current_user.last_record_created_at = datetime.utcnow()
    db.add(current_user)
    db.commit()

    # Log creation
    AuditService.log_action(
        db=db,
        user_id=current_user.id,
        action="create",
        resource_type="observation",
        resource_id=db_observation.id,
        new_values=observation_data.model_dump()
    )

    return db_observation


# Health check
@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow()}


# Create tables
def create_db_and_tables():
    SQLModel.metadata.create_all(engine)


if __name__ == "__main__":
    create_db_and_tables()
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)