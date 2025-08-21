from pathlib import Path

from loguru import logger

from old.helpers.config_loader_helper import ConfigManager
from old.validators.config.app_config import AppConfig
from old.validators.config.fastapi_config import ServerConfig
from old.validators.config.redis_config import RedisConfig
from old.validators.config.sql_alchemy_config import DatabaseConfig

_app_config_file = Path("./dev_configs/app_config.toml")
_app_config_manager = ConfigManager(config_path=_app_config_file, config_class=AppConfig)
app_config: AppConfig = _app_config_manager.get_config()

fastapi_server_config: ServerConfig = app_config.server
redis_config: RedisConfig = app_config.redis
db_conf: DatabaseConfig = app_config.database

logger.debug(fastapi_server_config.model_dump_json(indent=2))
