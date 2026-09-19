import json
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import User

from bot.observability.service import MonitorService
from bot.observability.settings import MonitorSettings


@pytest.fixture
async def monitor(tmp_path):
    now = [datetime(2026, 9, 20, 12, tzinfo=UTC)]
    service = MonitorService(
        MonitorSettings(_env_file=None, directory=tmp_path / "monitor"), clock=lambda: now[0]
    )
    service._last_send = float("-inf")
    yield service, now
    await service.close()


def user(**fields):
    return User(
        id=12345,
        is_bot=False,
        first_name="Иван",
        last_name="Петров",
        username="ivan_test",
        language_code="ru",
        is_premium=True,
        **fields,
    )


def db_user(connected=False):
    return {
        "id": 42,
        "created_at": datetime(2026, 9, 20, tzinfo=UTC),
        "timezone": "Europe/Moscow",
        "connected": connected,
        "notifications_enabled": True,
        "encrypted_password": "never-export-me",
    }


def pending(service):
    return [
        json.loads(row[0])
        for row in service._connection.execute("SELECT payload FROM pending ORDER BY id")
    ]


def bind(service):
    with service._connection:
        service._set("chat_id", -100123)
        service._set("topics", {"users": 11, "errors": 12})


async def test_arrival_profile_is_useful_persistent_and_only_once(monitor):
    service, now = monitor
    service.contact(user(), db_user(), kind="arrived", event_time=now[0])
    service.contact(user(), db_user(), kind="arrived", event_time=now[0])
    service.contact(user(), db_user(), kind="seen", event_time=now[0])
    rows = pending(service)
    assert len(rows) == 1
    text = service.format_contact(rows[0])
    for expected in (
        "Новый пользователь",
        "12345",
        "Иван",
        "Петров",
        "@ivan_test",
        "https://t.me/ivan_test",
        "Premium: да",
        "ID в БД: 42",
        "/db_user 12345",
    ):
        assert expected in text
    assert "never-export-me" not in json.dumps(rows)
    await service.close()
    restored = MonitorService(service.settings)
    try:
        assert restored.profile(12345)["first_name"] == "Иван"
        assert len(pending(restored)) == 1
        restored.contact(user(), db_user(), kind="arrived", event_time=now[0])
        assert len(pending(restored)) == 1
    finally:
        await restored.close()


async def test_existing_user_silent_baseline_does_not_become_new(monitor):
    service, now = monitor
    service.contact(user(), db_user(), kind="seen", event_time=now[0])
    service.contact(user(), db_user(), kind="arrived", event_time=now[0])
    assert not pending(service)


async def test_block_return_dedup_and_stale_membership(monitor):
    service, now = monitor
    service.contact(user(), db_user(), kind="seen", event_time=now[0])
    now[0] += timedelta(seconds=10)
    blocked_at = now[0]
    service.contact(user(), db_user(), kind="left", event_time=blocked_at)
    service.contact(user(), db_user(), kind="left", event_time=blocked_at)
    service.contact(
        user(), db_user(), kind="returned", event_time=blocked_at - timedelta(seconds=1)
    )
    assert service.profile(12345)["status"] == "blocked"
    now[0] += timedelta(seconds=10)
    service.contact(user(), db_user(), kind="returned", event_time=now[0])
    service.contact(user(), db_user(), kind="returned", event_time=now[0])
    service.contact(user(), db_user(), kind="left", event_time=blocked_at)
    assert service.profile(12345)["status"] == "active"
    assert [row["event"] for row in pending(service)] == ["left", "returned"]


async def test_connect_disconnect_distinct_from_departure(monitor):
    service, now = monitor
    service.contact(user(), db_user(), kind="seen", event_time=now[0])
    service.contact(user(), db_user(True), kind="yandex_connected", event_time=now[0])
    service.contact(user(), db_user(True), kind="yandex_connected", event_time=now[0])
    now[0] += timedelta(seconds=1)
    service.contact(user(), db_user(True), kind="seen", event_time=now[0])
    service.contact(user(), db_user(), kind="yandex_disconnected", event_time=now[0])
    assert [r["event"] for r in pending(service)] == ["yandex_connected", "yandex_disconnected"]
    assert service.profile(12345)["status"] == "active"


async def test_newer_profile_and_connection_cannot_be_replaced_by_delayed_event(monitor):
    service, now = monitor
    service.contact(user(), db_user(), kind="seen", event_time=now[0])
    service.contact(user(), db_user(True), kind="yandex_connected", event_time=now[0])
    newer = now[0] + timedelta(seconds=10)
    renamed = User(id=12345, is_bot=False, first_name="Новое имя")
    service.contact(renamed, db_user(True), kind="yandex_connected", event_time=newer)
    service.contact(
        user(), db_user(), kind="yandex_disconnected", event_time=now[0] + timedelta(seconds=5)
    )
    profile = service.profile(12345)
    assert profile["first_name"] == "Новое имя"
    assert "username" not in profile
    assert "is_premium" not in profile
    assert profile["db_user"]["connected"] is True
    assert [r["event"] for r in pending(service)] == ["yandex_connected"]


