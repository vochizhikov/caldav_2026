from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    TypeAdapter,
    ValidationError,
    field_validator,
)
from pydantic_core import PydanticCustomError

_EMAIL_ADAPTER = TypeAdapter(EmailStr)
_BLOCKED_EMAIL_DOMAINS = frozenset({"gmail.com"})
_INVALID_LOGIN_MESSAGE = (
    "⚠️ Отправьте корректный логин Яндекса или полный адрес почты без пробелов."
)
_BLOCKED_EMAIL_MESSAGE = (
    "⚠️ Адреса @gmail.com не поддерживаются. "
    "Отправьте логин Яндекса или адрес почты Яндекса."
)


class LoginInput(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True, strict=True)

    login: str = Field(min_length=1, max_length=320)

    @field_validator("login")
    @classmethod
    def valid_login(cls, value: str) -> str:
        if any(char.isspace() or char in ":<>" for char in value):
            raise ValueError(_INVALID_LOGIN_MESSAGE)
        # Yandex also accepts a username without an email domain.
        if "@" not in value:
            return value
        email = _EMAIL_ADAPTER.validate_python(value)
        if email.rsplit("@", 1)[1] in _BLOCKED_EMAIL_DOMAINS:
            raise PydanticCustomError("blocked_email_domain", _BLOCKED_EMAIL_MESSAGE)
        return email


def validate_timezone(value: str) -> str:
    value = value.strip()
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(
            "🌍 Укажите часовой пояс IANA, например Europe/Moscow или Asia/Tbilisi."
        ) from exc
    return value


def validate_login(value: str) -> str:
    try:
        return LoginInput(login=value).login
    except ValidationError as exc:
        errors = exc.errors(include_url=False, include_context=False, include_input=False)
        message = (
            _BLOCKED_EMAIL_MESSAGE
            if errors[0]["type"] == "blocked_email_domain"
            else _INVALID_LOGIN_MESSAGE
        )
        raise ValueError(message) from exc


def validate_advance(value: str) -> int | None:
    if value == "off":
        return None
    if value not in {"1", "3", "5", "10"}:
        raise ValueError("⏱️ Допустимо 1, 3, 5 или 10 минут.")
    return int(value)


def validate_upcoming_days(value: str) -> int:
    if value not in {"1", "2", "3", "4", "5", "6", "7"}:
        raise ValueError("🗓 Выберите от 1 до 7 дней со встречами.")
    return int(value)
