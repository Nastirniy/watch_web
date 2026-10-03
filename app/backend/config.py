import os
from typing import Optional
from pathlib import Path
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings
import toml

from app.backend.enums.general import AppEnvironment


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
    pool_size: int = 20
    max_overflow: int = 40

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
    public_prefix: str = "/t"
    # Origin prepended to public URLs, e.g. https://monitoring.example; empty keeps them relative
    public_base_url: str = ""
    # Origin the API uses to pre-warm the nginx cache after an upload, e.g. http://nginx inside compose.
    # Falls back to public_base_url; pre-warming is skipped when both are empty.
    prewarm_base_url: str = ""

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
    imgproxy: ImgproxySettings = Field(default_factory=ImgproxySettings)
    upload: UploadSettings = Field(default_factory=UploadSettings)
    # Single nginx flag -- enables all nginx offloading:
    # upload (client_body_in_file_only), download and stream (X-Accel-Redirect), thumbnails (X-Accel-Redirect to imgproxy)
    enable_nginx: bool = False

    nginx_upload_temp_path: str = "/var/www/nginx_uploads"
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


class HealthConfig(BaseModel):
    """Configuration for health endpoint security"""
    allowed_hosts: list[str] = ["127.0.0.1", "localhost", "::1"]
    rate_limit_requests: int = 10
    rate_limit_window_seconds: int = 60
    enable_rate_limit: bool = True
    enable_host_check: bool = True

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

        # Initialize models
        for field_name, field_type in self.__annotations__.items():
            if field_name in config_data:
                kwargs[field_name] = field_type(**config_data[field_name])

        super().__init__(**kwargs)


settings = Settings()
# Note: Actual logging will be set up in main.py after this config is loaded
print(f"INFO: Loaded configuration for environment: {settings.app.environment}")
