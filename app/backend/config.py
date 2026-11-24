import os
from typing import Optional
from pathlib import Path
from pydantic import BaseModel, Field, PositiveInt, field_validator, model_validator
from pydantic_settings import BaseSettings
import toml

from app.backend.enums.general import AppEnvironment, P12Encryption


class AppConfig(BaseModel):
    name: str = "Radio Monitoring System"
    version: str = "1.0.0"
    debug: bool = False
    environment: str = AppEnvironment.PRODUCTION.value
    # Base of the RFC 9457 "type" URIs, e.g. "https://monitoring.example/problems". Empty: the origin of
    # the request + /problems, so every deployment documents its own problem types.
    problem_base_url: str = ""


class DatabaseConfig(BaseModel):
    host: str = "localhost"
    port: int = 3306
    name: str = "radio_monitoring"
    user: str = "dbuser"
    password: str
    echo: bool = False
    # Per process: 4 api workers x 20 plus the media worker stay under MariaDB's default max_connections 151
    pool_size: int = 10
    max_overflow: int = 10
    # How long a request waits for a free connection; then 503 database_busy with Retry-After (errors/handlers.py)
    pool_timeout_seconds: float = Field(default=10, ge=1)

    @property
    def url(self) -> str:
        return f"mysql+aiomysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class SecurityConfig(BaseModel):
    secret_key: str
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30  # never beyond the hard end of its session
    # A session ends after this long without a request; every request and refresh moves the end
    session_idle_timeout_minutes: int = 30
    # Optional hard limit since the login whatever the activity; 0: an active session never ends by itself
    session_max_hours: int = 0
    jwt_leeway_seconds: int = 10
    online_threshold_seconds: int = 300  # user is online when a session was touched within this window
    activity_touch_interval_seconds: int = 60  # sessions are touched at most this often
    # The previous refresh token presented again this soon after its rotation is refused without ending the session
    # (two tabs refreshing at once); any other reuse of a used refresh token ends it (auth/sessions.py)
    refresh_reuse_grace_seconds: int = Field(default=30, ge=0)


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"  # More secure default
    port: int = 8000
    reload: bool = False
    workers: int = 4
    ssl_keyfile: Optional[str] = None
    ssl_certfile: Optional[str] = None


class AdminConfig(BaseModel):
    username: str = "admin"
    username_alias: str = "System Administrator"
    pki_certificate_cn: str = "admin@radiomonitoring.local"
    pki_certificate_serial: str = "ABC123DEF456"
    pki_certificate_fingerprint: str = "12:34:56:78:90:AB:CD:EF:12:34:56:78:90:AB:CD:EF:12:34:56:78"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file_path: Optional[str] = None
    enable_file_logging: bool = False


class TimezoneConfig(BaseModel):
    timezone: str = "UTC"

    @field_validator('timezone')
    @classmethod
    def validate_timezone(cls, v):
        # Handle empty string as fallback to default
        if v == "":
            return "UTC"
        return v


def _parse_json_sequence(value):
    """Accept a JSON list given as a string (environment overrides) next to real lists from TOML"""
    if isinstance(value, str):
        import json
        return json.loads(value)
    return value


class ThumbnailSettings(BaseModel):
    """Thumbnail and preview sizes rendered on demand by imgproxy.

    Nothing here is precomputed: every size is a bounding box that imgproxy
    fits the original (or the video poster) into on request.
    """
    small_size: tuple[int, int] = (150, 150)
    medium_size: tuple[int, int] = (300, 300)
    large_size: tuple[int, int] = (600, 600)
    preview_size: tuple[int, int] = (1600, 1600)
    quality: int = 85
    # Lifetime of the signed URLs embedded in list responses
    url_ttl_seconds: int = 3600
    # Upper bound a share link may ask for
    share_max_ttl_seconds: int = 7 * 24 * 3600
    # Request the medium size once after an image upload so the first gallery view is a cache hit
    prewarm: bool = True

    @field_validator('small_size', 'medium_size', 'large_size', 'preview_size', mode='before')
    @classmethod
    def parse_size(cls, v):
        # Environment overrides arrive as strings: MYSETTINGS_STORAGE__THUMBNAIL__SMALL_SIZE=[150, 150]
        return _parse_json_sequence(v)

    @field_validator('quality')
    @classmethod
    def validate_quality(cls, v):
        return max(1, min(100, v))

    @field_validator('url_ttl_seconds', 'share_max_ttl_seconds')
    @classmethod
    def validate_ttl(cls, v):
        return max(60, int(v))

    def size_for(self, name: str) -> tuple[int, int]:
        sizes = {
            "small": self.small_size,
            "medium": self.medium_size,
            "large": self.large_size,
            "preview": self.preview_size,
        }
        return sizes.get(name, self.medium_size)


