import asyncio
from collections import defaultdict
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from aiogram import Bot, Dispatcher
from aiogram.client.session.base import BaseSession
from aiogram.fsm.storage.memory import SimpleEventIsolation
from aiogram.methods import AnswerCallbackQuery, GetMe, SendDocument
from aiogram.types import Message, Update
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from bot.middlewares.session import SessionMiddleware
from bot.observability.snapshots import SnapshotTooLarge
from bot.observability.telegram import install_telegram_audit
from db.models import User

ADMIN = 74529696
GROUP = -100123456


class MonitorFake:
    def __init__(self):
        self.settings = SimpleNamespace(admin_ids=[ADMIN])
        self.chat_id = None
        self.contacts = []
        self.errors = []
        self.setups = []
        self.profiles = {}
        self.context = ContextVar("test_monitor_user", default=None)

    @contextmanager
    def operation(self, user_id=None):
        token = self.context.set(user_id)
        try:
            yield
        finally:
            self.context.reset(token)

    def contact(self, user, db_user, *, kind, event_time):
        profile = user.model_dump(exclude_none=True)
        self.contacts.append({
            "user": profile, "db_user": db_user, "kind": kind,
            "event_time": event_time, "context_user_id": self.context.get(),
        })
        self.profiles[user.id] = profile

    def error(self, source, event, error_type=None, user_id=None, **fields):
        self.errors.append({
            "source": source, "event": event, "error_type": error_type,
            "user_id": user_id, **fields,
        })

    def profile(self, telegram_id):
        return self.profiles.get(telegram_id)

    async def setup(self, bot, chat_id):
        self.setups.append(chat_id)
        self.chat_id = chat_id
        return {"users": 1, "errors": 2}

    def status_text(self):
        return "Мониторинг: 2 темы"


class SnapshotsFake:
    def __init__(self, directory):
        self.directory = directory
        self.calls = []
        self.paths = []
        self.stats_calls = 0

    async def user_snapshot(self, telegram_id=None, *, user_id=None, metadata=None):
        self.calls.append({"telegram_id": telegram_id, "user_id": user_id, "metadata": metadata})
        path = self.directory / f"user-{len(self.calls)}.json"
        path.write_text('{"user": "snapshot"}', encoding="utf-8")
        self.paths.append(path)
        return path

    async def users_snapshot(self):
        self.calls.append({"users": True})
        path = self.directory / f"users-{len(self.calls)}.json"
        path.write_bytes(b'{"users": []}')
        self.paths.append(path)
        return path

    async def stats(self):
        self.stats_calls += 1
        return dict(users=3, calendars=4, events=5, occurrences=6, notifications=7)


