import json
import logging
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram import Bot
from aiogram.exceptions import TelegramForbiddenError
from aiogram.methods import CreateForumTopic, SendDocument, SendMessage
from aiogram.types import ForumTopic
from sqlalchemy import select
from test_handlers import FakeTelegram, telegram_callback, telegram_message
from test_monitor_telegram import membership_update

from bot.dispatcher import create_dispatcher
from bot.observability import install_monitor
from bot.observability.errors import install_error_monitor
from bot.observability.service import MonitorService
from bot.observability.settings import MonitorSettings
from bot.observability.snapshots import SnapshotTools
from bot.observability.telegram import install_telegram_audit
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from CalendarClient import CalendarConnectionError
from db.models import User

ADMIN = 74529696
GROUP = -100987654321
PASSWORD = "synthetic-monitor-integration-password"


class IntegrationTelegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.documents = []
        self.forbidden_chat = None

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        if self.forbidden_chat and getattr(method, "chat_id", None) == self.forbidden_chat:
            raise TelegramForbiddenError(method=method, message="private-error-body")
        if isinstance(method, CreateForumTopic):
            self.calls.append(method)
            return ForumTopic(
                message_thread_id=len(self.calls), name=method.name, icon_color=7322096
            )
        if isinstance(method, SendDocument):
            self.documents.append(method.document.path.read_bytes())
        return await super().make_request(bot, method, timeout)


def pending(monitor):
    return [
        json.loads(row[0])
        for row in monitor._connection.execute("SELECT payload FROM pending ORDER BY id")
    ]


def errors(monitor):
    return [
        json.loads(row[0])
        for row in monitor._connection.execute("SELECT payload FROM errors ORDER BY fingerprint")
    ]


@pytest.fixture
async def monitored_application(sessions, settings, tmp_path):
    transport = IntegrationTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = SimpleNamespace(calendars=AsyncMock(return_value=[]), snapshot=AsyncMock())
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    monitor = MonitorService(MonitorSettings(_env_file=None, directory=tmp_path / "monitor"))
    snapshots = SnapshotTools(sessions.kw["bind"], sessions, monitor.directory / "snapshots")
    monitor._cleanups.extend(
        [
            install_error_monitor(client, bot, monitor),
            install_telegram_audit(dispatcher, bot, monitor, sessions, snapshots),
        ]
    )
    app = SimpleNamespace(
        transport=transport,
        bot=bot,
        client=client,
        dispatcher=dispatcher,
        monitor=monitor,
        snapshots=snapshots,
        sessions=sessions,
        vault=vault,
        context=dict(calendar_client=client, vault=vault, settings=settings),
    )
    yield app
    await monitor.close()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_real_start_emits_one_arrival_and_existing_users_get_silent_baseline(
    monitored_application,
):
    app = monitored_application
    async with app.sessions.begin() as session:
        session.add(User(telegram_id=777, chat_id=777, created_at=datetime(2026, 1, 1, tzinfo=UTC)))
    for index, text in enumerate(["/start", "private-routine-message", "/start", "/help"]):
        await app.dispatcher.feed_update(app.bot, telegram_message(text, index), **app.context)
    await app.dispatcher.feed_update(
        app.bot, telegram_message("/start", 10, user_id=777), **app.context
    )
    assert [item["event"] for item in pending(app.monitor)] == ["arrived"]
    profile = pending(app.monitor)[0]["profile"]
    assert profile["telegram_id"] == 555 and profile["first_name"] == "Test"
    assert profile["db_user"]["telegram_id"] == 555
    assert app.monitor.profile(777)["arrival_recorded"] is True
    persisted = "\n".join(app.monitor._connection.iterdump())
    assert "private-routine-message" not in persisted
    assert "Главное меню" not in persisted
    assert "Добро" not in persisted
    assert "my_chat_member" in app.dispatcher.resolve_used_update_types()
    assert errors(app.monitor) == []