class PosterSettings(BaseModel):
    """Video poster extraction, done by the media worker with ffmpeg.

    A poster is the only precomputed derived file in the system: one JPEG per
    video, which imgproxy then resizes like any image.
    """
    max_attempts: int = 3
    lease_seconds: int = 300
    timeout_seconds: int = 60
    poll_interval_seconds: float = 1.0
    idle_poll_interval_seconds: float = 5.0
    # Fraction of the duration to seek to, capped by max_seek_seconds
    frame_position: float = 0.1
    max_seek_seconds: int = 30
    # Poster width cap; imgproxy makes every smaller size from it
    max_width: int = 1920
    # Parallel ffmpeg processes per worker
    concurrency: int = 2
    heartbeat_seconds: int = 30
    stale_after_seconds: int = 120
    # Retry delays after the 1st, 2nd, ... failure
    backoff_seconds: list[int] = [30, 120, 480]

    @field_validator('backoff_seconds', mode='before')
    @classmethod
    def parse_backoff(cls, v):
        return _parse_json_sequence(v)

    @field_validator('frame_position')
    @classmethod
    def validate_frame_position(cls, v):
        return max(0.0, min(1.0, v))

    @field_validator('max_attempts', 'concurrency')
    @classmethod
    def validate_positive(cls, v):
        return max(1, int(v))


class StoryboardSettings(BaseModel):
    """Video storyboards (seek previews): sprite sheets made by the media worker with ffmpeg.

    They run only while the poster queue is empty and take at most `slots` places,
    which must stay below poster.concurrency so a poster always has a free place.
    """
    # Off: no claims, the API answers unsupported, existing files stay
    enabled: bool = True
    slots: int = 1
    # Shorter videos get none
    min_duration_seconds: int = 60
    # Bounds of the adaptive frame step
    max_frames: int = 300
    min_step_seconds: int = 2
    tile_width: int = 160
    columns: int = 10
    rows: int = 10
    # ffmpeg -q:v (2 best, 31 worst)
    jpeg_qscale: int = 5
    threads: int = 1
    # POSIX niceness of ffmpeg, 0 = unchanged
    nice: int = 10
    timeout_seconds: int = 1800
    lease_seconds: int = 300
    max_attempts: int = 3
    # Retry delays after the 1st, 2nd, ... failure
    backoff_seconds: list[int] = [60, 300, 1800]
    # Retry-After of the 202 answer
    retry_after_seconds: int = 30

    @field_validator('backoff_seconds', mode='before')
    @classmethod
    def parse_backoff(cls, v):
        return _parse_json_sequence(v)

    @field_validator('slots', 'max_attempts', 'max_frames', 'min_step_seconds', 'tile_width', 'columns', 'rows',
                     'threads', 'timeout_seconds', 'lease_seconds', 'retry_after_seconds')
    @classmethod
    def validate_positive(cls, v):
        return max(1, int(v))

    @field_validator('jpeg_qscale')
    @classmethod
    def validate_qscale(cls, v):
        return max(2, min(31, int(v)))

    @field_validator('nice', 'min_duration_seconds')
    @classmethod
    def validate_not_negative(cls, v):
        return max(0, int(v))


class UploadSettings(BaseModel):
    """Resumable uploads (POST /media/uploads)"""
    # An open session that received nothing for this long is expired and its part file removed
    session_ttl_hours: int = 24
    # Largest single PATCH body; nginx client_max_body_size on the uploads location must allow it
    max_chunk_bytes: int = 64 * 1024 * 1024
    # Recommended chunk size sent to clients (they may go lower on bad links)
    recommended_chunk_bytes: int = 8 * 1024 * 1024
    # How often the media worker expires stale sessions
    sweep_interval_seconds: int = 600

    @field_validator('session_ttl_hours', 'max_chunk_bytes', 'recommended_chunk_bytes', 'sweep_interval_seconds')
    @classmethod
    def validate_positive(cls, v):
        return max(1, int(v))


