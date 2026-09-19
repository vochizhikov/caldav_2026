import base64
import binascii
import hashlib
import struct
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

from bot.utils.common import event_text

CATALOG_PREFIX = "evcat:"
CATALOG_DAYS = 7
_HEADER = struct.Struct(">QQ4sB")
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
_TEXT_LIMIT = 4000


def timezone_key(timezone: str) -> bytes:
    return hashlib.sha256(timezone.encode()).digest()[:4]


@dataclass(frozen=True)
class CatalogCursor:
    """Keep each message's dates stable without server-side pagination state.

    The header plus seven 3-byte date ordinals fits in a 62-byte Telegram callback.
    """

    user_id: int
    opened_at: datetime
    timezone_hash: bytes
    dates: tuple[date, ...]
    page: int = 0

    def callback(self, page: int) -> str:
        elapsed = self.opened_at - _EPOCH
        microseconds = (elapsed.days * 86400 + elapsed.seconds) * 1_000_000 + elapsed.microseconds
        payload = _HEADER.pack(microseconds, self.user_id, self.timezone_hash, page)
        payload += b"".join(day.toordinal().to_bytes(3, "big") for day in self.dates)
        return CATALOG_PREFIX + base64.urlsafe_b64encode(payload).decode().rstrip("=")

    @classmethod
    def decode(cls, data: str):
        try:
            if not data.startswith(CATALOG_PREFIX) or len(data.encode()) > 64:
                raise ValueError("Invalid catalog callback")
            encoded = data.removeprefix(CATALOG_PREFIX)
            payload = base64.b64decode(
                encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True
            )
            if len(payload) not in {_HEADER.size + 3 * count for count in range(1, 8)}:
                raise ValueError("Invalid catalog dates")
            microseconds, user_id, zone_hash, page = _HEADER.unpack_from(payload)
            dates = tuple(
                date.fromordinal(int.from_bytes(payload[offset : offset + 3], "big"))
                for offset in range(_HEADER.size, len(payload), 3)
            )
            if tuple(sorted(set(dates))) != dates or page >= len(dates) or date.max in dates:
                raise ValueError("Invalid catalog page")
            return cls(
                user_id=user_id,
                opened_at=_EPOCH + timedelta(microseconds=microseconds),
                timezone_hash=zone_hash,
                dates=dates,
                page=page,
            )
        except (ValueError, OverflowError, struct.error, binascii.Error) as exc:
            raise ValueError("Invalid catalog callback") from exc

    @property
    def day(self) -> date:
        return self.dates[self.page]

    def at(self, page: int):
        return replace(self, page=page)


def _fits(text: str) -> bool:
    # Count raw HTML conservatively, including UTF-16 surrogate pairs used by Telegram.
    return len(text.encode("utf-16-le")) // 2 <= _TEXT_LIMIT


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def catalog_text(rows, user, cursor: CatalogCursor) -> str:
    header = (
        f"🗓 <b>Ближайшие события · {cursor.day:%d.%m.%Y}</b>\n"
        f"📖 День {cursor.page + 1} из {len(cursor.dates)} · <i>{escape(user.timezone)}</i>"
    )
    if not rows:
        return header + "\n\nВ этот день больше нет событий в выбранных календарях."
    warning = (
        "\n\n⚠️ Последнее обновление не удалось; данные могут быть устаревшими."
        if any(calendar.last_error for _, calendar in rows)
        else ""
    )
    cards = [
        event_text(
            occurrence.summary,
            occurrence.starts_at,
            occurrence.all_day,
            user.timezone,
            location=occurrence.location,
            meeting_url=occurrence.meeting_url,
            calendar_name=calendar.name,
        )
        for occurrence, calendar in rows
    ]
    detailed = header + "\n\n" + "\n\n".join(cards) + warning
    if _fits(detailed):
        return detailed

    zone = ZoneInfo(user.timezone)
    compact_note = "\n\nКраткий вид: время и название события."
    for title_limit in (120, 80, 40, 20, 10):
        lines = []
        for occurrence, _ in rows:
            when = (
                "Весь день"
                if occurrence.all_day
                else occurrence.starts_at.astimezone(zone).strftime("%H:%M")
            )
            lines.append(f"{when} · {escape(_shorten(occurrence.summary, title_limit))}")
        compact = header + compact_note + "\n\n" + "\n".join(lines) + warning
        if _fits(compact):
            return compact

    footer = (
        "\n\n⚠️ Не поместилось событий: {count}. Для полного списка отключите "
        "«Режим каталога» в /settings, выберите 7 дней и снова откройте «Ближайшие события»."
    )
    visible = []
    for line in lines:
        candidate = header + compact_note + "\n\n" + "\n".join([*visible, line])
        if not _fits(candidate + warning + footer.format(count=len(rows))):
            break
        visible.append(line)
    return (
        header
        + compact_note
        + "\n\n"
        + "\n".join(visible)
        + warning
        + footer.format(count=len(rows) - len(visible))
    )
