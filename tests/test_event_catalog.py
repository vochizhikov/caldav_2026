import re
from datetime import date, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import AnswerCallbackQuery, EditMessageText, SendMessage
from aiogram.types import Update
from sqlalchemy import delete
from test_handlers import FakeTelegram, telegram_callback, telegram_message

from bot.dispatcher import create_dispatcher
from bot.keyboards.event_catalog import catalog_keyboard
from bot.services.synchronizer import Synchronizer
from bot.utils.event_catalog import CatalogCursor, catalog_text, timezone_key
from db.models import MyCalendar, Occurrence, User

TELEGRAM_ID = 5_000_000_001


@pytest.fixture
async def catalog_app(sessions, settings, account, now, monkeypatch):
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.upcoming_catalog_mode = True
        user.upcoming_event_days = 3
        for key, title, offset in (
            ("first", "Первый день", timedelta(minutes=1)),
            ("first-extra", "Ещё встреча первого дня", timedelta(hours=1)),
            ("second", "Второй день", timedelta(days=2)),
            ("third", "Третий день", timedelta(days=9)),
        ):
            session.add(
                Occurrence(
                    calendar_id=account[1],
                    remote_key=key,
                    summary=title,
                    starts_at=now + offset,
                    ends_at=now + offset + timedelta(hours=1),
                )
            )
    monkeypatch.setattr("bot.handlers.events.user_routers.utcnow", lambda: now)
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)

    async def click(data, *, message_id=501, user_id=TELEGRAM_ID):
        update = telegram_callback(data, 1, user_id).model_dump()
        update["callback_query"]["message"]["message_id"] = message_id
        transport.calls.clear()
        await dispatcher.feed_update(bot, Update.model_validate(update), settings=settings)

    async def open_catalog(command=False):
        transport.calls.clear()
        update = (
            telegram_message("/events", 1, TELEGRAM_ID)
            if command
            else telegram_callback("menu:events", 1, TELEGRAM_ID)
        )
        await dispatcher.feed_update(bot, update, settings=settings)

    try:
        yield SimpleNamespace(transport=transport, click=click, open=open_catalog)
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


def arrows(method):
    if method.reply_markup is None:
        return {}
    return {
        button.text: button.callback_data
        for row in method.reply_markup.inline_keyboard
        for button in row
    }


@pytest.mark.parametrize("command", [False, True])
@pytest.mark.parametrize("saved_days", [1, 3, 7])
async def test_catalog_pages_edit_the_same_message_and_have_boundary_arrows(
    catalog_app, sessions, account, command, saved_days
):
    app = catalog_app
    async with sessions.begin() as session:
        (await session.get(User, account[0])).upcoming_event_days = saved_days
    await app.open(command=command)
    sent = [call for call in app.transport.calls if isinstance(call, SendMessage)]
    assert len(sent) == 2
    first, menu = sent
    assert "Первый день" in first.text and "Ещё встреча первого дня" in first.text
    assert "Второй день" not in first.text and "Третий день" not in first.text
    assert "День 1 из 3" in first.text and list(arrows(first)) == ["→"]
    assert menu.text == "🏠 <b>Главное меню</b>"
    await app.click(arrows(first)["→"])
    second = app.transport.calls[-1]
    assert isinstance(second, EditMessageText) and second.message_id == 501
    assert second.chat_id == TELEGRAM_ID
    assert "Второй день" in second.text and "Первый день" not in second.text
    assert "День 2 из 3" in second.text and list(arrows(second)) == ["←", "→"]
    assert not any(isinstance(call, SendMessage) for call in app.transport.calls)
    await app.click(arrows(second)["→"])
    last = app.transport.calls[-1]
    assert isinstance(last, EditMessageText) and last.message_id == 501
    assert "Третий день" in last.text and "День 3 из 3" in last.text
    assert list(arrows(last)) == ["←"]
    await app.click(arrows(last)["←"])
    assert "Второй день" in app.transport.calls[-1].text
    await app.click(arrows(second)["←"])
    assert "Первый день" in app.transport.calls[-1].text
    assert list(arrows(app.transport.calls[-1])) == ["→"]


