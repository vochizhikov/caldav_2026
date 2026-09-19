from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, SendMessage
from aiogram.types import Message, Update
from sqlalchemy import func, select

from bot.dispatcher import create_dispatcher
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from bot.states.states import ConnectAccount, EditSettings
from CalendarClient import CalendarConnectionError
from CalendarClient.models import CalendarSnapshot, RemoteCalendar
from db.models import MyCalendar, Notification, Occurrence, User


class FakeTelegram(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):
        self.calls.append(method)
        if isinstance(method, (DeleteMessage, AnswerCallbackQuery)):
            return True
        return Message(
            message_id=100,
            date=datetime.now(UTC),
            chat={"id": method.chat_id, "type": "private"},
            text=getattr(method, "text", ""),
        )

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True
    ):
        yield b""


def telegram_message(text, update_id, user_id=555):
    return Update.model_validate(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 1789747200,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "chat": {"id": user_id, "type": "private"},
                "text": text,
                "entities": (
                    [{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                    if text.startswith("/")
                    else []
                ),
            },
        }
    )


def telegram_callback(data, update_id, user_id=555, reply_markup=None):
    return Update.model_validate(
        {
            "update_id": update_id,
            "callback_query": {
                "id": str(update_id),
                "chat_instance": "test",
                "data": data,
                "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
                "message": {
                    "message_id": 100,
                    "date": 1789747200,
                    "chat": {"id": user_id, "type": "private"},
                    "reply_markup": reply_markup,
                },
            },
        }
    )


def buttons_by_callback(message):
    return {
        button.callback_data: button.text
        for row in message.reply_markup.inline_keyboard
        for button in row
    }


