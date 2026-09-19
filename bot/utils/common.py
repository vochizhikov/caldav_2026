from datetime import datetime
from html import escape
from zoneinfo import ZoneInfo

from CalendarClient.event_details import first_link


def event_text(
    summary: str,
    starts_at: datetime,
    all_day: bool,
    timezone: str,
    *,
    location: str = "",
    meeting_url: str = "",
    calendar_name: str = "",
) -> str:
    local = starts_at.astimezone(ZoneInfo(timezone))
    when = local.strftime("%d.%m.%Y %H:%M")
    suffix = f" · <i>{escape(timezone)}</i>"
    if all_day:
        suffix = " · весь день" + suffix
    lines = [f"🗒 <b>{escape(summary[:500])}</b>", f"⏱️ {when}{suffix}"]
    if location and location.strip():
        lines.append(f"📍 {escape(location.strip()[:300])}")
    if link := first_link(meeting_url or ""):
        safe_link = escape(link, quote=True)
        lines.append(f'☎️ <a href="{safe_link}">{safe_link}</a>')
    if calendar_name:
        lines.append(f"📅 Календарь: {escape(calendar_name[:200])}")
    return "\n".join(lines)