class TelegramFake(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []
        self.documents = []
        self.fail_documents = False

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        self.calls.append(method)
        if isinstance(method, GetMe):
            return TelegramUser(
                id=1000, is_bot=True, first_name="Bot", username="testvochizhikovcaldavbot",
            )
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, SendDocument):
            self.documents.append(method.document.path.read_bytes())
            if self.fail_documents:
                raise RuntimeError("secret-transport-body")
        chat_id = getattr(method, "chat_id", 555)
        return Message(
            message_id=100, date=datetime.now(UTC),
            chat={"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            text=getattr(method, "text", ""),
        )

    async def stream_content(
        self, url, headers=None, timeout=30, chunk_size=65536, raise_for_status=True,  # noqa: ASYNC109
    ):
        yield b""


def message_update(text, update_id=1, user_id=555, chat_id=None, forum=False, sender_chat=None):
    chat_id = chat_id or user_id
    data = {
        "message_id": update_id, "date": 1789747200,
        "from": {
            "id": user_id, "is_bot": False, "first_name": "Имя", "last_name": "Фамилия",
            "username": "testuser", "language_code": "ru", "is_premium": True,
            "allows_write_to_pm": True, "added_to_attachment_menu": False,
            "untrusted_extension": "hidden-extension-secret",
        },
        "chat": {
            "id": chat_id, "type": "private" if chat_id > 0 else "supergroup", "is_forum": forum,
        },
        "text": text,
    }
    if sender_chat:
        data["sender_chat"] = {"id": sender_chat, "type": "supergroup"}
    return Update.model_validate({"update_id": update_id, "message": data})


def callback_update(callback_data="secret-callback", update_id=1):
    return Update.model_validate({
        "update_id": update_id,
        "callback_query": {
            "id": str(update_id), "chat_instance": "test", "data": callback_data,
            "from": {"id": 555, "is_bot": False, "first_name": "Test"},
            "message": {
                "message_id": 10, "date": 1789747200,
                "from": {"id": 1000, "is_bot": True, "first_name": "Bot"},
                "chat": {"id": 555, "type": "private"}, "text": "secret-old-reply",
            },
        },
    })


def membership_update(old, new, update_id=1, actor_id=555, chat_id=555, member_id=1000):
    def member(status):
        result = {"status": status, "user": {"id": member_id, "is_bot": True, "first_name": "Bot"}}
        if status == "kicked":
            result["until_date"] = 0
        return result

    return Update.model_validate({
        "update_id": update_id,
        "my_chat_member": {
            "chat": {"id": chat_id, "type": "private" if chat_id > 0 else "supergroup"},
            "from": {"id": actor_id, "is_bot": False, "first_name": "Test"},
            "date": 1789747200 + update_id,
            "old_chat_member": member(old), "new_chat_member": member(new),
        },
    })


@pytest.fixture
async def monitored_bot(sessions, settings, tmp_path):
    monitor = MonitorFake()
    snapshots = SnapshotsFake(tmp_path)
    transport = TelegramFake()
    bot = Bot("1000:TEST_TOKEN", session=transport)
    dispatcher = Dispatcher(events_isolation=SimpleEventIsolation())
    synchronizer = SimpleNamespace(locks=defaultdict(asyncio.Lock))
    middleware = SessionMiddleware(sessions, synchronizer, settings)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    cleanup = install_telegram_audit(dispatcher, bot, monitor, sessions, snapshots)
    yield dispatcher, bot, monitor, snapshots, transport, cleanup
    cleanup()
    await dispatcher.storage.close()
    await bot.session.close()


async def test_first_contact_only_after_committed_user_creation_and_no_content(monitored_bot):
    dispatcher, bot, monitor, _, _, _ = monitored_bot

    @dispatcher.message()
    async def reply(message):
        await message.answer("secret-answer")

    await dispatcher.feed_update(bot, message_update("secret-password"))
    await dispatcher.feed_update(bot, message_update("/start secret-argument", 2))
    assert [entry["kind"] for entry in monitor.contacts].count("arrived") == 1
    assert monitor.contacts[0]["db_user"]["id"] == 1
    assert monitor.contacts[0]["context_user_id"] == 555
    profile = monitor.contacts[0]["user"]
    assert profile["first_name"] == "Имя" and profile["last_name"] == "Фамилия"
    assert profile["language_code"] == "ru" and profile["is_premium"]
    assert "untrusted_extension" not in profile
    observed = str(monitor.contacts) + str(monitor.errors)
    assert "secret-" not in observed and "hidden-extension" not in observed
    assert not monitor.errors


async def test_existing_user_is_silent_seen_not_new_or_newly_connected(monitored_bot, sessions):
    dispatcher, bot, monitor, _, _, _ = monitored_bot
    async with sessions.begin() as session:
        session.add(User(telegram_id=555, chat_id=555, encrypted_password="encrypted-secret"))
    await dispatcher.feed_update(bot, message_update("anything"))
    assert {entry["kind"] for entry in monitor.contacts} == {"seen"}
    assert monitor.contacts[0]["db_user"]["connected"] is True
    assert "encrypted_password" not in str(monitor.contacts)
    assert "encrypted-secret" not in str(monitor.contacts)


async def test_failed_initial_lookup_never_mislabels_existing_user_as_new(
    monitored_bot, sessions, monkeypatch,
):
    from bot.observability import telegram

    dispatcher, bot, monitor, _, _, _ = monitored_bot
    async with sessions.begin() as session:
        session.add(User(telegram_id=555, chat_id=555))
    original = telegram._load_user
    failed = False

    async def temporarily_unavailable(*args, **kwargs):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("secret-database-error")
        return await original(*args, **kwargs)

    monkeypatch.setattr(telegram, "_load_user", temporarily_unavailable)
    await dispatcher.feed_update(bot, message_update("secret-message"))
    assert [entry["kind"] for entry in monitor.contacts] == ["seen"]
    assert monitor.errors[0]["event"] == "profile_lookup_failed"
    assert "secret-" not in str(monitor.contacts) + str(monitor.errors)


async def test_connection_and_disconnection_observe_committed_changes(monitored_bot):
    dispatcher, bot, monitor, _, _, _ = monitored_bot

    @dispatcher.message()
    async def connect(message, user):
        if message.text == "connect":
            user.yandex_login = "example@yandex.ru"
            user.encrypted_password = "credential-ciphertext-secret"
        elif message.text == "disconnect":
            user.encrypted_password = None
            user.yandex_login = None

    await dispatcher.feed_update(bot, message_update("start"))
    await dispatcher.feed_update(bot, message_update("connect", 2))
    await dispatcher.feed_update(bot, message_update("disconnect", 3))
    events = [entry for entry in monitor.contacts if entry["kind"] != "seen"]
    assert [entry["kind"] for entry in events] == [
        "arrived", "yandex_connected", "yandex_disconnected",
    ]
    assert events[1]["db_user"]["yandex_login"] == "example@yandex.ru"
    assert "credential-ciphertext-secret" not in str(monitor.contacts)


async def test_rolled_back_connection_is_not_announced_but_first_user_is(monitored_bot):
    dispatcher, bot, monitor, _, _, _ = monitored_bot

    @dispatcher.message()
    async def fail(message, user):
        user.encrypted_password = "secret-never-committed"
        raise RuntimeError("secret-error-body")

    with pytest.raises(RuntimeError):
        await dispatcher.feed_update(bot, message_update("secret-input"))
    assert [entry["kind"] for entry in monitor.contacts] == ["arrived"]
    assert monitor.contacts[0]["db_user"]["connected"] is False
    assert monitor.errors[0]["error_type"] == "RuntimeError"
    assert "secret-" not in str(monitor.contacts) + str(monitor.errors)


async def test_callback_uses_human_actor_and_no_callback_or_reply_content(monitored_bot):
    dispatcher, bot, monitor, _, _, _ = monitored_bot

    @dispatcher.callback_query()
    async def reply(query):
        await query.answer("secret-toast")

    await dispatcher.feed_update(bot, callback_update())
    assert monitor.contacts[0]["kind"] == "arrived"
    assert monitor.contacts[0]["user"]["id"] == 555
    assert "secret-" not in str(monitor.contacts)


async def test_polling_subscribes_to_membership_and_observes_block_unblock(monitored_bot):
    dispatcher, bot, monitor, _, _, _ = monitored_bot
    assert "my_chat_member" in dispatcher.resolve_used_update_types()
    await dispatcher.feed_update(bot, membership_update("member", "kicked"))
    await dispatcher.feed_update(bot, membership_update("kicked", "member", 2))
    await dispatcher.feed_update(bot, membership_update("member", "member", 3))
    assert [entry["kind"] for entry in monitor.contacts] == ["left", "returned"]
    assert [entry["user"]["id"] for entry in monitor.contacts] == [555, 555]
    assert monitor.contacts[0]["db_user"] is None


@pytest.mark.parametrize("actor_id,chat_id,member_id", [
    (777, 555, 1000), (555, GROUP, 1000), (555, 555, 999),
])
async def test_membership_rejects_group_wrong_actor_or_wrong_bot(
    monitored_bot, actor_id, chat_id, member_id,
):
    dispatcher, bot, monitor, _, _, _ = monitored_bot
    await dispatcher.feed_update(bot, membership_update(
        "member", "kicked", actor_id=actor_id, chat_id=chat_id, member_id=member_id,
    ))
    assert not monitor.contacts


@pytest.mark.parametrize("command", ["logs_setup", "monitor_setup"])
async def test_setup_aliases_create_two_topics_before_private_filter(monitored_bot, command):
    dispatcher, bot, monitor, _, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update(
        f"/{command}@testvochizhikovcaldavbot", user_id=ADMIN, chat_id=GROUP, forum=True,
    ))
    assert monitor.setups == [GROUP]
    assert "Пользователи, Ошибки" in transport.calls[-1].text
    assert not monitor.contacts


