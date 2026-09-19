from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


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
    value = value.strip()
    if not value or len(value) > 320 or any(c.isspace() for c in value) or ":" in value:
        raise ValueError("⚠️ Отправьте логин Яндекса или полный адрес почты без пробелов.")
    return value


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