async def test_block_unblock_updates_are_delivered_once_with_real_monitor(monitored_application):
    app = monitored_application
    await app.dispatcher.feed_update(app.bot, telegram_message("/start", 1), **app.context)
    for update in [
        membership_update("member", "kicked", 2, member_id=app.bot.id),
        membership_update("member", "kicked", 2, member_id=app.bot.id),
        membership_update("kicked", "member", 3, member_id=app.bot.id),
        membership_update("kicked", "member", 3, member_id=app.bot.id),
    ]:
        await app.dispatcher.feed_update(app.bot, update, **app.context)
    assert [item["event"] for item in pending(app.monitor)] == ["arrived", "left", "returned"]
    assert app.monitor.profile(555)["status"] == "active"


async def test_connect_disconnect_emit_transitions_without_storing_password(monitored_application):
    app = monitored_application
    for index, value in enumerate(["/start", "/connect", "integration@yandex.ru", PASSWORD]):
        await app.dispatcher.feed_update(app.bot, telegram_message(value, index), **app.context)
    await app.dispatcher.feed_update(
        app.bot, telegram_callback("account:disconnect:confirm", 4), **app.context
    )
    assert [item["event"] for item in pending(app.monitor)] == [
        "arrived",
        "yandex_connected",
        "yandex_disconnected",
    ]
    persisted = "\n".join(app.monitor._connection.iterdump())
    assert PASSWORD not in persisted and "encrypted_password" not in persisted
    assert app.monitor.profile(555)["db_user"]["connected"] is False
    async with app.sessions() as session:
        assert (await session.scalar(select(User))).encrypted_password is None


async def test_real_delivery_routes_users_and_safe_errors_without_api_mismatch(
    monitored_application,
    monkeypatch,
):
    app = monitored_application
    original_send = app.monitor._send

    async def send_without_pacing(*args):
        app.monitor._last_send = 0
        return await original_send(*args)

    monkeypatch.setattr(app.monitor, "_send", send_without_pacing)
    await app.monitor.setup(app.bot, GROUP)
    await app.dispatcher.feed_update(app.bot, telegram_message("/start", 1), **app.context)
    # The adapter must preserve user context and safe error details but omit bodies.
    original_calendar_call = app.client.calendars
    # The wrapped AsyncMock remains accessible through wraps.__wrapped__.
    original_calendar_call.__wrapped__.side_effect = CalendarConnectionError(
        "private-caldav-error-body", code="authentication"
    )
    with app.monitor.operation(user_id=555):
        with pytest.raises(CalendarConnectionError):
            await app.client.calendars("integration@yandex.ru", PASSWORD)
        logging.getLogger("bot.services.synchronizer").error(
            "Synchronization failed for user=%s (%s)", 7, "RuntimeError"
        )
    payloads = errors(app.monitor)
    caldav_error = next(item for item in payloads if item["source"] == "caldav")
    assert caldav_error["code"] == "authentication" and caldav_error["user_id"] == 555
    runtime_error = next(item for item in payloads if item["event"] == "runtime_error")
    assert runtime_error["db_user_id"] == 7
    assert runtime_error["filename"] == "test_monitor_integration.py"
    assert "private-caldav-error-body" not in json.dumps(payloads)
    for _ in range(4):
        app.monitor._last_send = 0
        # One user event and up to one error are sent by each real delivery pass.
        await app.monitor.deliver(app.bot)
    sent = [
        call
        for call in app.transport.calls
        if isinstance(call, SendMessage) and call.chat_id == GROUP
    ]
    topics = app.monitor._get("topics")
    assert {call.message_thread_id for call in sent} == {topics["users"], topics["errors"]}
    assert pending(app.monitor) == []
    assert any("Пользователь БД ID: 7" in call.text for call in sent)
    assert "private-caldav-error-body" not in "\n".join(call.text for call in sent)


