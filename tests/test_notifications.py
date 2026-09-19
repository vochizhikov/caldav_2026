from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from aiogram.exceptions import TelegramForbiddenError, TelegramNetworkError, TelegramRetryAfter
from aiogram.methods import SendMessage
from sqlalchemy import select

from bot.services.user_notifier import UserNotifier
from db.models import MyCalendar, Notification, Occurrence, User


async def add_occurrence(sessions, account, starts_at):
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).initialized = True
        session.add(
            Occurrence(
                calendar_id=account[1],
                remote_key="meeting",
                summary="<Встреча>",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )
        )


@pytest.mark.parametrize("offset", [0, 1, 3, 5, 10])
async def test_reminder_offsets_and_persistent_deduplication(
    sessions, account, settings, now, offset
):
    await add_occurrence(sessions, account, now + timedelta(minutes=offset))
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.advance_minutes = offset or None
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.enqueue_reminders(now + timedelta(seconds=1))
    await notifier.deliver_pending(now)
    # A new service instance simulates a process restart with the same SQLite database.
    restarted = UserNotifier(bot, sessions, settings)
    await restarted.enqueue_reminders(now + timedelta(seconds=2))
    await restarted.deliver_pending(now + timedelta(seconds=2))
    assert bot.send_message.await_count == 1
    assert "&lt;Встреча&gt;" in bot.send_message.call_args.args[1]


async def test_start_and_advance_are_independent(sessions, account, settings, now):
    await add_occurrence(sessions, account, now + timedelta(minutes=5))
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    start = now + timedelta(minutes=5)
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).last_synced_at = start
    await notifier.enqueue_reminders(start)
    await notifier.deliver_pending(start)
    assert bot.send_message.await_count == 2


@pytest.mark.parametrize("disabled", ["notifications_enabled", "notify_reminder", "calendar"])
async def test_recheck_settings_before_sending(sessions, account, settings, now, disabled):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    async with sessions.begin() as session:
        if disabled == "calendar":
            (await session.get(MyCalendar, account[1])).enabled = False
        else:
            setattr(await session.get(User, account[0]), disabled, False)
    await notifier.deliver_pending(now)
    bot.send_message.assert_not_awaited()


async def test_moved_event_cancels_queued_reminder(sessions, account, settings, now):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    async with sessions.begin() as session:
        occurrence = await session.scalar(select(Occurrence))
        occurrence.starts_at = now + timedelta(hours=1)
    await notifier.deliver_pending(now)
    bot.send_message.assert_not_awaited()


async def test_stale_calendar_and_old_reminders_are_not_sent(sessions, account, settings, now):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).last_synced_at = now - timedelta(hours=1)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    bot.send_message.assert_not_awaited()
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).last_synced_at = now + timedelta(hours=1)
    await notifier.enqueue_reminders(now + timedelta(hours=1))
    await notifier.deliver_pending(now + timedelta(hours=1))
    bot.send_message.assert_not_awaited()


async def test_transient_telegram_failure_retries(sessions, account, settings, now):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramNetworkError(SendMessage(chat_id=1, text=""), "offline"),
        None,
    ]
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    async with sessions() as session:
        item = await session.scalar(select(Notification))
        assert item.status == "pending" and item.attempts == 1
    await notifier.deliver_pending(now + timedelta(seconds=3))
    async with sessions() as session:
        assert (await session.scalar(select(Notification))).status == "sent"


async def test_rate_limit_respects_retry_after(sessions, account, settings, now):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    bot.send_message.side_effect = [
        TelegramRetryAfter(SendMessage(chat_id=1, text=""), "slow", 10),
        None,
    ]
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    await notifier.deliver_pending(now + timedelta(seconds=5))
    assert bot.send_message.await_count == 1
    await notifier.deliver_pending(now + timedelta(seconds=12))
    assert bot.send_message.await_count == 2


async def test_blocked_bot_disables_notifications(sessions, account, settings, now):
    await add_occurrence(sessions, account, now)
    bot = AsyncMock()
    bot.send_message.side_effect = TelegramForbiddenError(
        SendMessage(chat_id=1, text=""), "blocked"
    )
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    async with sessions() as session:
        assert not (await session.get(User, account[0])).notifications_enabled
        assert (await session.scalar(select(Notification))).status == "discarded"


@pytest.mark.parametrize("offset", [0, 1, 3, 5, 10])
@pytest.mark.parametrize("lateness", [0, 20])
async def test_enabling_does_not_catch_up_due_reminders(
    sessions,
    account,
    settings,
    now,
    offset,
    lateness,
):
    await add_occurrence(sessions, account, now + timedelta(minutes=offset, seconds=-lateness))
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.advance_minutes = offset or None
        (await session.get(MyCalendar, account[1])).notifications_since = now
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    # The baseline is persistent and must still apply after a restart.
    restarted = UserNotifier(bot, sessions, settings)
    await restarted.enqueue_reminders(now + timedelta(seconds=5))
    await restarted.deliver_pending(now + timedelta(seconds=5))
    bot.send_message.assert_not_awaited()
    async with sessions() as session:
        assert await session.scalar(select(Notification.id)) is None


async def test_future_reminders_still_work_after_enabling(sessions, account, settings, now):
    await add_occurrence(sessions, account, now + timedelta(minutes=6))
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).notifications_since = now
    bot = AsyncMock()
    notifier = UserNotifier(bot, sessions, settings)
    await notifier.enqueue_reminders(now)
    await notifier.deliver_pending(now)
    bot.send_message.assert_not_awaited()
    await notifier.enqueue_reminders(now + timedelta(minutes=1))
    await notifier.deliver_pending(now + timedelta(minutes=1))
    bot.send_message.assert_awaited_once()