@pytest.mark.parametrize("connected", [False, True])
@pytest.mark.parametrize("entry_point", ["/start", "/help", "/cancel", "/menu", "menu:home"])
async def test_main_menu_reflects_account_connection(sessions, settings, connected, entry_point):
    if connected:
        async with sessions.begin() as session:
            session.add(
                User(
                    telegram_id=555,
                    chat_id=555,
                    yandex_login="test@yandex.ru",
                    encrypted_password="encrypted",
                )
            )
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    update = (
        telegram_message(entry_point, 1)
        if entry_point.startswith("/")
        else telegram_callback(entry_point, 1)
    )
    try:
        await dispatcher.feed_update(bot, update, settings=settings)
        buttons = buttons_by_callback(transport.calls[-1])
        assert buttons["menu:events"] == "🗓 Ближайшие события"
        if connected:
            assert list(buttons) == ["menu:calendars", "menu:events"]
        else:
            assert list(buttons) == ["menu:calendars", "menu:events", "menu:connect"]
            assert buttons["menu:connect"] == "🔗 Подключить Яндекс Аккаунт"
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("entry_point", ["/connect", "menu:connect"])
async def test_connected_account_requires_disconnect_before_connect(
    sessions, settings, entry_point
):
    async with sessions.begin() as session:
        session.add(
            User(
                telegram_id=555,
                chat_id=555,
                yandex_login="existing@yandex.ru",
                encrypted_password="existing-encrypted-password",
            )
        )
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = AsyncMock()
    vault = AsyncMock()
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    context = dict(calendar_client=client, vault=vault, settings=settings)
    update = (
        telegram_message(entry_point, 1)
        if entry_point.startswith("/")
        else telegram_callback(entry_point, 1)
    )
    try:
        await dispatcher.feed_update(bot, update, **context)
        reply = transport.calls[-1]
        assert "/disconnect" in reply.text
        assert "menu:connect" not in buttons_by_callback(reply)
        state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
        assert await state.get_state() is None
        for index, value in enumerate(["replacement@yandex.ru", "replacement-password"], 2):
            await dispatcher.feed_update(bot, telegram_message(value, index), **context)
        client.calendars.assert_not_awaited()
        vault.encrypt.assert_not_called()
        async with sessions() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 555))
            assert user.yandex_login == "existing@yandex.ru"
            assert user.encrypted_password == "existing-encrypted-password"
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_onboarding_settings_and_account_isolation(sessions, settings):
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = AsyncMock()
    client.calendars.return_value = [RemoteCalendar("https://caldav.yandex.ru/cal/test", "Рабочий")]
    client.snapshot.return_value = CalendarSnapshot()
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    context = dict(
        calendar_client=client, vault=vault, synchronizer=synchronizer, settings=settings
    )
    try:
        await dispatcher.feed_update(bot, telegram_message("/start", 0), **context)
        assert (
            buttons_by_callback(transport.calls[-1])["menu:connect"]
            == "🔗 Подключить Яндекс Аккаунт"
        )
        for index, text in enumerate(["/connect", "test@yandex.ru", "app-password"], 1):
            await dispatcher.feed_update(bot, telegram_message(text, index), **context)
        assert any(isinstance(call, DeleteMessage) for call in transport.calls)
        assert (
            buttons_by_callback(transport.calls[-1])["account:disconnect"]
            == "🔌 Отключить Яндекс аккаунт"
        )
        await dispatcher.feed_update(bot, telegram_message("/menu", 4), **context)
        assert "menu:connect" not in buttons_by_callback(transport.calls[-1])
        async with sessions() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 555))
            assert vault.decrypt(user.encrypted_password) == "app-password"
            assert user.encrypted_password != "app-password"
            calendar = await session.scalar(select(MyCalendar))
            assert not calendar.enabled and not calendar.initialized
            calendar_id = calendar.id
        client.snapshot.assert_not_awaited()
        await dispatcher.feed_update(bot, telegram_callback("menu:calendars", 5), **context)
        calendar_rows = transport.calls[-1].reply_markup.inline_keyboard
        assert [row[0].callback_data for row in calendar_rows[-4:]] == [
            "calendar:sync",
            "menu:settings",
            "account:disconnect",
            "menu:home",
        ]
        settings_button = calendar_rows[-3][0]
        assert settings_button.text == "⚙️ Настройки уведомлений"
        await dispatcher.feed_update(
            bot, telegram_callback(settings_button.callback_data, 6), **context
        )
        back_button = transport.calls[-1].reply_markup.inline_keyboard[-1][0]
        assert back_button.text == "← Мои календари"
        await dispatcher.feed_update(
            bot, telegram_callback(back_button.callback_data, 7), **context
        )
        assert f"calendar:toggle:{calendar_id}" in buttons_by_callback(transport.calls[-1])
        await dispatcher.feed_update(bot, telegram_callback("settings:advance:10", 10), **context)
        await dispatcher.feed_update(
            bot, telegram_callback("settings:toggle:remind_at_start", 11), **context
        )
        await dispatcher.feed_update(bot, telegram_callback("settings:advance:7", 12), **context)
        await dispatcher.feed_update(
            bot, telegram_callback(f"calendar:toggle:{calendar_id}", 13, user_id=777), **context
        )
        async with sessions() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 555))
            assert user.advance_minutes == 10 and not user.remind_at_start
            assert not (await session.get(MyCalendar, calendar_id)).enabled
        await dispatcher.feed_update(
            bot, telegram_callback(f"calendar:toggle:{calendar_id}", 14), **context
        )
        for index, text in enumerate(["/settings", "/calendars", "/events", "/sync"], 20):
            await dispatcher.feed_update(bot, telegram_message(text, index), **context)
        async with sessions() as session:
            calendar = await session.get(MyCalendar, calendar_id)
            assert calendar.enabled and calendar.initialized and calendar.notifications_since
        assert not any(
            "Не удалось выполнить" in (getattr(call, "text", "") or "") for call in transport.calls
        )
        await dispatcher.feed_update(
            bot, telegram_callback("account:disconnect:confirm", 30), **context
        )
        assert (
            buttons_by_callback(transport.calls[-1])["menu:connect"]
            == "🔗 Подключить Яндекс Аккаунт"
        )
        async with sessions() as session:
            assert await session.scalar(select(func.count(MyCalendar.id))) == 0
            user = await session.scalar(select(User).where(User.telegram_id == 555))
            assert user.encrypted_password is None
        await dispatcher.feed_update(bot, telegram_callback("menu:connect", 31), **context)
        state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
        assert await state.get_state() == ConnectAccount.login.state
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_upcoming_settings_persist_and_leave_notifications_unchanged(
    sessions, settings, account, now
):
    telegram_id = 5_000_000_001
    async with sessions.begin() as session:
        session.add(
            Notification(
                user_id=account[0],
                calendar_id=account[1],
                kind="reminder",
                dedupe_key="pending-reminder",
                payload="{}",
                expires_at=now + timedelta(hours=1),
            )
        )
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)

    async def click(data, update_id, user_id=telegram_id, reply_markup=None):
        await dispatcher.feed_update(
            bot, telegram_callback(data, update_id, user_id, reply_markup), settings=settings
        )

    try:
        await click("menu:settings", 1)
        assert (
            buttons_by_callback(transport.calls[-1])["settings:upcoming"]
            == "🗓 Настройка ближайших событий"
        )
        state = dispatcher.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
        await state.set_state(EditSettings.timezone)
        await click("settings:upcoming", 2)
        assert await state.get_state() is None
        reply = transport.calls[-1]
        initial_keyboard = reply.reply_markup
        assert "Дни без встреч пропускаются" in reply.text
        assert "Выбрано дней со встречами: <b>1</b>" in reply.text
        assert "Europe/Moscow" in reply.text
        buttons = buttons_by_callback(reply)
        assert buttons["settings:upcoming:days:1"] == "✅ 1 день"
        assert list(buttons) == [
            "settings:upcoming:catalog:on",
            *(f"settings:upcoming:days:{days}" for days in range(1, 8)),
            "menu:settings",
        ]
        assert len(reply.reply_markup.inline_keyboard[0]) == 1
        assert buttons["menu:settings"] == "← Настройки уведомлений"
        for update_id, days in enumerate([2, 3, 4, 5, 6, 7, 1, 4], 3):
            await click(f"settings:upcoming:days:{days}", update_id)
            reply = transport.calls[-1]
            buttons = buttons_by_callback(reply)
            assert f"Выбрано дней со встречами: <b>{days}</b>" in reply.text
            selected = [
                key
                for key, text in buttons.items()
                if key.startswith("settings:upcoming:days:") and text.startswith("✅")
            ]
            assert selected == [f"settings:upcoming:days:{days}"]
            async with sessions() as session:
                assert (await session.get(User, account[0])).upcoming_event_days == days
        # An older message must refresh even if another message already saved this value.
        await click("settings:upcoming:days:4", 15, reply_markup=initial_keyboard)
        assert buttons_by_callback(transport.calls[-1])["settings:upcoming:days:4"] == "✅ 4 дня"
        current_keyboard = transport.calls[-1].reply_markup
        await click("settings:upcoming:days:4", 16, reply_markup=current_keyboard)
        assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        assert transport.calls[-1].text == "ℹ️ Уже выбрано"
        # An old message may already show the new choice: save without an identical edit.
        await click("settings:upcoming:days:1", 17, reply_markup=initial_keyboard)
        assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        assert transport.calls[-1].text == "✅ Сохранено"
        async with sessions() as session:
            assert (await session.get(User, account[0])).upcoming_event_days == 1
        await click("settings:upcoming:days:4", 18, reply_markup=initial_keyboard)
        for update_id, invalid in enumerate(["0", "8", "bad", "01"], 21):
            await click(f"settings:upcoming:days:{invalid}", update_id)
            assert isinstance(transport.calls[-1], AnswerCallbackQuery)
            assert "от 1 до 7" in transport.calls[-1].text
        await click("settings:upcoming:days:2", 25, user_id=777)
        await click("settings:upcoming", 26)
        assert buttons_by_callback(transport.calls[-1])["settings:upcoming:days:4"] == "✅ 4 дня"
        back = transport.calls[-1].reply_markup.inline_keyboard[-1][0]
        await click(back.callback_data, 27)
        assert "settings:toggle:notifications_enabled" in buttons_by_callback(transport.calls[-1])
        async with sessions() as session:
            user = await session.get(User, account[0])
            assert user.upcoming_event_days == 4 and user.advance_minutes == 5
            assert user.notifications_enabled and user.remind_at_start
            assert (await session.scalar(select(Notification))).status == "pending"
            other = await session.scalar(select(User).where(User.telegram_id == 777))
            assert other.upcoming_event_days == 2
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_upcoming_catalog_setting_persists_and_handles_old_keyboards(
    sessions, settings, account, now
):
    telegram_id = 5_000_000_001
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.upcoming_event_days = 3
        session.add(
            Notification(
                user_id=account[0],
                calendar_id=account[1],
                kind="reminder",
                dedupe_key="catalog-pending-reminder",
                payload="{}",
                expires_at=now + timedelta(hours=1),
            )
        )
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)

    async def click(data, update_id, user_id=telegram_id, reply_markup=None):
        await dispatcher.feed_update(
            bot, telegram_callback(data, update_id, user_id, reply_markup), settings=settings
        )

    try:
        await click("settings:upcoming", 1)
        reply = transport.calls[-1]
        initial_keyboard = reply.reply_markup
        assert "Режим показа: <b>Список</b>" in reply.text
        assert "Каталог — события одного дня в одном сообщении" in reply.text
        assert "обновляя это же сообщение" in reply.text
        assert buttons_by_callback(reply)["settings:upcoming:catalog:on"] == "⬜ Режим каталога"
        assert [button.callback_data for button in initial_keyboard.inline_keyboard[0]] == [
            "settings:upcoming:catalog:on"
        ]
        state = dispatcher.fsm.get_context(bot=bot, chat_id=telegram_id, user_id=telegram_id)
        await state.set_state(EditSettings.timezone)
        await click("settings:upcoming:catalog:on", 2, reply_markup=initial_keyboard)
        assert await state.get_state() is None
        reply = transport.calls[-1]
        assert isinstance(reply, EditMessageText)
        assert "Режим показа: <b>Каталог</b>" in reply.text
        assert "Каталог: <b>7 дней со встречами</b>" in reply.text
        assert "Выбрано дней со встречами" not in reply.text
        assert buttons_by_callback(reply)["settings:upcoming:catalog:off"] == "✅ Режим каталога"
        assert [
            [button.callback_data for button in row] for row in reply.reply_markup.inline_keyboard
        ] == [["settings:upcoming:catalog:off"], ["menu:settings"]]
        enabled_keyboard = reply.reply_markup
        # Repeated and old callbacks select the explicit mode, without toggling it back.
        await click("settings:upcoming:catalog:on", 3, reply_markup=enabled_keyboard)
        assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        assert transport.calls[-1].text == "ℹ️ Уже выбрано"
        await click("settings:upcoming:catalog:on", 4, reply_markup=initial_keyboard)
        assert isinstance(transport.calls[-1], EditMessageText)
        assert "Режим показа: <b>Каталог</b>" in transport.calls[-1].text
        # If an old message already shows the desired mode, only persistence changes.
        await click("settings:upcoming:catalog:off", 5, reply_markup=initial_keyboard)
        assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        assert transport.calls[-1].text == "✅ Сохранено"
        async with sessions() as session:
            assert not (await session.get(User, account[0])).upcoming_catalog_mode
        await click("settings:upcoming:catalog:invalid", 6)
        assert isinstance(transport.calls[-1], AnswerCallbackQuery)
        assert transport.calls[-1].text == "⚠️ Неизвестный режим показа"
        await click("settings:upcoming", 7)
        assert "Режим показа: <b>Список</b>" in transport.calls[-1].text
        assert "Выбрано дней со встречами: <b>3</b>" in transport.calls[-1].text
        assert buttons_by_callback(transport.calls[-1])["settings:upcoming:days:3"] == "✅ 3 дня"
        await click("settings:upcoming:catalog:on", 8)
        # Day buttons in an old list message cannot change the saved list preference in catalog.
        before_click = len(transport.calls)
        await click("settings:upcoming:days:7", 9, reply_markup=initial_keyboard)
        replies = transport.calls[before_click:]
        answer = next(call for call in replies if isinstance(call, AnswerCallbackQuery))
        assert answer.text == "📖 В режиме каталога всегда 7 дней со встречами."
        assert isinstance(transport.calls[-1], EditMessageText)
        assert "Каталог: <b>7 дней со встречами</b>" in transport.calls[-1].text
        assert list(buttons_by_callback(transport.calls[-1])) == [
            "settings:upcoming:catalog:off",
            "menu:settings",
        ]
        assert (
            buttons_by_callback(transport.calls[-1])["settings:upcoming:catalog:off"]
            == "✅ Режим каталога"
        )
        await click("settings:upcoming:catalog:on", 10, user_id=777)
        await click("settings:upcoming:catalog:off", 11, user_id=777)
        await click("settings:upcoming", 12)
        assert "Режим показа: <b>Каталог</b>" in transport.calls[-1].text
        back = transport.calls[-1].reply_markup.inline_keyboard[-1][0]
        assert back.text == "← Настройки уведомлений"
        await click(back.callback_data, 13)
        assert "settings:toggle:notifications_enabled" in buttons_by_callback(transport.calls[-1])
        async with sessions() as session:
            user = await session.get(User, account[0])
            assert user.upcoming_catalog_mode and user.upcoming_event_days == 3
            assert user.notifications_enabled and user.remind_at_start and user.advance_minutes == 5
            assert (await session.scalar(select(Notification))).status == "pending"
            other = await session.scalar(select(User).where(User.telegram_id == 777))
            assert not other.upcoming_catalog_mode and other.upcoming_event_days == 1
        await click("settings:upcoming:catalog:off", 14)
        assert "Выбрано дней со встречами: <b>3</b>" in transport.calls[-1].text
        assert buttons_by_callback(transport.calls[-1])["settings:upcoming:days:3"] == "✅ 3 дня"
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("entry_point", ["/events", "menu:events"])
@pytest.mark.parametrize("days", [1, 3])
async def test_events_show_selected_days_then_main_menu(
    sessions, settings, account, now, monkeypatch, entry_point, days
):
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.upcoming_event_days = days
        for day_index, offset in enumerate([1, 5, 9, 13], 1):
            for event_index in range(12):
                start = now + timedelta(days=offset, minutes=event_index)
                session.add(
                    Occurrence(
                        calendar_id=account[1],
                        remote_key=f"{day_index}-{event_index}",
                        summary=f"День {day_index}, встреча {event_index}: " + "Тема " * 40,
                        starts_at=start,
                        ends_at=start + timedelta(hours=1),
                    )
                )
    monkeypatch.setattr("bot.handlers.events.user_routers.utcnow", lambda: now)
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    update = (
        telegram_message(entry_point, 1, user_id=5_000_000_001)
        if entry_point.startswith("/")
        else telegram_callback(entry_point, 1, user_id=5_000_000_001)
    )
    try:
        await dispatcher.feed_update(bot, update, settings=settings)
        messages = [call for call in transport.calls if isinstance(call, SendMessage)]
        assert len(messages) >= 4  # Header, multiple event chunks, then the main menu.
        assert messages[-1].text == "🏠 <b>Главное меню</b>"
        assert list(buttons_by_callback(messages[-1])) == ["menu:calendars", "menu:events"]
        assert all(message.reply_markup is None for message in messages[:-1])
        event_text = "\n".join(message.text for message in messages[:-1])
        for day_index in range(1, 5):
            for event_index in range(12):
                assert (f"День {day_index}, встреча {event_index}:" in event_text) == (
                    day_index <= days
                )
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("entry_point", ["/events", "menu:events"])
@pytest.mark.parametrize("connected", [False, True])
async def test_events_without_results_still_send_main_menu(
    sessions, settings, entry_point, connected
):
    if connected:
        async with sessions.begin() as session:
            session.add(User(telegram_id=555, chat_id=555, encrypted_password="encrypted"))
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    update = (
        telegram_message(entry_point, 1)
        if entry_point.startswith("/")
        else telegram_callback(entry_point, 1)
    )
    try:
        await dispatcher.feed_update(bot, update, settings=settings)
        messages = [call for call in transport.calls if isinstance(call, SendMessage)]
        assert len(messages) == 2 and messages[0].reply_markup is None
        assert ("нет ближайших событий" if connected else "/connect") in messages[0].text
        assert messages[1].text == "🏠 <b>Главное меню</b>"
        assert ("menu:connect" in buttons_by_callback(messages[1])) == (not connected)
        assert "menu:home" not in buttons_by_callback(messages[1])
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


