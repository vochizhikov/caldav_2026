import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, DeleteMessage, EditMessageText, GetMe
from aiogram.types import InputMediaPhoto, Message, Update, User

from bot.observability.telegram import install_telegram_audit
from bot.states.states import ConnectAccount, EditSettings

ADMIN = 74529696
GROUP = -100123456


class AuditFake:
    def __init__(self):
        self.settings = SimpleNamespace(admin_ids=[ADMIN], detailed_minutes=30)
        self.chat_id = None
        self.events = []
        self.secrets = []
        self.setups = []
        self.mode = "normal"
        self.context = ContextVar("test_audit", default=None)

    def emit(self, category, event, **fields):
        self.events.append({
            "category": category, "event": event, **(self.context.get() or {}), **fields,
        })

    def sanitize(self, value):
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[SECRET]")
        elif isinstance(value, list):
            return [self.sanitize(item) for item in value]
        elif isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        return value

    def register_secret(self, secret):
        self.secrets.append(secret)

    def encrypt(self, value):
        return f"cipher-{self.secrets.index(value)}"

    @contextmanager
    def operation(self, user_id=None, operation_id=None):
        token = self.context.set({"user_id": user_id, "operation_id": operation_id or "bg-op"})
        try:
            yield
        finally:
            self.context.reset(token)

    async def setup(self, bot, chat_id):
        self.setups.append(chat_id)
        self.chat_id = chat_id
        return dict(conversation=1, caldav=2, database=3, archives=4)

    def set_mode(self, mode, minutes=30):
        self.mode = mode
        self.minutes = minutes

    def status_text(self):
        return f"mode={self.mode}"


class TelegramFake(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.failure = None

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        if self.failure:
            raise self.failure
        if isinstance(method, GetMe):
            return User(id=1000, is_bot=True, first_name="Bot", username="testvochizhikovcaldavbot")
        if isinstance(method, (AnswerCallbackQuery, DeleteMessage)):
            return True
        chat_id = getattr(method, "chat_id", 555)
        return Message(
            message_id=100,
            date=datetime.now(UTC),
            chat={"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            text=getattr(method, "text", ""),
        )

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True,  # noqa: ASYNC109
    ):
        yield b""


def message_update(text, update_id=1, user_id=555, chat_id=None, forum=False, sender_chat=None):
    chat_id = chat_id or user_id
    message = {
        "message_id": update_id,
        "date": 1789747200,
        "from": {"id": user_id, "is_bot": False, "first_name": "Test"},
        "chat": {
            "id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "is_forum": forum,
        },
        "text": text,
    }
    if sender_chat:
        message["sender_chat"] = {"id": sender_chat, "type": "supergroup"}
    return Update.model_validate({"update_id": update_id, "message": message})


def callback_update(data, update_id=1):
    return Update.model_validate({
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id), "chat_instance": "test", "data": data,
            "from": {"id": 555, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": 10, "date": 1789747200,
                "chat": {"id": 555, "type": "private"},
            },
        },
    })


@pytest.fixture
async def audit_bot():
    audit = AuditFake()
    transport = TelegramFake()
    bot = Bot("1000:TEST_TOKEN", session=transport)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    cleanup = install_telegram_audit(dispatcher, bot, audit)
    yield dispatcher, bot, audit, transport, cleanup
    cleanup()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_setup_intercepts_before_private_only_handler_middleware(audit_bot):
    dispatcher, bot, audit, transport, _ = audit_bot
    reached = []

    async def private_only(handler, event, data):
        reached.append(event)
        assert event.chat.type == "private"
        return await handler(event, data)

    dispatcher.message.outer_middleware(private_only)
    await dispatcher.feed_update(
        bot, message_update("/logs_setup", user_id=ADMIN, chat_id=GROUP, forum=True),
    )
    assert audit.setups == [GROUP]
    assert audit.chat_id == GROUP
    assert "Группа подключена" in transport.calls[-1].text
    assert reached == [] and audit.events == []


