from functools import lru_cache
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    bot_token: SecretStr
    encryption_key: SecretStr
    database_url: str = "sqlite+aiosqlite:///./calendar_bot.db"
    default_timezone: str = "Europe/Moscow"
    sync_interval_seconds: int = 60
    notifier_interval_seconds: int = 5
    caldav_timeout_seconds: int = 20
    reminder_grace_seconds: int = 90
    event_horizon_days: int = 45
    admin_ids: list[int] = []

    @field_validator("encryption_key")
    @classmethod
    def valid_key(cls, value: SecretStr) -> SecretStr:
        try:
            Fernet(value.get_secret_value().encode())
        except (ValueError, TypeError) as exc:
            raise ValueError("Сгенерируйте ENCRYPTION_KEY: python -m bot.generate_key") from exc
        return value

    @field_validator("default_timezone")
    @classmethod
    def valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError("Неизвестный часовой пояс IANA") from exc
        return value

    @field_validator(
        "sync_interval_seconds",
        "notifier_interval_seconds",
        "caldav_timeout_seconds",
        "reminder_grace_seconds",
        "event_horizon_days",
    )
    @classmethod
    def positive(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Значение должно быть положительным")
        return value


@lru_cache
def get_settings() -> Settings:
    return Settings()