async def test_group_chat_cannot_connect(sessions, settings):
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    synchronizer = Synchronizer(sessions, AsyncMock(), AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    update = telegram_message("/connect", 1).model_dump()
    update["message"]["chat"] = {"id": -123, "type": "group"}
    try:
        await dispatcher.feed_update(bot, Update.model_validate(update), settings=settings)
        async with sessions() as session:
            assert await session.scalar(select(func.count(User.id))) == 0
    finally:
        await dispatcher.storage.close()
        await bot.session.close()


@pytest.mark.parametrize("code", ["authentication", "network", "internal"])
async def test_connection_failure_guides_user_without_echoing_password(sessions, settings, code):
    transport = FakeTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = AsyncMock()
    client.calendars.side_effect = CalendarConnectionError(f"Код: {code}.", code=code)
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    context = dict(
        calendar_client=client, vault=vault, synchronizer=synchronizer, settings=settings
    )
    try:
        for index, value in enumerate(["/connect", "test@yandex.ru", "synthetic-secret"]):
            await dispatcher.feed_update(bot, telegram_message(value, index), **context)
        state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
        expected = ConnectAccount.password.state if code == "authentication" else None
        assert await state.get_state() == expected
        replies = "\n".join(getattr(call, "text", "") or "" for call in transport.calls)
        assert f"Код: {code}" in replies and "synthetic-secret" not in replies
        assert "/connect" in replies
        assert any(isinstance(call, DeleteMessage) for call in transport.calls)
        async with sessions() as session:
            assert (await session.scalar(select(User))).encrypted_password is None
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
