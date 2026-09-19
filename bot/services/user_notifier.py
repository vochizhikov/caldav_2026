import asyncio
import json
import logging
from datetime import datetime, timedelta

from aiogram.exceptions import (
    TelegramAPIError,
    TelegramBadRequest,
    TelegramForbiddenError,
    TelegramRetryAfter,
)
from sqlalchemy import delete, select, update

from bot.utils.common import event_text
from bot.utils.settings_utils import reminder_offsets
from CalendarClient.changes import instant_until
from CalendarClient.models import stable_key
from db.models import MyCalendar, Notification, Occurrence, User, utcnow

logger = logging.getLogger(__name__)


def format_notification(notification: Notification, user: User) -> str:
    payload = json.loads(notification.payload)
    labels = {
        "created": "🆕 Событие создано",
        "updated": "✏️ Событие изменено",
        "deleted": "🗑 Событие удалено или отменено",
    }
    if notification.kind == "reminder":
        minutes = notification.offset_minutes
        label = f"🔔 Встреча через {minutes} мин." if minutes else "🔔 Встреча начинается"
    else:
        label = labels[notification.kind]
    text = f"<b>{label}</b>\n\n" + event_text(
        payload["summary"],
        datetime.fromisoformat(payload["starts_at"]),
        payload["all_day"],
        user.timezone,
        location=payload.get("location", ""),
        meeting_url=payload.get("meeting_url", ""),
        calendar_name=payload["calendar"],
    )
    if payload.get("previous"):
        previous = payload["previous"]
        text += "\n\n↩️ <b>Ранее:</b>\n" + event_text(
            previous["summary"],
            datetime.fromisoformat(previous["starts_at"]),
            previous["all_day"],
            user.timezone,
        )
    return text


