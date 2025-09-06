import os
from typing import Any, Dict
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings
import toml
from loguru import logger


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
    access_token_expire_minutes: int = 1440
    session_expire_hours: int = 24


class ServerConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8000
    reload: bool = False
    workers: int = 4


class AdminConfig(BaseModel):
    username: str = "admin"
    username_alias: str = "System Administrator"
    pki_certificate_cn: str = "ADMIN_CN"
    pki_certificate_serial: str = "ADMIN_SERIAL_001"
    pki_certificate_fingerprint: str = "ADMIN_FINGERPRINT_001"


class Settings(BaseSettings):
    app: AppConfig = Field(default_factory=AppConfig)
    database: DatabaseConfig = Field(default_factory=DatabaseConfig)
    security: SecurityConfig = Field(default_factory=SecurityConfig)
    server: ServerConfig = Field(default_factory=ServerConfig)
    admin: AdminConfig = Field(default_factory=AdminConfig)

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
            logger.warning(f"Config file not found in any of: {config_paths}")
            logger.warning(f"Current working directory: {os.getcwd()}")

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
logger.info(f"Loaded configuration for environment: {settings.app.environment}")