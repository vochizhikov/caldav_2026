"""Settings for the removable admin monitor."""

from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitorSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="MONITOR_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )
    enabled: bool = True
    directory: Path = Path("data/monitoring")
    admin_ids: list[int] = Field(default_factory=lambda: [74529696])
    error_repeat_seconds: int = Field(default=300, ge=10, le=86400)
    max_pending: int = Field(default=10000, ge=100, le=100000)
