import json
from unittest.mock import AsyncMock

import caldav.davclient
import pytest
from aiogram import Bot
from aiogram.methods import CreateForumTopic, SendDocument, SendMessage
from aiogram.types import ForumTopic
from cryptography.fernet import Fernet
from sqlalchemy import select
from test_audit_caldav import FakeSession, response
from test_handlers import FakeTelegram, telegram_message

from bot.dispatcher import create_dispatcher
from bot.observability import install_audit
from bot.observability.caldav import install_caldav_audit
from bot.observability.database import install_database_audit
from bot.observability.service import AuditService, encryption_key
from bot.observability.settings import AuditSettings
from bot.observability.telegram import install_telegram_audit
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from bot.states.states import ConnectAccount
from CalendarClient.client import CalendarClient
from db.models import User

GROUP = -100987654321
PASSWORD = "synthetic-integration-app-password"


class IntegrationTelegram(FakeTelegram):
    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        if isinstance(method, CreateForumTopic):
            self.calls.append(method)
            return ForumTopic(
                message_thread_id=len(self.calls), name=method.name, icon_color=7322096
            )
        return await super().make_request(bot, method, timeout)


def journal_records(directory):
    return [
        json.loads(line)
        for path in sorted(directory.glob("*/*.jsonl"))
        for line in path.read_text(encoding="utf-8").splitlines()
    ]


def sent_log_text(transport):
    chunks = []
    for call in transport.calls:
        if getattr(call, "chat_id", None) != GROUP:
            continue
        for field in ("text", "caption"):
            value = getattr(call, field, None)
            if isinstance(value, str):
                chunks.append(value)
        document = getattr(call, "document", None)
        if isinstance(getattr(document, "data", None), bytes):
            chunks.append(document.data.decode("utf-8"))
    return "\n".join(chunks)


async def deliver_without_delay(audit, bot, monkeypatch):
    async def immediately(call):
        return await call()

    monkeypatch.setattr(audit, "_paced", immediately)
    for _ in range(30):
        before = dict(audit._state["cursors"])
        await audit.deliver(bot)
        if audit._state["cursors"] == before:
            return
    pytest.fail("Offline journal delivery did not drain its bounded test data")


def real_audit(tmp_path, settings):
    return AuditService(
        AuditSettings(_env_file=None, directory=tmp_path / "audit", encryption_key=None),
        settings.encryption_key.get_secret_value(),
        secrets=(settings.bot_token.get_secret_value(),),
    )


async def test_real_onboarding_audits_password_replies_and_database_commits(
    tmp_path, sessions, settings, monkeypatch
):
    transport = IntegrationTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = AsyncMock()
    client.calendars.return_value = []
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    audit = real_audit(tmp_path, settings)
    audit._cleanups.extend(
        [
            install_database_audit(sessions.kw["bind"], sessions, audit),
            install_telegram_audit(dispatcher, bot, audit),
        ]
    )
    context = dict(calendar_client=client, vault=vault, settings=settings)
    state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
    try:
        await audit.setup(bot, GROUP)
        for index, value in enumerate(["/start", "/connect", "integration@yandex.ru"]):
            await dispatcher.feed_update(bot, telegram_message(value, index), **context)
        assert await state.get_state() == ConnectAccount.password.state
        assert (await state.get_data())["login"] == "integration@yandex.ru"

        await dispatcher.feed_update(bot, telegram_message(PASSWORD, 3), **context)
        assert await state.get_state() is None
        client.calendars.assert_awaited_once_with("integration@yandex.ru", PASSWORD)
        async with sessions() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 555))
            assert vault.decrypt(user.encrypted_password) == PASSWORD
            stored_password = user.encrypted_password

        records = journal_records(audit.directory)
        incoming = [record for record in records if record["event"] == "user_message"]
        assert len(incoming) == 4
        login_record = next(record for record in incoming if record["operation_id"] == "tg-2")
        assert login_record["data"]["state"] == ConnectAccount.login.state
        assert login_record["data"]["text"] == "integration@yandex.ru"
        password_record = next(record for record in incoming if record["operation_id"] == "tg-3")
        assert password_record["data"]["state"] == ConnectAccount.password.state
        encrypted = password_record["data"]["text_encrypted"]
        assert encrypted.startswith("fernet:")
        cipher = Fernet(encryption_key(audit.settings, settings.encryption_key.get_secret_value()))
        assert cipher.decrypt(encrypted.removeprefix("fernet:").encode()).decode() == PASSWORD
        assert "text" not in password_record["data"]

        committed = [record for record in records if record["event"] == "transaction_committed"]
        assert committed and all(record["user_id"] == 555 for record in committed)
        changes = [
            (record, statement)
            for record in committed
            for statement in record["data"]["statements"]
        ]
        created = next(statement for _, statement in changes if statement["operation"] == "INSERT")
        assert created["parameters"][0]["telegram_id"] == 555
        assert created["parameters"][0]["timezone"] == "Europe/Moscow"
        saved_record, saved = next(
            (record, statement)
            for record, statement in changes
            if any("encrypted_password" in values for values in statement["parameters"])
            and statement["operation"] == "UPDATE"
        )
        assert saved_record["operation_id"] == "tg-3"
        assert saved_record["data"]["status"] == "committed"
        assert saved_record["data"]["omitted_statements"] == 0
        assert saved["parameters"][0]["encrypted_password"] == stored_password
        assert saved["parameters"][0]["yandex_login"] == "integration@yandex.ru"
        assert saved["rowcount"] == 1 and "UPDATE users" in saved["sql"]
        replies = [record for record in records if record["event"] == "bot_reply"]
        expected = [call.text for call in transport.calls if isinstance(call, SendMessage)]
        assert [
            record["data"]["text"] for record in replies if "text" in record["data"]
        ] == expected
        assert any(
            record["operation_id"] == "tg-3" and record["data"]["method"] == "deleteMessage"
            for record in replies
        )

        await deliver_without_delay(audit, bot, monkeypatch)
        # Delivering audit messages must not recursively record those messages.
        assert journal_records(audit.directory) == records
        output = json.dumps(records, ensure_ascii=False) + sent_log_text(transport)
        for secret in (
            PASSWORD,
            settings.bot_token.get_secret_value(),
            settings.encryption_key.get_secret_value(),
        ):
            assert secret not in output
        category_calls = [
            call
            for call in transport.calls
            if getattr(call, "chat_id", None) == GROUP
            and isinstance(call, (SendMessage, SendDocument))
        ]
        assert {call.message_thread_id for call in category_calls} == {
            audit._state["topics"]["conversation"],
            audit._state["topics"]["database"],
        }
    finally:
        await audit.close()
        await dispatcher.storage.close()
        await bot.session.close()