class ImgproxySettings(BaseModel):
    """imgproxy renderer. The API never talks to it directly in production:
    nginx proxies the internal and public prefixes to it and caches the result.
    """
    # Base URL the API uses for pre-warm requests and for proxying when nginx is disabled
    url: str = "http://127.0.0.1:8080"
    # Hex-encoded key and salt; both empty means unsigned URLs (development only)
    key: str = ""
    salt: str = ""
    # nginx internal location used through X-Accel-Redirect
    nginx_internal_prefix: str = "/_thumb"
    # nginx public location for signed, expiring URLs (list responses and share links)
    public_prefix: str = "/media-images"
    # Origin prepended to public URLs, e.g. https://monitoring.example; empty keeps them relative
    public_base_url: str = ""
    # Origin the API uses to pre-warm the nginx cache after an upload, e.g. http://nginx inside compose.
    # Falls back to public_base_url; pre-warming is skipped when both are empty.
    prewarm_base_url: str = ""
    # imgproxy's IMGPROXY_MAX_SRC_RESOLUTION (megapixels): imgproxy refuses every render of a larger image, so such
    # images are stored but get thumbnail_status unsupported. docker-compose.yml gives both containers one variable.
    # 250 covers 200 MP phone cameras; libvips decodes JPEG at a reduced scale and PNG line by line, so memory stays
    # low (200 MP: 63 MB JPEG, 124 MB PNG), while the limit still stops a crafted image of a few hundred KB that holds
    # gigapixels (1.6 GP: 3.5 s of CPU per render)
    max_src_resolution_mp: float = 250

    @property
    def signing_enabled(self) -> bool:
        return bool(self.key and self.salt)


