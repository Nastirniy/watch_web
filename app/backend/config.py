import os
from typing import Optional
from pydantic import BaseModel, Field, field_validator
from pydantic_settings import BaseSettings
import toml


class AppConfig(BaseModel):
    name: str = "Radio Monitoring System"
    version: str = "1.0.0"
    debug: bool = False
    environment: str = "production"


class DatabaseConfig(BaseModel):
    host: str = "localhost"
    port: int = 3306
    name: str = "radio_monitoring"
    user: str = "dbuser"
    password: str = "dbpassword"
    echo: bool = False
    pool_size: int = 20
    max_overflow: int = 40

    @property
    def url(self) -> str:
        return f"mysql+aiomysql://{self.user}:{self.password}@{self.host}:{self.port}/{self.name}"


class SecurityConfig(BaseModel):
    secret_key: str = "change-this-secret-key"
    algorithm: str = "HS256"
    access_token_expire_minutes: int = 30  # Reduced from 1440 for better security
    refresh_token_expire_days: int = 7
    session_expire_hours: int = 8  # Reduced from 24 for better security
    password_min_length: int = 12
    max_login_attempts: int = 5
    lockout_duration_minutes: int = 30
    require_https: bool = True
    allowed_origins: list[str] = ["https://localhost:3000"]
    jwt_leeway_seconds: int = 10


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
    dst_handling: bool = True
    
    @field_validator('timezone')
    @classmethod
    def validate_timezone(cls, v):
        # Handle empty string as fallback to default
        if v == "":
            return "UTC"
        return v


class StorageConfig(BaseModel):
    """Configuration for media file storage"""
    upload_path: str = "uploads"
    chunk_size_mb: int = 1
    upload_timeout_minutes: int = 30
    max_file_size_gb: int = 10
    
    @field_validator('upload_path')
    @classmethod
    def validate_upload_path(cls, v):
        # Ensure upload path is not empty
        if not v.strip():
            return "uploads"
        return v.strip()
    
    @field_validator('chunk_size_mb')
    @classmethod
    def validate_chunk_size(cls, v):
        # Ensure chunk size is between 1MB and 100MB
        return max(1, min(100, v))
    
    @field_validator('upload_timeout_minutes')
    @classmethod
    def validate_timeout(cls, v):
        # Ensure timeout is between 5 minutes and 24 hours
        return max(5, min(1440, v))
    
    @field_validator('max_file_size_gb')
    @classmethod
    def validate_max_file_size(cls, v):
        # Ensure max file size is between 1GB and 1TB
        return max(1, min(1024, v))


class Settings(BaseSettings):
    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)
    timezone: TimezoneConfig = Field(default_factory=TimezoneConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)

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