@pytest.mark.parametrize("user_id,forum,sender_chat", [
    (555, True, None), (ADMIN, False, None), (ADMIN, True, GROUP),
])
async def test_setup_rejects_nonadmin_nonforum_anonymous(
    monitored_bot, user_id, forum, sender_chat,
):
    dispatcher, bot, monitor, _, _, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update(
        "/monitor_setup", user_id=user_id, chat_id=GROUP, forum=forum, sender_chat=sender_chat,
    ))
    assert not monitor.setups


@pytest.mark.parametrize("command", ["logs_status", "monitor_status"])
async def test_status_aliases_only_admin_private_or_bound_group(monitored_bot, command):
    dispatcher, bot, monitor, _, transport, _ = monitored_bot
    monitor.chat_id = GROUP
    await dispatcher.feed_update(bot, message_update(f"/{command}", user_id=555))
    assert "администратору" in transport.calls[-1].text
    await dispatcher.feed_update(bot, message_update(
        f"/{command}", 2, user_id=ADMIN, chat_id=GROUP - 1,
    ))
    assert "подключённой группе" in transport.calls[-1].text
    await dispatcher.feed_update(bot, message_update(
        f"/{command}", 3, user_id=ADMIN, chat_id=GROUP,
    ))
    assert transport.calls[-1].text == monitor.status_text()