@pytest.mark.parametrize("saved_days", [1, 3, 7])
async def test_single_catalog_day_has_no_buttons(catalog_app, sessions, account, saved_days):
    async with sessions.begin() as session:
        (await session.get(User, account[0])).upcoming_event_days = saved_days
        await session.execute(
            delete(Occurrence).where(Occurrence.remote_key.in_(["second", "third"]))
        )
    await catalog_app.open()
    first = next(call for call in catalog_app.transport.calls if isinstance(call, SendMessage))
    assert "День 1 из 1" in first.text and first.reply_markup is None


@pytest.mark.parametrize("saved_days", [1, 3, 7])
async def test_catalog_always_shows_seven_nonempty_days_and_list_restores_saved_count(
    catalog_app, sessions, account, now, saved_days
):
    async with sessions.begin() as session:
        (await session.get(User, account[0])).upcoming_event_days = saved_days
        await session.execute(delete(Occurrence))
        for day_index in range(1, 9):
            session.add(
                Occurrence(
                    calendar_id=account[1],
                    remote_key=f"catalog-day-{day_index}",
                    summary=f"Каталожный день {day_index}",
                    starts_at=now + timedelta(days=day_index * 2),
                    ends_at=now + timedelta(days=day_index * 2, hours=1),
                )
            )
    await catalog_app.open()
    page = next(call for call in catalog_app.transport.calls if isinstance(call, SendMessage))
    for day_index in range(1, 8):
        assert f"День {day_index} из 7" in page.text
        for available_day in range(1, 9):
            assert (f"Каталожный день {available_day}" in page.text) == (available_day == day_index)
        if day_index < 7:
            await catalog_app.click(arrows(page)["→"])
            page = catalog_app.transport.calls[-1]
            assert isinstance(page, EditMessageText)
        else:
            assert list(arrows(page)) == ["←"]

    await catalog_app.click("settings:upcoming:catalog:off")
    async with sessions() as session:
        user = await session.get(User, account[0])
        assert not user.upcoming_catalog_mode and user.upcoming_event_days == saved_days
    await catalog_app.open(command=True)
    messages = [call for call in catalog_app.transport.calls if isinstance(call, SendMessage)]
    assert messages[-1].text == "🏠 <b>Главное меню</b>"
    assert all(message.reply_markup is None for message in messages[:-1])
    event_text = "\n".join(message.text for message in messages[:-1])
    for day_index in range(1, 9):
        assert (f"Каталожный день {day_index}" in event_text) == (day_index <= saved_days)


async def test_catalog_dates_survive_time_settings_and_sync_changes(
    catalog_app, sessions, account, now, monkeypatch
):
    app = catalog_app
    await app.open()
    first = next(call for call in app.transport.calls if isinstance(call, SendMessage))
    target = arrows(first)["→"]
    async with sessions.begin() as session:
        (await session.get(User, account[0])).upcoming_event_days = 1
        session.add(
            Occurrence(
                calendar_id=account[1],
                remote_key="inserted-between-days",
                summary="Новый промежуточный день",
                starts_at=now + timedelta(days=1),
                ends_at=now + timedelta(days=1, hours=1),
            )
        )
    # Open a newer catalog. Its selection must not replace the old message's dates.
    await app.open()
    monkeypatch.setattr("bot.handlers.events.user_routers.utcnow", lambda: now + timedelta(days=5))
    await app.click(target, message_id=777)
    page = app.transport.calls[-1]
    assert isinstance(page, EditMessageText) and page.message_id == 777
    assert "Второй день" in page.text and "Новый промежуточный день" not in page.text
    assert "20.09.2026" in page.text and "День 2 из 3" in page.text
    async with sessions.begin() as session:
        await session.execute(delete(Occurrence).where(Occurrence.remote_key == "second"))
    await app.click(target)
    page = app.transport.calls[-1]
    assert "20.09.2026" in page.text and "больше нет событий" in page.text
    assert list(arrows(page)) == ["←", "→"]


