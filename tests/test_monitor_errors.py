import asyncio
import json
import logging
from contextlib import contextmanager
from contextvars import ContextVar

import pytest
from aiogram import Bot
from aiogram.client.session.base import BaseSession
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.methods import AnswerCallbackQuery, SendMessage

from bot.observability.errors import install_error_monitor
from CalendarClient.errors import CalendarConnectionError

SECRET = "secret-password-do-not-record"
GROUP = -100123


class MonitorFake:
    chat_id = GROUP

    def __init__(self):
        self.events = []
        self.current = ContextVar("test_monitor_user", default=None)

    def error(self, source, event, *, error_type=None, user_id=None, **fields):
        self.events.append(
            {
                "source": source,
                "event": event,
                "error_type": error_type,
                "user_id": user_id or self.current.get(),
                **fields,
            }
        )

    @contextmanager
    def operation(self, user_id):
        token = self.current.set(user_id)
        try:
            yield
        finally:
            self.current.reset(token)


class CalendarFake:
    failure = None

    async def calendars(self, login, password):
        if self.failure:
            raise self.failure
        return ["calendar"]

    async def snapshot(self, login, password, *args):
        if self.failure:
            raise self.failure
        return "snapshot"


class TelegramFake(BaseSession):
    def __init__(self):
        super().__init__()
        self.failure = None
        self.log_during_request = False

    async def close(self):
        pass

    async def make_request(self, bot, method, timeout=None):  # noqa: ASYNC109
        if self.log_during_request:
            logging.getLogger("aiogram.client").error("monitor delivery %s", SECRET)
        if self.failure:
            raise self.failure
        return True

    async def stream_content(
        self,
        url,
        headers=None,
        timeout=30,  # noqa: ASYNC109
        chunk_size=65536,
        raise_for_status=True,
    ):
        yield b""


@pytest.fixture
def installed():
    client = CalendarFake()
    monitor = MonitorFake()
    bot = Bot("123456789:abcdefghijklmnopqrstuvwxyz123456789", session=TelegramFake())
    cleanup = install_error_monitor(client, bot, monitor)
    yield client, bot, monitor, cleanup
    cleanup()


async def test_successful_calls_never_produce_monitor_records(installed):
    client, bot, monitor, _ = installed
    assert await client.calendars("private@yandex.ru", SECRET) == ["calendar"]
    assert await client.snapshot("private@yandex.ru", SECRET, "private-calendar") == "snapshot"
    assert await bot.send_message(123, SECRET)
    logging.getLogger("bot.services.synchronizer").warning("ignored %s", SECRET)
    assert monitor.events == []


@pytest.mark.parametrize("method", ["calendars", "snapshot"])
async def test_caldav_failure_records_only_safe_code_and_operation(installed, method):
    client, _, monitor, _ = installed
    client.failure = CalendarConnectionError(f"URL and credentials {SECRET}", code="authentication")
    client.failure.http_status = 401
    with monitor.operation(777), pytest.raises(CalendarConnectionError) as caught:
        await getattr(client, method)("private@yandex.ru", SECRET)
    assert caught.value is client.failure
    assert monitor.events == [
        {
            "source": "caldav",
            "event": "operation_failed",
            "user_id": 777,
            "error_type": "CalendarConnectionError",
            "code": "authentication",
            "http_status": 401,
            "operation": "discover_calendars" if method == "calendars" else "fetch_events",
        }
    ]
    assert SECRET not in json.dumps(monitor.events)
    assert "private@yandex.ru" not in json.dumps(monitor.events)


async def test_unknown_caldav_error_code_and_exception_text_are_not_logged(installed):
    client, _, monitor, _ = installed
    client.failure = CalendarConnectionError(SECRET, code=SECRET)
    with pytest.raises(CalendarConnectionError):
        await client.calendars("login", SECRET)
    assert monitor.events[0]["code"] == "unknown"
    client.failure = ValueError(SECRET)
    with pytest.raises(ValueError):
        await client.snapshot("login", SECRET)
    assert monitor.events[1]["error_type"] == "ValueError"
    assert "code" not in monitor.events[1]
    assert SECRET not in json.dumps(monitor.events)


def test_errors_from_callbacks_and_background_have_safe_type_and_location(installed):
    _, _, monitor, _ = installed
    with monitor.operation(888):
        logging.getLogger("bot.dispatcher").error("Telegram handler failed (%s)", "ValueError")
    logging.getLogger("bot.services.synchronizer").error(
        "Synchronization failed for user=%s (%s)", 42, "TimeoutError"
    )
    assert monitor.events[0]["source"] == "bot.dispatcher"
    assert monitor.events[0]["error_type"] == "ValueError"
    assert monitor.events[0]["user_id"] == 888
    assert monitor.events[1]["error_type"] == "TimeoutError"
    assert monitor.events[1]["db_user_id"] == 42
    assert monitor.events[1]["user_id"] is None
    assert all(e["filename"] == "test_monitor_errors.py" and e["line"] > 0 for e in monitor.events)
    assert all(e["event"] == "runtime_error" for e in monitor.events)