@pytest.mark.parametrize("user_id,forum,sender_chat", [
    (555, True, None), (ADMIN, False, None), (ADMIN, True, GROUP),
])
async def test_setup_rejects_nonadmin_nonforum_and_anonymous(
    audit_bot, user_id, forum, sender_chat,
):
    dispatcher, bot, audit, transport, _ = audit_bot
    await dispatcher.feed_update(bot, message_update(
        "/logs_setup", user_id=user_id, chat_id=GROUP, forum=forum, sender_chat=sender_chat,
    ))
    assert not audit.setups
    assert transport.calls and not audit.events


async def test_setup_command_with_bot_mention_and_wrong_mention(audit_bot):
    dispatcher, bot, audit, _, _ = audit_bot
    await dispatcher.feed_update(bot, message_update(
        "/logs_setup@other_bot", user_id=ADMIN, chat_id=GROUP, forum=True,
    ))
    assert not audit.setups
    await dispatcher.feed_update(bot, message_update(
        "/logs_setup@testvochizhikovcaldavbot", 2, user_id=ADMIN, chat_id=GROUP, forum=True,
    ))
    assert audit.setups == [GROUP]


async def test_mode_is_admin_only_and_restricted_to_private_or_bound_group(audit_bot):
    dispatcher, bot, audit, transport, _ = audit_bot
    audit.chat_id = GROUP
    for index, (user, chat) in enumerate([(555, 555), (ADMIN, GROUP - 1)], 1):
        await dispatcher.feed_update(bot, message_update(
            "/logs_mode detailed 15", index, user_id=user, chat_id=chat,
        ))
        assert audit.mode == "normal"
    await dispatcher.feed_update(bot, message_update(
        "/logs_mode detailed 15", 3, user_id=ADMIN, chat_id=GROUP,
    ))
    assert audit.mode == "detailed" and audit.minutes == 15
    await dispatcher.feed_update(bot, message_update("/logs_status", 4, user_id=ADMIN))
    assert transport.calls[-1].text == "mode=detailed"
    await dispatcher.feed_update(bot, message_update("/logs_mode normal", 5, user_id=ADMIN))
    assert audit.mode == "normal" and not audit.events


@pytest.mark.parametrize("argument", [
    "", "debug", "detailed 0", "detailed -1", "detailed 1441", "detailed bad",
    "normal 30", "detailed 10 extra", "detailed " + "9" * 5000,
])
async def test_mode_rejects_invalid_duration_without_changing_settings(audit_bot, argument):
    dispatcher, bot, audit, transport, _ = audit_bot
    await dispatcher.feed_update(bot, message_update(f"/logs_mode {argument}", user_id=ADMIN))
    assert audit.mode == "normal"
    assert "Используйте" in transport.calls[-1].text
    assert not audit.events


@pytest.mark.parametrize("raw_state,text,field", [
    (ConnectAccount.password, "secret-password", "text_encrypted"),
    (ConnectAccount.password, "/cancel secret-password", "text_encrypted"),
    (None, "accidental-password", "text_encrypted"),
    (None, "/not-a-real-command-secret", "text_encrypted"),
    (None, "/start secret-argument", "arguments_encrypted"),
    (None, "fernet:actually-plaintext", "text_encrypted"),
    (ConnectAccount.login, "accidental malformed password", "text_encrypted"),
    (EditSettings.timezone, "accidental-password", "text_encrypted"),
])
async def test_password_unknown_text_and_arguments_are_encrypted_before_handler(
    audit_bot, raw_state, text, field,
):
    dispatcher, bot, audit, _, _ = audit_bot
    state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
    await state.set_state(raw_state)

    @dispatcher.message()
    async def echo(message):
        await message.answer(message.text)

    await dispatcher.feed_update(bot, message_update(text))
    incoming, outgoing = audit.events
    assert incoming[field].startswith("cipher-")
    assert incoming["operation_id"] == outgoing["operation_id"] == "tg-1"
    secret = text.split(maxsplit=1)[1] if field == "arguments_encrypted" else text
    assert secret not in str(audit.events)
    assert "[SECRET]" in outgoing["text"]


@pytest.mark.parametrize("raw_state,text", [
    (ConnectAccount.login, "user@yandex.ru"), (EditSettings.timezone, "Europe/Moscow"),
])
async def test_known_nonsecret_input_states_preserve_text(audit_bot, raw_state, text):
    dispatcher, bot, audit, _, _ = audit_bot
    state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
    await state.set_state(raw_state)
    await dispatcher.feed_update(bot, message_update(text))
    assert audit.events[0]["text"] == text


