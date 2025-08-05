# Example usage
import asyncio
import tomllib
from pathlib import Path

from loguru import logger

from helpers.config_loader_helper import ConfigManager
from validators.config.app_config import AppConfig


async def load_config():
    logger.remove()  # Remove default handler
    logger.add(
        lambda msg: print(msg, end=""),  # Console output
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level: <8}</level> | {message}",
        level="INFO"
    )

    config_file = Path("../../dev_configs/error_test_conf.toml")
    config_file = Path("../../dev_configs/success_test_conf.toml")

    try:
        # Load and validate configuration
        conf_manager = ConfigManager(config_path=config_file, config_class=AppConfig)
        loaded_conf: AppConfig = await conf_manager.get_config_async()

        # Use the configuration
        logger.info(f"App: {loaded_conf.app_name} v{loaded_conf.version}")
        logger.info(f"Database: {loaded_conf.database.host}:{loaded_conf.database.port}")
        logger.info(f"Server: {loaded_conf.server.host}:{loaded_conf.server.port}")

    except (FileNotFoundError, ValueError, tomllib.TOMLDecodeError) as e:
        logger.error(f"Failed to load configuration: {e}")
        exit(1)

if __name__ == "__main__":
    asyncio.run(load_config())