async def test_forbidden_delivery_is_an_error_not_a_false_block_event(monitored_application):
    app = monitored_application
    await app.dispatcher.feed_update(app.bot, telegram_message("/start", 1), **app.context)
    app.transport.forbidden_chat = 555
    with pytest.raises(TelegramForbiddenError):
        await app.bot.send_message(555, "private-outgoing-content")
    assert [item["event"] for item in pending(app.monitor)] == ["arrived"]
    problem = next(item for item in errors(app.monitor) if item["event"] == "delivery_forbidden")
    assert problem["user_id"] == 555 and problem["method"] == "sendMessage"
    persisted = "\n".join(app.monitor._connection.iterdump())
    assert "private-error-body" not in persisted and "private-outgoing-content" not in persisted


async def test_private_admin_user_and_users_exports_use_real_tools_and_remove_files(
    monitored_application,
):
    app = monitored_application
    await app.dispatcher.feed_update(app.bot, telegram_message("/start", 1), **app.context)
    async with app.sessions.begin() as session:
        user = await session.scalar(select(User).where(User.telegram_id == 555))
        user.encrypted_password = app.vault.encrypt(PASSWORD)
    for index, command in enumerate(["/db_user 555", "/db_users"], 2):
        await app.dispatcher.feed_update(
            app.bot, telegram_message(command, index, user_id=ADMIN), **app.context
        )
    user_snapshot, users_snapshot = [json.loads(content) for content in app.transport.documents]
    assert user_snapshot["user"]["telegram_id"] == 555
    assert "notifications" not in user_snapshot
    assert user_snapshot["telegram_profile"]["first_name"] == "Test"
    assert [user["telegram_id"] for user in users_snapshot["users"]] == [555]
    assert users_snapshot["users"][0]["connected"] is True
    assert not list(app.snapshots.directory.iterdir())
    assert PASSWORD.encode() not in b"".join(app.transport.documents)
    assert b"encrypted_password" not in b"".join(app.transport.documents)
    # Admin commands bypass normal session creation and lifecycle notifications.
    assert [item["event"] for item in pending(app.monitor)] == ["arrived"]


async def test_full_installer_disabled_has_no_storage_or_hooks(
    sessions, settings, tmp_path, monkeypatch
):
    import bot.observability.settings as monitor_settings_module

    directory = tmp_path / "disabled-monitor"
    monkeypatch.setenv("MONITOR_ENABLED", "false")
    monkeypatch.setenv("MONITOR_DIRECTORY", str(directory))
    monkeypatch.setattr(
        monitor_settings_module, "MonitorSettings", lambda: MonitorSettings(_env_file=None)
    )
    transport = IntegrationTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = SimpleNamespace(calendars=AsyncMock(), snapshot=AsyncMock())
    synchronizer = Synchronizer(sessions, client, AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    before = (
        dict(sessions.kw),
        list(dispatcher.update.outer_middleware),
        list(bot.session.middleware),
        list(dispatcher.my_chat_member.handlers),
        list(logging.getLogger().handlers),
        client.calendars,
        client.snapshot,
    )
    try:
        assert (
            install_monitor(
                bot=bot,
                dispatcher=dispatcher,
                engine=sessions.kw["bind"],
                sessions=sessions,
                client=client,
                settings=settings,
            )
            is None
        )
        after = (
            dict(sessions.kw),
            list(dispatcher.update.outer_middleware),
            list(bot.session.middleware),
            list(dispatcher.my_chat_member.handlers),
            list(logging.getLogger().handlers),
            client.calendars,
            client.snapshot,
        )
        assert after == before
        assert "my_chat_member" not in dispatcher.resolve_used_update_types()
        await dispatcher.feed_update(bot, telegram_message("/start", 1), settings=settings)
        assert not directory.exists()
        async with sessions() as session:
            assert await session.scalar(select(User.telegram_id)) == 555
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