class StorageConfig(BaseModel):
    """Configuration for media file storage"""
    upload_path: str = "uploads"
    temp_path: str = "temp"
    max_file_size_gb: int = 10
    thumbnail: ThumbnailSettings = Field(default_factory=ThumbnailSettings)
    poster: PosterSettings = Field(default_factory=PosterSettings)
    storyboard: StoryboardSettings = Field(default_factory=StoryboardSettings)
    imgproxy: ImgproxySettings = Field(default_factory=ImgproxySettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    # Single nginx flag -- enables all nginx offloading:
    # upload (client_body_in_file_only), download and stream (X-Accel-Redirect), thumbnails (X-Accel-Redirect to imgproxy)
    enable_nginx: bool = False

    nginx_upload_temp_path: str = "/var/www/nginx_uploads"
    # Files in nginx_upload_temp_path older than this are leftovers (requests nginx accepted and the API
    # refused before its handler ran); `media_tools sweep-nginx-temp` deletes them
    nginx_upload_temp_max_age_hours: int = 24
    nginx_internal_uploads_prefix: str = "/protected_uploads"

    # ffmpeg binaries used by the media worker. A bare name is looked up on PATH;
    # when not found the imageio-ffmpeg package (if installed) supplies a static binary.
    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"

    @field_validator('upload_path')
    @classmethod
    def validate_upload_path(cls, v):
        # Ensure upload path is not empty
        if not v.strip():
            return "uploads"
        return v.strip()

    @field_validator('max_file_size_gb')
    @classmethod
    def validate_max_file_size(cls, v):
        # Ensure max file size is between 1GB and 1TB
        return max(1, min(1024, v))


class PkiConfig(BaseModel):
    """The client certificate authority: MYSETTINGS_PKI__* (spec section 3,
    docs/superpowers/specs/2026-10-12-pki-certificates-design.md).

    PKI is configured when ca_cert_path, signer_socket, store_dir and crl_path are all set. Unconfigured, the API
    issues and exports nothing; uploaded certificates are never ca_issued.
    """
    ca_cert_path: str = ""          # the client CA certificate (PEM), read-only
    signer_socket: str = ""         # the Unix socket of pki-signer
    store_dir: str = ""             # the folder holding every issued certificate (<user_id>/<certificate_id>/)
    crl_path: str = ""              # the CRL file nginx reads (ssl_crl), written by the API
    extra_crl_paths: list[str] = []  # CRL files appended to the published one (the existing root's)
    # Out of range refuses to start. The signer has its own limits (signer.env): max_validity_days must stay at most
    # SIGNER_MAX_VALIDITY_DAYS and crl_days at most SIGNER_MAX_CRL_DAYS, or it refuses to sign (PKI.md, section 3)
    validity_days: int = Field(default=365, ge=1)       # default validity of a new certificate, <= max_validity_days
    max_validity_days: int = Field(default=730, ge=1)   # upper limit a request may ask for
    key_size: int = Field(default=2048, ge=2048, le=4096)   # RSA key size of user keys
    p12_encryption: P12Encryption = P12Encryption.AES256  # legacy: 3DES / SHA-1 for old Windows and macOS
    crl_days: int = Field(default=7, ge=1)              # how long a signed CRL stays valid (next_update)

    @field_validator('extra_crl_paths', mode='before')
    @classmethod
    def parse_extra_crl_paths(cls, v):
        # Environment overrides arrive as strings: MYSETTINGS_PKI__EXTRA_CRL_PATHS=["/pki/crl/legacy-root.crl"]
        return _parse_json_sequence(v)

    @model_validator(mode='after')
    def validity_within_the_limit(self):
        if self.validity_days > self.max_validity_days:
            raise ValueError(f"pki.validity_days ({self.validity_days}) must be at most pki.max_validity_days "
                             f"({self.max_validity_days})")
        return self

    def is_configured(self) -> bool:
        return all((self.ca_cert_path, self.signer_socket, self.store_dir, self.crl_path))


class HealthConfig(BaseModel):
    """Configuration for health endpoint security"""
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60
    enable_rate_limit: bool = True
    # None: on in production, off elsewhere (resolved in Settings); set it to decide explicitly
    enable_host_check: Optional[bool] = None

    @field_validator('allowed_hosts', mode='before')
    @classmethod
    def parse_allowed_hosts(cls, v):
        # Environment overrides arrive as strings: MYSETTINGS_HEALTH__ALLOWED_HOSTS=["127.0.0.1", "localhost", "::1"]
        return _parse_json_sequence(v)

    @field_validator('rate_limit_requests')
    @classmethod
    def validate_rate_limit_requests(cls, v):
        # Ensure rate limit is between 1 and 1000 requests
        return max(1, min(1000, v))

    @field_validator('rate_limit_window_seconds')
    @classmethod
    def validate_rate_limit_window(cls, v):
        # Ensure window is between 10 and 3600 seconds (1 hour)
        return max(10, min(3600, v))


class MovesConfig(BaseModel):
    """Moves that wait for the uploads below them: MYSETTINGS_MOVES__*"""
    # An upload that received no bytes for this long no longer keeps a move waiting; it is left alone and may go on after
    # the move. A chunk writer renews its claim every 30 seconds while bytes flow (services/media/uploads.py): stay well
    # above that
    upload_stale_seconds: int = Field(default=300, ge=60)
    # How often the media worker looks at the waiting moves (a backup: the requests that meet a move look too); also
    # the Retry-After of the 202 and 409 move_pending
    check_interval_seconds: int = Field(default=15, ge=1)


class EventsConfig(BaseModel):
    """Events to the web client: MYSETTINGS_EVENTS__* (API.md "Real-time events", ENV.md).
    Off: nothing is published, the stream answers 204 (WHATWG: stop reconnecting)."""
    enabled: bool = False
    # False: this process publishes but runs no hub and serves no stream (204): the streams live in a separate process
    serve_streams: bool = True
    poll_seconds: float = Field(default=1.0, gt=0)
    # A gap is watched this long; above innodb_lock_wait_timeout (50): a transaction that commits later loses its event
    gap_ttl_seconds: int = Field(default=60, ge=1)
    # A gap holds the resume position this long: a rollback must not pin every reconnect to a minute of duplicates
    position_hold_seconds: int = Field(default=8, ge=0)
    max_gap_jump: PositiveInt = 1000      # a jump above this is no gap: AUTO_INCREMENT was raised (a restore)
    max_open_gaps: PositiveInt = 5000
    ring_rows: PositiveInt = 10000        # replay comes only from here; about 5 MB per worker
    queue_max: PositiveInt = 256          # frames waiting per connection; above it the stream closes
    replay_max: PositiveInt = 240         # below queue_max; a longer replay is reload
    heartbeat_seconds: int = Field(default=20, ge=1)
    session_check_seconds: int = Field(default=10, ge=1)
    rights_refresh_seconds: int = Field(default=30, ge=1)
    retention_seconds: int = Field(default=3600, ge=60)
    max_streams_per_worker: PositiveInt = 3000   # above the whole target: one worker usually holds nearly all
    refetch_rate_per_second: PositiveInt = 30    # what one event may ask of the API
    refetch_window_max_ms: PositiveInt = 60000
    retry_min_ms: PositiveInt = 3000
    retry_max_ms: PositiveInt = 15000
    drain_retry_max_ms: PositiveInt = 60000

    @model_validator(mode="after")
    def replay_fits_the_queue(self):
        if self.replay_max >= self.queue_max:
            raise ValueError("events.replay_max must stay below events.queue_max: a replay goes through the queue")
        if self.retry_min_ms > self.retry_max_ms:
            raise ValueError("events.retry_min_ms must be at most events.retry_max_ms")
        return self


class ReportConfig(BaseModel):
    """Configuration of the Excel reports"""
    # Band rows of one observations report; more answer 422 report_too_large (the workbook is built in memory)
    max_rows: PositiveInt = 20000


class BackupConfig(BaseModel):
    """Database backups: MYSETTINGS_BACKUP__* (docs/superpowers/specs/2026-10-22-db-backups-design.md, BACKUPS.md).

    Backups are configured when dir is set. The db-backup tool service writes dir and copy_dir; the API only reads
    them (status in GET /database/backups and /health).
    """
    dir: str = ""                   # the backup folders (<id>/dump.sql.gz + manifest.json)
    copy_dir: str = ""              # optional off-host copy (a mounted share), same retention
    stale_after_hours: PositiveInt = 26   # the newest good backup older than this is stale
    # Retention of scheduled backups: the newest of each of the last N days, ISO weeks and months
    keep_daily: int = Field(default=7, ge=0)
    keep_weekly: int = Field(default=4, ge=0)
    keep_monthly: int = Field(default=6, ge=0)
    keep_manual_days: int = Field(default=90, ge=0)     # manual and pre-upgrade backups
    keep_previous_days: int = Field(default=7, ge=0)    # <db>_before_* schemas a restore keeps for undo
    # create refuses when the free space is below the last backup's size x 1.5 plus this
    min_free_mb: int = Field(default=512, ge=0)
    dump_timeout_seconds: PositiveInt = 3600            # a dump or a load running longer is killed
    # The MariaDB data directory as the tool sees it (a read-only mount): restore and verify --deep load a copy of
    # the database there and refuse when it lacks the space; empty: they refuse unless --skip-space-check
    datadir: str = ""

    def is_configured(self) -> bool:
        return bool(self.dir)


def _warn_unknown_settings(values: dict, fields: dict, prefix: str = "") -> None:
    """A key that no settings model declares is ignored: a setting that was removed or renamed, or a typo.
    Say so at startup instead of running silently with the default (logging is not set up yet: print)."""
    for key, value in values.items():
        name = f"{prefix}{key}"
        if key not in fields:
            print(f"WARNING: unknown setting {name} is ignored (removed, renamed or misspelled; see ENV.md)")
            continue
        model = fields[key]
        if isinstance(value, dict) and isinstance(model, type) and issubclass(model, BaseModel):
            nested = {field: info.annotation for field, info in model.model_fields.items()}
            _warn_unknown_settings(value, nested, f"{name}.")


class Settings(BaseSettings):
    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    timezone: TimezoneConfig = Field(default_factory=TimezoneConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    health: HealthConfig = Field(default_factory=HealthConfig)
    pki: PkiConfig = Field(default_factory=PkiConfig)
    reports: ReportConfig = Field(default_factory=ReportConfig)
    backup: BackupConfig = Field(default_factory=BackupConfig)
    moves: MovesConfig = Field(default_factory=MovesConfig)
    events: EventsConfig = Field(default_factory=EventsConfig)

    def __init__(self, **kwargs):
        # Load from TOML first
        config_data = {}
        # Look for config.toml in current directory or app directory
        config_paths = ["config.toml", "app/config.toml", "../config.toml"]
        config_file = None
        for path in config_paths:
            if os.path.exists(path):
                config_file = path
                break
        
        if config_file:
            with open(config_file, "r") as f:
                config_data = toml.load(f)
        else:
            # Use print for config loading since logging isn't set up yet
            print(f"WARNING: Config file not found in any of: {config_paths}")
            print(f"WARNING: Current working directory: {os.getcwd()}")

        # Override with environment variables (MYSETTINGS_* pattern)
        env_prefix = "MYSETTINGS_"
        for key, value in os.environ.items():
            if key.startswith(env_prefix):
                path = key[len(env_prefix):].lower().split("__")
                current = config_data
                for part in path[:-1]:
                    if part not in current:
                        current[part] = {}
                    current = current[part]
                current[path[-1]] = value

        _warn_unknown_settings(config_data, dict(self.__annotations__))

        # Initialize models
        for field_name, field_type in self.__annotations__.items():
            if field_name in config_data:
                kwargs[field_name] = field_type(**config_data[field_name])

        super().__init__(**kwargs)
        if self.health.enable_host_check is None:
            self.health.enable_host_check = self.app.environment == AppEnvironment.PRODUCTION.value


settings = Settings()
# Note: Actual logging will be set up in main.py after this config is loaded
print(f"INFO: Loaded configuration for environment: {settings.app.environment}")