def test_logging_never_formats_arbitrary_messages_or_exception_details(installed):
    _, _, monitor, _ = installed
    logger = logging.getLogger("db.db_config")
    try:
        raise RuntimeError(f"Authorization: Basic {SECRET}")
    except RuntimeError:
        logger.exception("SQL INSERT password=%s", SECRET)
    logger.error(f"interpolated secret {SECRET}")
    logger.error("unknown template %s %s", "not-a-safe-error", SECRET)
    assert len(monitor.events) == 3
    first = monitor.events[0]
    assert first["error_type"] == "RuntimeError"
    assert first["frames"][0]["filename"] == "test_monitor_errors.py"
    serialized = json.dumps(monitor.events)
    for forbidden in (SECRET, "Authorization", "SQL INSERT", "interpolated", "not-a-safe-error"):
        assert forbidden not in serialized
    assert set(first["frames"][0]) == {"filename", "function", "line"}


def test_unrelated_logs_and_monitor_errors_are_excluded(installed):
    _, _, monitor, _ = installed
    for name in ("unrelated.service", "bot.observability.service", "urllib3.connectionpool"):
        logging.getLogger(name).error(SECRET)
    assert monitor.events == []


async def test_outgoing_background_forbidden_is_error_not_confirmed_departure(installed):
    _, bot, monitor, _ = installed
    method = SendMessage(chat_id=333, text=SECRET)
    bot.session.failure = TelegramForbiddenError(method=method, message=f"deactivated {SECRET}")
    with pytest.raises(TelegramForbiddenError):
        await bot(method)
    assert monitor.events == [
        {
            "source": "telegram",
            "event": "delivery_forbidden",
            "user_id": 333,
            "error_type": "TelegramForbiddenError",
            "method": "sendMessage",
        }
    ]


async def test_failed_callback_answer_uses_user_context_without_callback_data(installed):
    _, bot, monitor, _ = installed
    method = AnswerCallbackQuery(callback_query_id=SECRET, text=SECRET)
    bot.session.failure = TelegramBadRequest(method=method, message=SECRET)
    with monitor.operation(444), pytest.raises(TelegramBadRequest):
        await bot(method)
    assert monitor.events[0]["user_id"] == 444
    assert monitor.events[0]["method"] == "answerCallbackQuery"
    assert SECRET not in json.dumps(monitor.events)


async def test_retry_after_preserves_numeric_delay_only(installed):
    _, bot, monitor, _ = installed
    method = SendMessage(chat_id=333, text=SECRET)
    bot.session.failure = TelegramRetryAfter(method=method, message=SECRET, retry_after=30)
    with pytest.raises(TelegramRetryAfter):
        await bot(method)
    assert monitor.events[0]["retry_after_seconds"] == 30
    assert SECRET not in json.dumps(monitor.events)


async def test_monitor_upload_errors_and_nested_http_logs_do_not_recurse(installed):
    _, bot, monitor, _ = installed
    method = SendMessage(chat_id=GROUP, text="monitor report")
    bot.session.failure = TelegramBadRequest(method=method, message=SECRET)
    bot.session.log_during_request = True
    with pytest.raises(TelegramBadRequest):
        await bot(method)
    assert monitor.events == []


async def test_broken_monitor_does_not_replace_original_exception(installed):
    client, _, monitor, _ = installed
    error = RuntimeError(SECRET)
    client.failure = error

    def broken(*args, **kwargs):
        logging.getLogger("bot.dispatcher").error("should not recurse")
        raise OSError("disk full")

    monitor.error = broken
    with pytest.raises(RuntimeError) as caught:
        await client.calendars("login", SECRET)
    assert caught.value is error
    assert monitor.events == []


async def test_cleanup_restores_instances_and_removes_only_owned_handler(installed):
    client, bot, monitor, cleanup = installed
    root = logging.getLogger()
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        cleanup()
        cleanup()
        assert "calendars" not in vars(client)
        assert "snapshot" not in vars(client)
        assert not list(bot.session.middleware)
        assert other in root.handlers
        client.failure = RuntimeError(SECRET)
        with pytest.raises(RuntimeError):
            await client.calendars("login", SECRET)
        logging.getLogger("bot.dispatcher").error("after cleanup")
        assert monitor.events == []
    finally:
        root.removeHandler(other)


async def test_task_cancellation_is_not_reported_as_runtime_failure(installed):
    client, _, monitor, _ = installed
    client.failure = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.calendars("login", SECRET)
    assert monitor.events == []
