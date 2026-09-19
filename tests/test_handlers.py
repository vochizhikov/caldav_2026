from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.methods import AnswerCallbackQuery, DeleteMessage
from aiogram.types import Message, Update
from sqlalchemy import func, select

from bot.dispatcher import create_dispatcher
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from bot.states.states import ConnectAccount
from CalendarClient import CalendarConnectionError
from CalendarClient.models import CalendarSnapshot, RemoteCalendar
from db.models import MyCalendar, User


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


def telegram_callback(data, update_id, user_id=555):
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
        assert {"menu:calendars", "menu:events", "menu:settings"} <= buttons.keys()
        if connected:
            assert "menu:connect" not in buttons
        else:
            assert buttons["menu:connect"] == "🔗 Подключить Яндекс аккаунт"
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
            == "🔗 Подключить Яндекс аккаунт"
        )
        for index, text in enumerate(["/connect", "test@yandex.ru", "app-password"], 1):
            await dispatcher.feed_update(bot, telegram_message(text, index), **context)
        assert any(isinstance(call, DeleteMessage) for call in transport.calls)
        assert (
            buttons_by_callback(transport.calls[-1])["account:disconnect"]
            == "Отключить Яндекс аккаунт"
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
            == "🔗 Подключить Яндекс аккаунт"
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