@pytest.mark.parametrize("reason", ["foreign", "disconnected", "timezone", "disabled"])
async def test_catalog_callbacks_check_current_access(catalog_app, sessions, account, reason):
    await catalog_app.open()
    first = next(call for call in catalog_app.transport.calls if isinstance(call, SendMessage))
    target = arrows(first)["→"]
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        if reason == "disconnected":
            user.encrypted_password = None
        elif reason == "timezone":
            user.timezone = "Asia/Tokyo"
        elif reason == "disabled":
            (await session.get(MyCalendar, account[1])).enabled = False
    await catalog_app.click(target, user_id=777 if reason == "foreign" else TELEGRAM_ID)
    last = catalog_app.transport.calls[-1]
    if reason == "disabled":
        assert isinstance(last, EditMessageText) and "больше нет событий" in last.text
    else:
        assert isinstance(last, AnswerCallbackQuery) and last.show_alert
        assert not any(isinstance(call, EditMessageText) for call in catalog_app.transport.calls)


async def test_empty_catalog_still_finishes_with_main_menu(catalog_app, sessions):
    async with sessions.begin() as session:
        await session.execute(delete(Occurrence))
    await catalog_app.open()
    sent = [call for call in catalog_app.transport.calls if isinstance(call, SendMessage)]
    assert len(sent) == 2 and sent[0].reply_markup is None
    assert "нет ближайших событий" in sent[0].text
    assert sent[1].text == "🏠 <b>Главное меню</b>"


async def test_repeated_catalog_click_ignores_identical_message_error(catalog_app, monkeypatch):
    await catalog_app.open()
    first = next(call for call in catalog_app.transport.calls if isinstance(call, SendMessage))
    original_request = catalog_app.transport.make_request

    async def request(bot, method, timeout=None):  # noqa: ASYNC109 -- BaseSession signature
        if isinstance(method, EditMessageText):
            raise TelegramBadRequest(method=method, message="Bad Request: message is not modified")
        return await original_request(bot, method, timeout=timeout)

    monkeypatch.setattr(catalog_app.transport, "make_request", request)
    await catalog_app.click(arrows(first)["→"])
    assert len(catalog_app.transport.calls) == 1
    assert isinstance(catalog_app.transport.calls[0], AnswerCallbackQuery)


def test_cursor_roundtrip_fits_telegram_limit_for_all_seven_dates(now):
    days = tuple(now.date() + timedelta(days=index * 4) for index in range(7))
    cursor = CatalogCursor(2**63 - 1, now, timezone_key("Europe/Moscow"), days)
    for index in range(7):
        data = cursor.callback(index)
        assert len(data.encode()) <= 64
        assert CatalogCursor.decode(data) == cursor.at(index)


@pytest.mark.parametrize("data", ["evcat:", "evcat:???", "evcat:" + "a" * 65, "wrong:abc"])
async def test_malformed_catalog_callbacks_are_rejected(catalog_app, data):
    if data.startswith("evcat:"):
        await catalog_app.click(data)
        assert isinstance(catalog_app.transport.calls[-1], AnswerCallbackQuery)
    with pytest.raises(ValueError):
        CatalogCursor.decode(data)


def test_cursor_rejects_out_of_range_index_and_dates(now):
    cursor = CatalogCursor(1, now, timezone_key("Europe/Moscow"), (now.date(),))
    with pytest.raises(ValueError):
        CatalogCursor.decode(cursor.callback(1))
    with pytest.raises(ValueError):
        CatalogCursor.decode(CatalogCursor(1, now, cursor.timezone_hash, (date.max,)).callback(0))
    assert catalog_keyboard(cursor) is None


@pytest.mark.parametrize("count", [125, 500])
def test_catalog_long_day_fits_one_message_without_silent_omissions(now, count):
    user = User(timezone="Europe/Moscow")
    cursor = CatalogCursor(1, now, timezone_key(user.timezone), (now.date(),))
    calendar = SimpleNamespace(name="Рабочий", last_error=None)
    rows = [
        (
            Occurrence(
                summary=f"T{index:03} " + "<&😀>" * 100,
                starts_at=now,
                all_day=False,
                location="",
                meeting_url="",
            ),
            calendar,
        )
        for index in range(count)
    ]
    text = catalog_text(rows, user, cursor)
    assert len(text.encode("utf-16-le")) // 2 <= 4096
    assert "Краткий вид" in text and "<&" not in text
    shown = len(re.findall(r"T\d{3}", text))
    if shown < count:
        assert f"Не поместилось событий: {count - shown}" in text
        assert "отключите «Режим каталога» в /settings" in text
    else:
        assert "Не поместилось" not in text
