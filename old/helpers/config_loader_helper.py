import asyncio
import tomllib
from pathlib import Path
from typing import Optional, Union, Dict, Any, Type, TypeVar

from loguru import logger
from pydantic import BaseModel, ValidationError


# Type variable for generic config loading
ConfigType = TypeVar('ConfigType', bound=BaseModel)


class ConfigManager:
    """Manages configuration loading, caching, and reloading asynchronously and synchronously."""

    def __init__(self, config_path: Path, config_class: Type[ConfigType]):
        """Initializes the config manager."""
        self.loader = ConfigLoader(config_path)
        self.config_class = config_class
        self._config: Optional[ConfigType] = None
        self._file_mtime: Optional[float] = None

    async def get_config_async(self, force_reload: bool = False) -> ConfigType:
        """
        Gets validated configuration asynchronously, reloading if needed.
        This method will await the underlying asynchronous loading operations.
        """
        if force_reload or self._needs_reload():
            # Await the asynchronous reload operation
            self._config = await self.loader.reload_async(self.config_class)
            self._file_mtime = self.loader.config_path.stat().st_mtime
        elif self._config is None:
            # Await the asynchronous load and validate operation
            self._config = await self.loader.load_and_validate_async(self.config_class)
            self._file_mtime = self.loader.config_path.stat().st_mtime
        return self._config

    def get_config(self, force_reload: bool = False) -> ConfigType:
        """
        Gets validated configuration synchronously, reloading if needed.
        This method uses the synchronous loading operations.
        """
        if force_reload or self._needs_reload():
            # Use the synchronous reload operation
            self._config = self.loader.reload(self.config_class)
            self._file_mtime = self.loader.config_path.stat().st_mtime
        elif self._config is None:
            # Use the synchronous load and validate operation
            self._config = self.loader.load_and_validate(self.config_class)
            self._file_mtime = self.loader.config_path.stat().st_mtime
        return self._config

    def _needs_reload(self) -> bool:
        """
        Checks if the config file has been modified.
        This remains synchronous as file stat operations are typically fast
        and don't block the event loop significantly.
        """
        if not self.loader.config_path.exists():
            return False
        current_mtime = self.loader.config_path.stat().st_mtime
        return self._file_mtime is None or current_mtime > self._file_mtime


class ConfigLoader:
    """Loads and validates TOML configurations asynchronously."""

    def __init__(self, config_path: Union[str, Path]):
        """Initializes the config loader."""
        self.config_path = Path(config_path)
        self._raw_config: Optional[Dict[str, Any]] = None
        self._validated_config: Optional[BaseModel] = None

    def _load_toml_content(self) -> Dict[str, Any]:
        """Loads raw TOML content from file synchronously."""
        if not self.config_path.exists():
            logger.error(f"Config file not found: {self.config_path}")
            raise FileNotFoundError(f"Config file not found: {self.config_path}")
        try:
            with open(self.config_path, "rb") as f:
                return tomllib.load(f)
        except tomllib.TOMLDecodeError as e:
            logger.error(f"Invalid TOML format in {self.config_path}: {e}")
            raise

    async def _load_toml_content_async(self) -> Dict[str, Any]:
        """Loads raw TOML content asynchronously using a thread pool executor."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._load_toml_content)

    def _validate_raw_config(self, config_class: Type[ConfigType]) -> ConfigType:
        """Validates raw config with Pydantic model synchronously."""
        if self._raw_config is None:  # Ensure raw config is loaded
            raise ValueError("Raw config not loaded before validation.")
        try:
            return config_class(**self._raw_config)
        except ValidationError as e:
            formatted_error = self._format_validation_error(e)
            logger.error(formatted_error)
            raise ValueError(formatted_error) from e

    # Synchronous methods (kept for flexibility, though async preferred with ConfigManager)
    def load_and_validate(self, config_class: Type[ConfigType]) -> ConfigType:
        """Loads and validates TOML synchronously."""
        self._raw_config = self._load_toml_content()
        self._validated_config = self._validate_raw_config(config_class)
        return self._validated_config

    async def load_and_validate_async(self, config_class: Type[ConfigType]) -> ConfigType:
        """Loads and validates TOML asynchronously."""
        self._raw_config = await self._load_toml_content_async()
        self._validated_config = self._validate_raw_config(config_class)
        return self._validated_config

    # Synchronous reload (kept for flexibility)
    def reload(self, config_class: Type[ConfigType]) -> ConfigType:
        """Reloads and validates configuration synchronously."""
        logger.info(f"{'Loading' if not self._raw_config else 'Reloading'} config from {self.config_path}")
        self._raw_config = None  # Clear cached raw config
        self._validated_config = None  # Clear cached validated config
        return self.load_and_validate(config_class)

    async def reload_async(self, config_class: Type[ConfigType]) -> ConfigType:
        """Reloads and validates configuration asynchronously."""
        logger.info(f"{'Loading' if not self._raw_config else 'Reloading'} config from {self.config_path}")
        self._raw_config = None  # Clear cached raw config
        self._validated_config = None  # Clear cached validated config
        return await self.load_and_validate_async(config_class)

    def _format_validation_error(self, error: ValidationError) -> str:
        """Formats Pydantic validation errors."""
        error_lines = [f"Config validation failed for '{self.config_path}':\n"]
        for i, err in enumerate(error.errors(), 1):
            field_path = ".".join(str(loc) for loc in err["loc"])
            error_lines.append(f"  {i}. Field: '{field_path}'")
            error_lines.append(f"     Error: {err['msg']}")
            error_lines.append(f"     Type: {err['type']}")
            error_lines.append(f"     Value: {repr(err.get('input', 'Not provided'))}")
            error_lines.append("")
        return "\n".join(error_lines)

    async def validate_file_only(self, config_class: Type[ConfigType]) -> bool:
        """Validates config file without storing result."""
        try:
            await self.load_and_validate_async(config_class)
            return True
        except (ValidationError, FileNotFoundError, Exception) as e:
            logger.error(f"Config validation failed: {e}")
            return False

    @property
    def raw_config(self) -> Optional[Dict[str, Any]]:
        """Gets raw configuration dictionary."""
        return self._raw_config

    @property
    def validated_config(self) -> Optional[BaseModel]:
        """Gets validated configuration instance."""
        return self._validated_config

    def is_loaded(self) -> bool:
        """Checks if configuration is loaded and validated."""
        return self._validated_config is not None