@pytest.mark.parametrize("command", ["db_user 555", "db_stats", "db_users"])
async def test_database_commands_never_generate_files_in_group_or_for_nonadmin(
    monitored_bot, command,
):
    dispatcher, bot, monitor, snapshots, transport, _ = monitored_bot
    monitor.chat_id = GROUP
    for index, (user_id, chat_id) in enumerate([(555, 555), (ADMIN, GROUP)], 1):
        await dispatcher.feed_update(bot, message_update(
            f"/{command}", index, user_id=user_id, chat_id=chat_id,
        ))
    assert not snapshots.calls and not snapshots.stats_calls and not transport.documents


@pytest.mark.parametrize("internal", [False, True])
async def test_user_snapshot_selector_metadata_and_file_cleanup(monitored_bot, sessions, internal):
    dispatcher, bot, monitor, snapshots, transport, _ = monitored_bot
    async with sessions.begin() as session:
        user = User(telegram_id=555, chat_id=555)
        session.add(user)
        await session.flush()
        user_id = user.id
    monitor.profiles[555] = {"id": 555, "first_name": "Test"}
    selector = f"id:{user_id}" if internal else "555"
    await dispatcher.feed_update(bot, message_update(f"/db_user {selector}", user_id=ADMIN))
    expected = {"telegram_id": None if internal else 555, "user_id": user_id if internal else None}
    assert snapshots.calls == [{**expected, "metadata": monitor.profile(555)}]
    assert transport.documents == [b'{"user": "snapshot"}']
    assert not snapshots.paths[0].exists()
    async with sessions() as session:
        assert await session.scalar(select(User).where(User.telegram_id == ADMIN)) is None


