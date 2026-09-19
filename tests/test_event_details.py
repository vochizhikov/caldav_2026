import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from html.parser import HTMLParser
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from icalendar import Calendar
from icalendar import Event as ICalendarEvent
from sqlalchemy import select

from bot.handlers.events.user_routers import show_events
from bot.services.synchronizer import apply_snapshot
from bot.services.user_notifier import UserNotifier, format_notification
from bot.utils.common import event_text
from CalendarClient.client import parse_snapshot
from CalendarClient.event_details import meeting_link, meeting_location
from CalendarClient.models import CalendarSnapshot
from db.models import Event, MyCalendar, Notification, Occurrence, User

TELEMOST = "https://telemost.360.yandex.ru/j/7958899007"


def make_component(**fields):
    component = ICalendarEvent()
    component.add("UID", "details-test")
    component.add("DTSTART", datetime(2026, 9, 20, 4, tzinfo=UTC))
    component.add("DTEND", datetime(2026, 9, 20, 5, tzinfo=UTC))
    component.add("SUMMARY", "проба_1")
    for key, value in fields.items():
        component.add(key, value)
    return component


def parse(*components):
    calendar = Calendar()
    calendar.add("VERSION", "2.0")
    for component in components:
        calendar.add_component(component)
    return parse_snapshot(
        [calendar.to_ical().decode()],
        "Europe/Moscow",
        datetime(2026, 9, 19, tzinfo=UTC),
        datetime(2026, 9, 24, tzinfo=UTC),
    )


@pytest.mark.parametrize(
    "url",
    [
        TELEMOST,
        "https://telemost.yandex.ru/j/1234567890",
        "https://us02web.zoom.us/j/123456789?pwd=abc&from=calendar",
        "https://meet.google.com/abc-defg-hij",
        "https://teams.microsoft.com/l/meetup-join/19%3ameeting",
        "https://teams.live.com/meet/123456789?p=example",
        "https://example.webex.com/meet/alice",
        "https://meet.jit.si/team-room",
    ],
)
def test_first_meeting_link_skips_documents_and_preserves_url(url):
    component = make_component(
        description=f"Документ https://docs.example.test/agenda. Встреча ({url}). Ещё {TELEMOST}"
    )
    assert meeting_link(component) == url


@pytest.mark.parametrize(
    "value",
    [
        "",
        "Без видеовстречи",
        "https://example.test/document",
        "https://telemost.360.yandex.ru/",
        "https://zoom.us/pricing",
        "https://telemost.360.yandex.ru.evil.test/j/123",
        "https://telemost.360.yandex.ru@evil.test/j/123",
        "javascript:alert(1)",
    ],
)
def test_no_meeting_link_for_empty_unrelated_or_lookalike_urls(value):
    assert meeting_link(make_component(description=value)) == ""


@pytest.mark.parametrize("field", ["description", "url", "location", "conference", "X-ALT-DESC"])
def test_meeting_link_is_read_from_event_fields(field):
    assert meeting_link(make_component(**{field: TELEMOST})) == TELEMOST


def test_html_link_and_explicit_custom_conference():
    url = "https://us02web.zoom.us/j/123?pwd=one&from=calendar"
    component = make_component(description=f'<a href="{url.replace("&", "&amp;")}">Войти</a>')
    assert meeting_link(component) == url
    assert meeting_link(make_component(conference="https://video.example.test/room")) == (
        "https://video.example.test/room"
    )


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Купертино", "Купертино"),
        ("Офис (2 этаж)", "Офис (2 этаж)"),
        (f"Купертино ({TELEMOST})", "Купертино"),
        (TELEMOST, ""),
        (f"({TELEMOST}).", ""),
        ("  \n ", ""),
    ],
)
def test_room_is_optional_and_does_not_repeat_a_link(location, expected):
    assert meeting_location(make_component(location=location)) == expected


def test_recurring_meetings_keep_room_and_detached_override_link():
    master = make_component(location="Купертино", description=TELEMOST)
    master.add("RRULE", {"FREQ": "DAILY", "COUNT": 3})
    override = make_component(
        location="Сан-Хосе", description="https://meet.google.com/abc-defg-hij"
    )
    override["DTSTART"] = master["DTSTART"].__class__(datetime(2026, 9, 21, 8, tzinfo=UTC))
    override["DTEND"] = master["DTEND"].__class__(datetime(2026, 9, 21, 9, tzinfo=UTC))
    override.add("RECURRENCE-ID", datetime(2026, 9, 21, 4, tzinfo=UTC))
    snapshot = parse(master, override)
    assert snapshot.events[0].location == "Купертино"
    assert snapshot.events[0].meeting_url == TELEMOST
    assert [(item.location, item.meeting_url) for item in snapshot.occurrences] == [
        ("Купертино", TELEMOST),
        ("Сан-Хосе", "https://meet.google.com/abc-defg-hij"),
        ("Купертино", TELEMOST),
    ]


class TelegramHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.urls = []
        self.plain = []

    def handle_starttag(self, tag, attrs):
        assert tag in {"b", "i", "a"}
        self.tags.append(tag)
        if tag == "a":
            self.urls.append(dict(attrs)["href"])

    def handle_endtag(self, tag):
        assert self.tags.pop() == tag

    def handle_data(self, data):
        self.plain.append(data)