class UserNotifier:
    def __init__(self, bot, sessions, settings):
        self.bot = bot
        self.sessions = sessions
        self.settings = settings
        self.paused_until: datetime | None = None

    def is_fresh(self, calendar: MyCalendar, now: datetime) -> bool:
        freshness = max(
            self.settings.sync_interval_seconds * 3, self.settings.caldav_timeout_seconds * 2
        )
        return bool(
            calendar.initialized
            and calendar.last_synced_at
            and not calendar.last_error
            and (now - calendar.last_synced_at).total_seconds() <= freshness
        )

    async def enqueue_reminders(self, now: datetime):
        grace = timedelta(seconds=self.settings.reminder_grace_seconds)
        async with self.sessions.begin() as session:
            rows = (
                await session.execute(
                    select(Occurrence, MyCalendar, User)
                    .join(MyCalendar, MyCalendar.id == Occurrence.calendar_id)
                    .join(User, User.id == MyCalendar.user_id)
                    .where(
                        MyCalendar.enabled.is_(True),
                        User.notifications_enabled.is_(True),
                        User.notify_reminder.is_(True),
                        User.encrypted_password.is_not(None),
                        Occurrence.starts_at >= now - grace,
                        Occurrence.starts_at <= now + timedelta(minutes=10),
                    )
                )
            ).all()
            for occurrence, calendar, user in rows:
                if not self.is_fresh(calendar, now):
                    continue
                for offset in reminder_offsets(user):
                    due = occurrence.starts_at - timedelta(minutes=offset)
                    if calendar.notifications_since and due <= calendar.notifications_since:
                        continue
                    if not due <= now < due + grace:
                        continue
                    # A late advance reminder must not arrive after the meeting has begun.
                    if offset and now >= occurrence.starts_at:
                        continue
                    key = stable_key(
                        str(calendar.id),
                        occurrence.remote_key,
                        occurrence.starts_at.isoformat(),
                        str(offset),
                    )
                    if await session.scalar(
                        select(Notification.id).where(Notification.dedupe_key == key)
                    ):
                        continue
                    payload = {
                        "summary": occurrence.summary,
                        "starts_at": occurrence.starts_at.isoformat(),
                        "all_day": occurrence.all_day,
                        "calendar": calendar.name,
                        "location": occurrence.location,
                        "meeting_url": occurrence.meeting_url,
                    }
                    expires = min(due + grace, occurrence.starts_at) if offset else due + grace
                    session.add(
                        Notification(
                            user_id=user.id,
                            calendar_id=calendar.id,
                            kind="reminder",
                            dedupe_key=key,
                            payload=json.dumps(payload, ensure_ascii=False),
                            occurrence_key=occurrence.remote_key,
                            starts_at=occurrence.starts_at,
                            offset_minutes=offset,
                            created_at=now,
                            next_attempt_at=now,
                            expires_at=expires,
                        )
                    )

    async def eligible(self, session, item, user, calendar, now):
        if not user or not calendar or not user.encrypted_password:
            return False
        if not user.notifications_enabled or not calendar.enabled or item.expires_at <= now:
            return False
        if not calendar.initialized:
            return False
        if calendar.notifications_since and item.created_at < calendar.notifications_since:
            return False
        if not getattr(user, f"notify_{item.kind}", False):
            return False
        if item.kind == "updated":
            payload = json.loads(item.payload)
            until = (
                datetime.fromisoformat(payload["relevant_until"])
                if payload.get("relevant_until")
                else instant_until(datetime.fromisoformat(payload["starts_at"]))
            )
            # Also discard historical updates queued by older versions or delayed by retries.
            if until <= now:
                return False
        if item.kind == "reminder":
            due = item.starts_at - timedelta(minutes=item.offset_minutes)
            if calendar.notifications_since and due <= calendar.notifications_since:
                return False
            if item.offset_minutes not in reminder_offsets(user) or not self.is_fresh(
                calendar, now
            ):
                return False
            occurrence = await session.scalar(
                select(Occurrence).where(
                    Occurrence.calendar_id == calendar.id,
                    Occurrence.remote_key == item.occurrence_key,
                    Occurrence.starts_at == item.starts_at,
                )
            )
            if occurrence is None:
                return False
            # Include edits that occurred while delivery was waiting for a retry.
            payload = json.loads(item.payload)
            payload.update(
                summary=occurrence.summary,
                location=occurrence.location,
                meeting_url=occurrence.meeting_url,
                calendar=calendar.name,
            )
            item.payload = json.dumps(payload, ensure_ascii=False)
        return True

    async def deliver_pending(self, now: datetime | None = None):
        query_time = now or utcnow()
        if self.paused_until and query_time < self.paused_until:
            return
        async with self.sessions() as session:
            ids = list(
                await session.scalars(
                    select(Notification.id)
                    .where(
                        Notification.status == "pending", Notification.next_attempt_at <= query_time
                    )
                    .order_by(Notification.expires_at, Notification.id)
                    .limit(100)
                )
            )
        for notification_id in ids:
            delivered_at = utcnow() if now is None else now
            async with self.sessions.begin() as session:
                item = await session.get(Notification, notification_id)
                if item is None or item.status != "pending":
                    continue
                user = await session.get(User, item.user_id)
                calendar = await session.get(MyCalendar, item.calendar_id)
                if not await self.eligible(session, item, user, calendar, delivered_at):
                    item.status = "discarded"
                    continue
                try:
                    await self.bot.send_message(user.chat_id, format_notification(item, user))
                except TelegramForbiddenError:
                    user.notifications_enabled = False
                    await session.execute(
                        update(Notification)
                        .where(Notification.user_id == user.id, Notification.status == "pending")
                        .values(status="discarded")
                    )
                except TelegramRetryAfter as exc:
                    self.paused_until = delivered_at + timedelta(seconds=exc.retry_after + 1)
                    item.next_attempt_at = self.paused_until
                    item.attempts += 1
                    return
                except TelegramBadRequest:
                    item.status = "discarded"
                    logger.warning("Telegram rejected notification=%s", item.id)
                except (TelegramAPIError, OSError):
                    item.attempts += 1
                    item.next_attempt_at = delivered_at + timedelta(
                        seconds=min(300, 2 ** min(item.attempts, 8))
                    )
                else:
                    item.status = "sent"
                    item.sent_at = delivered_at

    async def run(self):
        while True:
            try:
                now = utcnow()
                await self.enqueue_reminders(now)
                await self.deliver_pending()
                async with self.sessions.begin() as session:
                    await session.execute(
                        delete(Notification).where(
                            Notification.expires_at < now - timedelta(days=30)
                        )
                    )
            except Exception as exc:
                logger.error("Notification cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.settings.notifier_interval_seconds)