@pytest.mark.parametrize("selector", ["", "id:", "-1", "0", "abc", "555 extra", "9" * 40])
async def test_user_snapshot_rejects_invalid_id(monitored_bot, selector):
    dispatcher, bot, _, snapshots, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update(f"/db_user {selector}", user_id=ADMIN))
    assert not snapshots.calls and "Используйте" in transport.calls[-1].text


async def test_unknown_user_snapshot_has_no_file(monitored_bot):
    dispatcher, bot, _, snapshots, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update("/db_user 999", user_id=ADMIN))
    assert not snapshots.calls and "не найден" in transport.calls[-1].text


async def test_users_snapshot_private_admin_file_cleanup_on_failed_send(monitored_bot):
    dispatcher, bot, monitor, snapshots, transport, _ = monitored_bot
    transport.fail_documents = True
    await dispatcher.feed_update(bot, message_update("/db_users", user_id=ADMIN))
    assert snapshots.calls == [{"users": True}]
    assert not snapshots.paths[0].exists()
    assert monitor.errors[0]["error_type"] == "RuntimeError"
    assert "secret-transport-body" not in str(monitor.errors)
    assert "Не удалось" in transport.calls[-1].text


async def test_users_snapshot_sends_table_json_and_removes_temp_file(monitored_bot):
    dispatcher, bot, _, snapshots, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update("/db_users", user_id=ADMIN))
    assert snapshots.calls == [{"users": True}]
    assert transport.documents == [b'{"users": []}']
    assert transport.calls[-1].caption == "Таблица users без сохранённых паролей"
    assert not snapshots.paths[0].exists()


async def test_full_database_snapshot_command_is_not_supported(monitored_bot):
    dispatcher, bot, _, snapshots, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update("/db_snapshot", user_id=ADMIN))
    assert not snapshots.calls and not transport.documents


async def test_users_snapshot_and_stats_reject_arguments(monitored_bot):
    dispatcher, bot, _, snapshots, _, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update("/db_users anything", user_id=ADMIN))
    await dispatcher.feed_update(bot, message_update("/db_stats anything", 2, user_id=ADMIN))
    assert not snapshots.calls and not snapshots.stats_calls


async def test_users_snapshot_size_error_is_explained_without_exception_body(
    monitored_bot, monkeypatch,
):
    dispatcher, bot, monitor, snapshots, transport, _ = monitored_bot

    async def fail():
        raise SnapshotTooLarge("secret-details")

    monkeypatch.setattr(snapshots, "users_snapshot", fail)
    await dispatcher.feed_update(bot, message_update("/db_users", user_id=ADMIN))
    assert "предел размера" in transport.calls[-1].text
    assert "secret-details" not in str(monitor.errors) + transport.calls[-1].text
    assert not transport.documents


async def test_stats_return_counts_in_private_chat(monitored_bot):
    dispatcher, bot, _, snapshots, transport, _ = monitored_bot
    await dispatcher.feed_update(bot, message_update("/db_stats", user_id=ADMIN))
    assert snapshots.stats_calls == 1
    assert "Пользователи: 3" in transport.calls[-1].text
    assert "Уведомления: 7" in transport.calls[-1].text


async def test_cleanup_unsubscribes_without_removing_other_membership_handlers(monitored_bot):
    dispatcher, bot, monitor, _, _, cleanup = monitored_bot

    @dispatcher.my_chat_member()
    async def other_handler(event):
        return None

    cleanup()
    cleanup()
    assert len(dispatcher.my_chat_member.handlers) == 1
    assert dispatcher.my_chat_member.handlers[0].callback is other_handler
    await dispatcher.feed_update(bot, message_update("no-monitor"))
    await bot.send_message(555, "no-outgoing-monitor")
    assert not monitor.contacts and not monitor.errors
