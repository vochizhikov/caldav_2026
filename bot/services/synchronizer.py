import asyncio
import json
import logging
from collections import defaultdict
from contextlib import nullcontext
from datetime import datetime, timedelta
from uuid import uuid4

from sqlalchemy import delete, select, update

from bot.repositories.my_calendar import reconcile, user_calendars
from bot.repositories.user import connected_ids
from CalendarClient.changes import changed_until, instant_until
from CalendarClient.client import CalendarConnectionError
from CalendarClient.models import CalendarSnapshot, stable_key
from db.models import Event, MyCalendar, Notification, Occurrence, User, utcnow

logger = logging.getLogger(__name__)


async def apply_snapshot(
    session, calendar: MyCalendar, user: User, snapshot: CalendarSnapshot, now: datetime
):
    """Persist the complete snapshot and change notifications in one transaction."""
    if not calendar.initialized:
        calendar.notifications_since = now
        await session.execute(
            update(Notification)
            .where(Notification.calendar_id == calendar.id, Notification.status == "pending")
            .values(status="discarded")
        )
    previous = {
        event.remote_key: event
        for event in await session.scalars(select(Event).where(Event.calendar_id == calendar.id))
    }
    incoming = {event.key: event for event in snapshot.events}

    def notify(kind, event, old=None, relevant_until=None):
        if not calendar.enabled or not calendar.initialized or not user.notifications_enabled:
            return
        if not getattr(user, f"notify_{kind}"):
            return
        payload = {
            "summary": event.summary,
            "starts_at": event.starts_at.isoformat(),
            "all_day": event.all_day,
            "calendar": calendar.name,
            "location": event.location,
            "meeting_url": event.meeting_url,
        }
        if old is not None:
            payload["previous"] = {
                "summary": old.summary,
                "starts_at": old.starts_at.isoformat(),
                "all_day": old.all_day,
            }
        if relevant_until is not None:
            payload["relevant_until"] = relevant_until.isoformat()
        session.add(
            Notification(
                user_id=user.id,
                calendar_id=calendar.id,
                kind=kind,
                dedupe_key=stable_key(uuid4().hex),
                payload=json.dumps(payload, ensure_ascii=False),
                created_at=now,
                next_attempt_at=now,
                expires_at=now + timedelta(days=1),
            )
        )

    for key, remote in incoming.items():
        old = previous.get(key)
        if old is None:
            if not remote.cancelled:
                notify("created", remote)
            session.add(
                Event(
                    calendar_id=calendar.id,
                    remote_key=key,
                    uid=remote.uid,
                    fingerprint=remote.fingerprint,
                    change_state=json.dumps(remote.change_state) if remote.change_state else None,
                    summary=remote.summary,
                    starts_at=remote.starts_at,
                    all_day=remote.all_day,
                    cancelled=remote.cancelled,
                    location=remote.location,
                    meeting_url=remote.meeting_url,
                )
            )
        elif old.fingerprint != remote.fingerprint:
            if remote.cancelled and not old.cancelled:
                notify("deleted", remote)
            elif old.cancelled and not remote.cancelled:
                notify("created", remote)
            elif not remote.cancelled:
                if remote.change_state is None:
                    # Keep support for simple snapshots supplied without iCalendar metadata.
                    relevant_until = instant_until(max(old.starts_at, remote.starts_at))
                else:
                    relevant_until = changed_until(
                        json.loads(old.change_state) if old.change_state else None,
                        remote.change_state,
                    )
                if relevant_until is not None and relevant_until > now:
                    notify("updated", remote, old, relevant_until)
            old.fingerprint = remote.fingerprint
            old.summary = remote.summary
            old.starts_at = remote.starts_at
            old.all_day = remote.all_day
            old.cancelled = remote.cancelled
            old.revision += 1
        else:
            # A floating/all-day event can change its UTC instant after a timezone change.
            old.starts_at = remote.starts_at
        if old is not None:
            old.change_state = json.dumps(remote.change_state) if remote.change_state else None
            # Backfill display details even when the event fingerprint is unchanged.
            old.location = remote.location
            old.meeting_url = remote.meeting_url
    for key, old in previous.items():
        if key not in incoming:
            if not old.cancelled:
                notify("deleted", old)
            await session.delete(old)

    occurrences = {
        item.remote_key: item
        for item in await session.scalars(
            select(Occurrence).where(Occurrence.calendar_id == calendar.id)
        )
    }
    for remote in snapshot.occurrences:
        local = occurrences.pop(remote.key, None)
        if local is None:
            local = Occurrence(calendar_id=calendar.id, remote_key=remote.key)
            session.add(local)
        local.summary = remote.summary
        local.location = remote.location
        local.meeting_url = remote.meeting_url
        local.starts_at = remote.starts_at
        local.ends_at = remote.ends_at
        local.all_day = remote.all_day
    for local in occurrences.values():
        await session.delete(local)
    calendar.initialized = True
    calendar.last_synced_at = now
    calendar.last_error = None


class Synchronizer:
    def __init__(self, sessions, client, vault, settings):
        self.sessions = sessions
        self.client = client
        self.vault = vault
        self.settings = settings
        self.locks = defaultdict(asyncio.Lock)

    async def sync_user(self, user_id: int, *, already_locked: bool = False) -> None:
        # Handlers already hold this lock through SessionMiddleware.
        async with nullcontext() if already_locked else self.locks[user_id]:
            async with self.sessions() as session:
                user = await session.get(User, user_id)
                if user is None or not user.encrypted_password:
                    return
                login = user.yandex_login
                password = self.vault.decrypt(user.encrypted_password)
                timezone = user.timezone
            try:
                calendars = await self.client.calendars(login, password)
            except CalendarConnectionError as exc:
                async with self.sessions.begin() as session:
                    for calendar in await user_calendars(session, user_id):
                        calendar.last_error = str(exc)
                return
            async with self.sessions.begin() as session:
                await reconcile(session, user_id, calendars)
                selected = [c for c in await user_calendars(session, user_id) if c.enabled]
            for selected_calendar in selected:
                now = utcnow()
                try:
                    snapshot = await self.client.snapshot(
                        login,
                        password,
                        selected_calendar.url,
                        timezone,
                        now - timedelta(days=1),
                        now + timedelta(days=self.settings.event_horizon_days),
                    )
                except CalendarConnectionError as exc:
                    async with self.sessions.begin() as session:
                        calendar = await session.get(MyCalendar, selected_calendar.id)
                        if calendar:
                            calendar.last_error = str(exc)
                    continue
                async with self.sessions.begin() as session:
                    user = await session.get(User, user_id)
                    calendar = await session.get(MyCalendar, selected_calendar.id)
                    if user and calendar and calendar.enabled:
                        await apply_snapshot(session, calendar, user, snapshot, utcnow())

    async def run(self) -> None:
        while True:
            try:
                async with self.sessions() as session:
                    ids = await connected_ids(session)
                for user_id in ids:
                    try:
                        await self.sync_user(user_id)
                    except Exception as exc:
                        logger.error(
                            "Synchronization failed for user=%s (%s)", user_id, type(exc).__name__
                        )
            except Exception as exc:
                logger.error("Synchronization cycle failed (%s)", type(exc).__name__)
            await asyncio.sleep(self.settings.sync_interval_seconds)


async def disconnect(session, user: User):
    await session.execute(delete(MyCalendar).where(MyCalendar.user_id == user.id))
    user.yandex_login = None
    user.encrypted_password = None
