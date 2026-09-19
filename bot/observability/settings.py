"""Optional audit configuration, kept out of the application's settings/schema."""

from pathlib import Path

from cryptography.fernet import Fernet
from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class AuditSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="AUDIT_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    enabled: bool = True
    directory: Path = Path("data/audit")
    admin_ids: list[int] = Field(default_factory=lambda: [74529696])
    encryption_key: SecretStr | None = None
    batch_seconds: int = Field(default=60, ge=10, le=3600)
    detailed_minutes: int = Field(default=30, ge=1, le=1440)
    retention_days: int = Field(default=3, ge=0, le=365)
    max_disk_mb: int = Field(default=128, ge=8, le=10240)
    max_text_chars: int = Field(default=262144, ge=1024, le=1048576)
    segment_bytes: int = Field(default=2 * 1024 * 1024, ge=65536, le=8 * 1024 * 1024)

    @field_validator("encryption_key")
    @classmethod
    def valid_key(cls, value):
        if value is not None:
            try:
                Fernet(value.get_secret_value().encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("AUDIT_ENCRYPTION_KEY must be a Fernet key") from exc
        return value