def test_event_card_uses_full_date_optional_fields_and_safe_html():
    url = "https://us02web.zoom.us/j/123?pwd=one&from=calendar"
    message = event_text(
        'проба_1 <b> & "тест"',
        datetime(2026, 9, 20, 4, tzinfo=UTC),
        False,
        "Europe/Moscow",
        location="Купертино <2>",
        meeting_url=url,
        calendar_name="Работа & задачи",
    )
    assert message.startswith("🗒 <b>проба_1 &lt;b&gt; &amp;")
    assert "⏱️ 20.09.2026 07:00 · <i>Europe/Moscow</i>" in message
    assert "📍 Купертино &lt;2&gt;" in message
    assert "☎️ <a href=" in message
    assert "📅 Календарь: Работа &amp; задачи" in message
    parsed = TelegramHTML()
    parsed.feed(message)
    assert not parsed.tags and parsed.urls == [url]


def test_event_card_hides_missing_room_link_and_includes_all_day_time():
    message = event_text(
        "Без переговорки", datetime(2026, 9, 19, 21, tzinfo=UTC), True, "Europe/Moscow"
    )
    assert "⏱️ 20.09.2026 00:00 · весь день · <i>Europe/Moscow</i>" in message
    assert "📍" not in message and "☎️" not in message
    assert "☎️" not in event_text(
        "Встреча",
        datetime(2026, 9, 20, tzinfo=UTC),
        False,
        "Europe/Moscow",
        meeting_url="javascript:alert(1)",
    )


async def apply(sessions, account, snapshot, now):
    async with sessions.begin() as session:
        await apply_snapshot(
            session,
            await session.get(MyCalendar, account[1]),
            await session.get(User, account[0]),
            snapshot,
            now,
        )


async def test_backfill_is_silent_and_deleted_notification_retains_details(sessions, account, now):
    rich = parse(make_component(location="Купертино", description=TELEMOST))
    legacy = CalendarSnapshot(
        [replace(rich.events[0], location="", meeting_url="")],
        [replace(rich.occurrences[0], meeting_url="")],
    )
    await apply(sessions, account, legacy, now)
    await apply(sessions, account, rich, now + timedelta(seconds=1))
    async with sessions() as session:
        saved = await session.scalar(select(Event))
        occurrence = await session.scalar(select(Occurrence))
        assert saved.location == "Купертино" and saved.meeting_url == TELEMOST
        assert occurrence.meeting_url == TELEMOST
        assert await session.scalar(select(Notification.id)) is None
    await apply(sessions, account, CalendarSnapshot(), now + timedelta(seconds=2))
    async with sessions() as session:
        item = await session.scalar(select(Notification))
        assert item.kind == "deleted"
        text = format_notification(item, await session.get(User, account[0]))
        assert "📍 Купертино" in text and TELEMOST in text


async def test_created_updated_and_event_list_use_details(sessions, account, now, monkeypatch):
    await apply(sessions, account, CalendarSnapshot(), now)
    await apply(
        sessions, account, parse(make_component(location="Купертино", description=TELEMOST)), now
    )
    changed_url = "https://meet.google.com/abc-defg-hij"
    await apply(
        sessions,
        account,
        parse(make_component(location="Сан-Хосе", description=changed_url)),
        now + timedelta(seconds=1),
    )
    async with sessions() as session:
        user = await session.get(User, account[0])
        items = list(await session.scalars(select(Notification).order_by(Notification.id)))
        assert [item.kind for item in items] == ["created", "updated"]
        assert "📍 Купертино" in format_notification(items[0], user)
        assert TELEMOST in format_notification(items[0], user)
        updated = format_notification(items[1], user)
        assert "📍 Сан-Хосе" in updated and changed_url in updated and "↩️ <b>Ранее:</b>" in updated
        monkeypatch.setattr("bot.handlers.events.user_routers.utcnow", lambda: now)
        message = SimpleNamespace(answer=AsyncMock())
        await show_events(message, session, user)
        card = message.answer.call_args_list[-2].args[0]
        assert "📍 Сан-Хосе" in card and changed_url in card
        assert "reply_markup" not in message.answer.call_args_list[-2].kwargs
        assert message.answer.call_args.args[0] == "🏠 <b>Главное меню</b>"
        assert (
            message.answer.call_args.kwargs["reply_markup"].inline_keyboard[0][0].callback_data
            == "menu:calendars"
        )


@pytest.mark.parametrize("offset", [0, 1, 3, 5, 10])
async def test_queued_reminder_uses_latest_room_and_link(sessions, account, settings, now, offset):
    rich = parse(make_component(location="Купертино", description=TELEMOST))
    rich = CalendarSnapshot(
        rich.events,
        [
            replace(
                rich.occurrences[0],
                starts_at=now + timedelta(minutes=offset),
                ends_at=now + timedelta(hours=1),
            )
        ],
    )
    await apply(sessions, account, rich, now - timedelta(seconds=1))
    async with sessions.begin() as session:
        (await session.get(User, account[0])).advance_minutes = offset or None
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    async with sessions.begin() as session:
        occurrence = await session.scalar(select(Occurrence))
        occurrence.location = "Сан-Хосе"
        occurrence.meeting_url = "https://meet.google.com/abc-defg-hij"
    await notifier.deliver_pending(now)
    text = bot.send_message.call_args.args[1]
    expected = f"🔔 Встреча через {offset} мин." if offset else "🔔 Встреча начинается"
    assert text.startswith(f"<b>{expected}</b>")
    assert '☎️ <a href="https://meet.google.com/abc-defg-hij">' in text
    assert "📍 Сан-Хосе" in text and "https://meet.google.com/abc-defg-hij" in text
    assert TELEMOST not in text


def test_legacy_notification_payload_without_new_fields_remains_readable(now):
    item = Notification(
        kind="created",
        payload=json.dumps(
            {
                "summary": "Старая запись",
                "starts_at": now.isoformat(),
                "all_day": False,
                "calendar": "Рабочий",
            }
        ),
    )
    text = format_notification(item, User(timezone="Europe/Moscow"))
    assert "🗒 <b>Старая запись</b>" in text
    assert "📍" not in text and "☎️" not in text