async def test_error_metadata_repeat_summary_and_user_context(monitor):
    service, now = monitor
    bind(service)
    with service.operation(user_id=12345):
        for _ in range(3):
            service.error(
                "caldav",
                "operation_failed",
                error_type="TimeoutError",
                operation="fetch_events",
                message="secret-password",
                body="private-body",
            )
    row = service._connection.execute("SELECT * FROM errors").fetchone()
    assert row["count"] == 3
    assert "secret-password" not in row["payload"]
    assert "private-body" not in row["payload"]
    assert json.loads(row["payload"])["user_id"] == 12345
    bot = SimpleNamespace(send_message=AsyncMock())
    await service.deliver(bot)
    assert bot.send_message.await_count == 1
    assert "Повторений в этой сводке: 3" in bot.send_message.await_args.args[1]
    assert bot.send_message.await_args.kwargs["message_thread_id"] == 12
    with service.operation(user_id=12345):
        service.error(
            "caldav", "operation_failed", error_type="TimeoutError", operation="fetch_events"
        )
    await service.deliver(bot)
    assert bot.send_message.await_count == 1
    now[0] += timedelta(seconds=301)
    service._last_send = float("-inf")
    await service.deliver(bot)
    assert bot.send_message.await_count == 2
    assert "Повторений в этой сводке: 1" in bot.send_message.await_args.args[1]


async def test_error_arriving_while_sending_is_not_lost(monitor):
    service, now = monitor
    bind(service)
    service.error("db", "runtime_error", error_type="RuntimeError")

    async def send(*args, **kwargs):
        service.error("db", "runtime_error", error_type="RuntimeError")

    await service.deliver(SimpleNamespace(send_message=AsyncMock(side_effect=send)))
    row = service._connection.execute("SELECT * FROM errors").fetchone()
    assert row["count"] == 2
    assert row["sent_count"] == 1


async def test_failed_user_topic_retains_event_but_errors_deliver(monitor):
    service, now = monitor
    bind(service)
    service.contact(user(), db_user(), kind="arrived", event_time=now[0])
    service.error("db", "failed", error_type="RuntimeError")

    async def send(*args, **kwargs):
        service._last_send = float("-inf")
        if kwargs["message_thread_id"] == 11:
            raise RuntimeError("closed topic")

    bot = SimpleNamespace(send_message=AsyncMock(side_effect=send))
    await service.deliver(bot)
    assert len(pending(service)) == 1
    assert service._connection.execute("SELECT sent_count FROM errors").fetchone()[0] == 1
    bot.send_message.side_effect = None
    service._last_send = float("-inf")
    await service.deliver(bot)
    assert not pending(service)


async def test_partial_setup_retry_does_not_duplicate_topics(monitor):
    service, _ = monitor
    bot = SimpleNamespace(
        create_forum_topic=AsyncMock(
            side_effect=[
                SimpleNamespace(message_thread_id=11),
                RuntimeError("network"),
            ]
        )
    )
    with pytest.raises(RuntimeError):
        await service.setup(bot, -100123)
    bot.create_forum_topic = AsyncMock(return_value=SimpleNamespace(message_thread_id=12))
    assert await service.setup(bot, -100123) == {"users": 11, "errors": 12}
    await service.setup(bot, -100123)
    assert bot.create_forum_topic.await_count == 1
    with pytest.raises(ValueError):
        await service.setup(bot, -100999)


async def test_pending_limit_keeps_existing_events_and_reports_drops(monitor):
    service, now = monitor
    service.settings.max_pending = 1
    service.contact(user(), db_user(), kind="arrived", event_time=now[0])
    service.contact(user(), db_user(), kind="left", event_time=now[0])
    assert len(pending(service)) == 1
    assert "Пропущено из-за переполнения: 1" in service.status_text()


async def test_error_locations_and_telegram_method_are_visible(monitor):
    service, _ = monitor
    service.error(
        "telegram",
        "api_error",
        error_type="TelegramRetryAfter",
        method="sendMessage",
        retry_after_seconds=10,
        filename="user_notifier.py",
        line=20,
        db_user_id=15,
        frames=[{"filename": "client.py", "function": "snapshot", "line": 100}],
    )
    row = service._connection.execute("SELECT payload FROM errors").fetchone()
    text = service.format_error(json.loads(row[0]), 1)
    for expected in ("sendMessage", "10", "user_notifier.py", "client.py:100", "15"):
        assert expected in text