async def test_real_caldav_audit_redacts_http_headers_and_forged_ciphertext_prefix(
    tmp_path, settings, monkeypatch
):
    audit = real_audit(tmp_path, settings)
    transport = IntegrationTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    body = f"fernet:{PASSWORD} {settings.bot_token.get_secret_value()}".encode()
    http = FakeSession([response(207, body, {"Set-Cookie": "synthetic-session-cookie"})])
    monkeypatch.setattr(caldav.davclient.requests, "Session", lambda **kwargs: http)
    client = CalendarClient()
    audit._cleanups.append(install_caldav_audit(client, audit))
    try:
        await audit.setup(bot, GROUP)
        with audit.operation(user_id=555, operation_id="offline-caldav"):
            with client._client("integration@yandex.ru", PASSWORD) as dav:
                result = dav.request(
                    "https://caldav.yandex.ru/cal/integration/",
                    "REPORT",
                    "<calendar-query/>",
                    {"Authorization": "Basic synthetic-basic-secret", "Depth": "1"},
                )
        assert result.status == 207 and result._raw == body
        request, received = journal_records(audit.directory)
        assert request["event"] == "http_request"
        assert request["data"]["headers"]["Authorization"] == "[REDACTED]"
        assert request["data"]["headers"]["Depth"] == "1"
        assert received["data"]["request_headers"]["Authorization"] == "[REDACTED]"
        assert received["data"]["headers"]["Set-Cookie"] == "[REDACTED]"
        assert request["data"]["request_id"] == received["data"]["request_id"]
        assert received["operation_id"] == request["operation_id"] == "offline-caldav"
        await deliver_without_delay(audit, bot, monkeypatch)
        output = json.dumps(journal_records(audit.directory)) + sent_log_text(transport)
        for secret in (
            PASSWORD,
            settings.bot_token.get_secret_value(),
            "synthetic-basic-secret",
            "credential-token",
            "synthetic-session-cookie",
        ):
            assert secret not in output
        assert "[REDACTED]" in received["data"]["body"]
        log_calls = [
            call for call in transport.calls if isinstance(call, (SendMessage, SendDocument))
        ]
        assert log_calls
        assert {call.message_thread_id for call in log_calls} == {audit._state["topics"]["caldav"]}
    finally:
        await audit.close()
        await bot.session.close()


async def test_disabled_extension_has_no_storage_or_application_hooks(
    tmp_path, sessions, settings, monkeypatch
):
    import bot.observability.settings as audit_settings_module

    directory = tmp_path / "disabled-audit"
    monkeypatch.setenv("AUDIT_ENABLED", "false")
    monkeypatch.setenv("AUDIT_DIRECTORY", str(directory))
    # Exercise environment configuration without reading the user's real .env.
    monkeypatch.setattr(
        audit_settings_module, "AuditSettings", lambda: AuditSettings(_env_file=None)
    )
    transport = IntegrationTelegram()
    bot = Bot(settings.bot_token.get_secret_value(), session=transport)
    client = CalendarClient()
    synchronizer = Synchronizer(sessions, client, AsyncMock(), settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    engine = sessions.kw["bind"]
    before = (
        dict(sessions.kw),
        list(dispatcher.update.outer_middleware),
        list(bot.session.middleware),
        list(engine.sync_engine.dispatch.after_cursor_execute),
        list(engine.dialect.dispatch.handle_error),
    )
    try:
        assert (
            install_audit(
                bot=bot,
                dispatcher=dispatcher,
                engine=engine,
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
            list(engine.sync_engine.dispatch.after_cursor_execute),
            list(engine.dialect.dispatch.handle_error),
        )
        assert after == before
        assert "_client" not in vars(client)
        await dispatcher.feed_update(bot, telegram_message("/start", 1), settings=settings)
        assert not directory.exists()
        async with sessions() as session:
            assert await session.scalar(select(User.telegram_id)) == 555
    finally:
        await dispatcher.storage.close()
        await bot.session.close()