async def test_caption_is_encrypted_without_serializing_attachment(audit_bot):
    dispatcher, bot, audit, _, _ = audit_bot
    data = message_update("placeholder").model_dump(exclude_none=True)
    data["message"].pop("text")
    data["message"]["caption"] = "secret-caption"
    data["message"]["document"] = {"file_id": "id", "file_unique_id": "unique"}
    await dispatcher.feed_update(bot, Update.model_validate(data))
    assert audit.events[0]["caption_encrypted"] == "cipher-0"
    assert "secret-caption" not in str(audit.events)
    assert "file_id" not in str(audit.events)


async def test_callback_edit_delete_and_answer_share_correlation(audit_bot):
    dispatcher, bot, audit, _, _ = audit_bot

    @dispatcher.callback_query()
    async def click(query):
        await query.answer("Сохранено")
        await query.message.edit_text("Новое сообщение")
        await query.message.delete()

    await dispatcher.feed_update(bot, callback_update("menu:settings", 10))
    assert audit.events[0]["callback_data"] == "menu:settings"
    assert [entry.get("method") for entry in audit.events[1:]] == [
        "answerCallbackQuery", "editMessageText", "deleteMessage",
    ]
    assert {entry["operation_id"] for entry in audit.events} == {"tg-10"}


async def test_background_replies_captured_and_log_group_excluded(audit_bot):
    _, bot, audit, _, _ = audit_bot
    audit.chat_id = GROUP
    await bot.send_message(555, "Напоминание")
    await bot.send_message(GROUP, "Пачка логов", message_thread_id=1)
    await bot(EditMessageText(chat_id=GROUP, message_id=100, text="Пачка обновлена"))
    assert len(audit.events) == 1
    assert audit.events[0]["text"] == "Напоминание"
    assert audit.events[0]["operation_id"] == "bg-op"


async def test_transport_errors_capture_type_only_and_preserve_failure(audit_bot):
    _, bot, audit, transport, _ = audit_bot
    transport.failure = RuntimeError("credential-body-must-not-be-logged")
    with pytest.raises(RuntimeError):
        await bot.send_message(555, "Напоминание")
    assert audit.events[0]["event"] == "bot_reply_failed"
    assert audit.events[0]["error_type"] == "RuntimeError"
    assert "credential-body" not in str(audit.events)


async def test_media_edit_captures_caption_without_file_payload(audit_bot):
    _, bot, audit, _, _ = audit_bot
    await bot.edit_message_media(
        chat_id=555, message_id=100,
        media=InputMediaPhoto(media="file-reference", caption="Подпись"),
    )
    assert audit.events[0]["media"] == {"type": "photo", "caption": "Подпись"}
    assert "file-reference" not in str(audit.events)


async def test_fsm_state_is_read_after_preceding_update_releases_isolation(audit_bot):
    dispatcher, bot, audit, _, _ = audit_bot
    state = dispatcher.fsm.get_context(bot=bot, chat_id=555, user_id=555)
    await state.set_state(ConnectAccount.login)
    entered = asyncio.Event()
    release = asyncio.Event()

    @dispatcher.message()
    async def transition(message, state):
        if message.text == "user@yandex.ru":
            entered.set()
            await release.wait()
            await state.set_state(ConnectAccount.password)

    first = asyncio.create_task(dispatcher.feed_update(bot, message_update("user@yandex.ru", 1)))
    await entered.wait()
    second = asyncio.create_task(dispatcher.feed_update(bot, message_update("secret-password", 2)))
    release.set()
    await asyncio.gather(first, second)
    assert audit.events[1]["state"] == ConnectAccount.password.state
    assert audit.events[1]["text_encrypted"].startswith("cipher-")
    assert "secret-password" not in str(audit.events)


async def test_cleanup_removes_both_hooks_and_is_idempotent(audit_bot):
    dispatcher, bot, audit, _, cleanup = audit_bot
    cleanup()
    cleanup()
    await dispatcher.feed_update(bot, message_update("/help"))
    await bot.send_message(555, "Ответ")
    assert not audit.events
