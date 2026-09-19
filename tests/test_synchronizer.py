from dataclasses import replace
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

from sqlalchemy import func, select

from bot.repositories.my_calendar import reconcile, user_calendars
from bot.services.synchronizer import Synchronizer, apply_snapshot, disconnect
from CalendarClient.client import CalendarConnectionError, parse_snapshot
from CalendarClient.models import CalendarSnapshot, RemoteCalendar, RemoteEvent, RemoteOccurrence
from db.models import Event, MyCalendar, Notification, Occurrence, User


def snapshot(now, summary="Встреча", fingerprint="first", cancelled=False):
    return CalendarSnapshot(
        [RemoteEvent("key", "uid", fingerprint, summary, now, False, cancelled)],
        []
        if cancelled
        else [
            RemoteOccurrence("occurrence", summary, "Офис", now, now + timedelta(hours=1), False)
        ],
    )


async def apply(sessions, account, data, now):
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        calendar = await session.get(MyCalendar, account[1])
        await apply_snapshot(session, calendar, user, data, now)


async def kinds(sessions):
    async with sessions() as session:
        return list(await session.scalars(select(Notification.kind).order_by(Notification.id)))


async def test_initial_sync_and_unchanged_resync_are_silent(sessions, account, now):
    await apply(sessions, account, snapshot(now), now)
    await apply(sessions, account, snapshot(now), now + timedelta(seconds=60))
    assert await kinds(sessions) == []
    async with sessions() as session:
        assert await session.scalar(select(func.count(Event.id))) == 1
        assert await session.scalar(select(func.count(Occurrence.id))) == 1


async def test_created_updated_and_deleted(sessions, account, now):
    await apply(sessions, account, CalendarSnapshot(), now)
    await apply(sessions, account, snapshot(now), now)
    await apply(sessions, account, snapshot(now, "Перенос", "second"), now)
    await apply(sessions, account, CalendarSnapshot(), now)
    assert await kinds(sessions) == ["created", "updated", "deleted"]


async def test_cancelled_then_removed_is_only_one_deletion(sessions, account, now):
    await apply(sessions, account, snapshot(now), now)
    await apply(sessions, account, snapshot(now, fingerprint="cancel", cancelled=True), now)
    await apply(sessions, account, CalendarSnapshot(), now)
    assert await kinds(sessions) == ["deleted"]


async def test_notification_preferences_and_disabled_changes_do_not_replay(sessions, account, now):
    await apply(sessions, account, CalendarSnapshot(), now)
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.notifications_enabled = False
    await apply(sessions, account, snapshot(now), now)
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        user.notifications_enabled = True
        user.notify_updated = False
    await apply(sessions, account, snapshot(now, fingerprint="second"), now)
    assert await kinds(sessions) == []


async def test_moving_event_outside_reminder_window_is_update_not_deletion(sessions, account, now):
    original = snapshot(now)
    await apply(sessions, account, original, now)
    later = replace(original.events[0], starts_at=now + timedelta(days=100), fingerprint="moved")
    await apply(sessions, account, CalendarSnapshot([later], []), now)
    assert await kinds(sessions) == ["updated"]


async def test_failed_caldav_read_preserves_events(sessions, account, settings, now):
    await apply(sessions, account, snapshot(now), now)
    async with sessions.begin() as session:
        calendar = await session.get(MyCalendar, account[1])
        remote = RemoteCalendar(calendar.url, calendar.name)
        calendar.remote_key = remote.key
    client = Mock()
    client.calendars = AsyncMock(return_value=[remote])
    client.snapshot = AsyncMock(side_effect=CalendarConnectionError("offline"))
    synchronizer = Synchronizer(
        sessions, client, Mock(decrypt=Mock(return_value="secret")), settings
    )
    await synchronizer.sync_user(account[0])
    assert await kinds(sessions) == []
    async with sessions() as session:
        assert await session.scalar(select(func.count(Event.id))) == 1
        assert (await session.get(MyCalendar, account[1])).last_error


async def test_disconnect_removes_cached_data_and_outbox(sessions, account, now):
    await apply(sessions, account, CalendarSnapshot(), now)
    await apply(sessions, account, snapshot(now), now)
    async with sessions.begin() as session:
        user = await session.get(User, account[0])
        await disconnect(session, user)
    async with sessions() as session:
        for model in (MyCalendar, Event, Occurrence, Notification):
            assert await session.scalar(select(func.count(model.id))) == 0
        assert (await session.get(User, account[0])).encrypted_password is None


async def test_new_calendars_stay_disabled_while_existing_selection_is_preserved(sessions, account):
    async with sessions.begin() as session:
        existing = await session.get(MyCalendar, account[1])
        remote = RemoteCalendar(existing.url, existing.name)
        existing.remote_key = remote.key
        new = RemoteCalendar("https://caldav.yandex.ru/new/", "Новый")
        await reconcile(session, account[0], [remote, new])
        calendars = await user_calendars(session, account[0])
        assert calendars[0].enabled
        assert not calendars[1].enabled and not calendars[1].initialized
        await reconcile(session, account[0], [remote, new])
        assert calendars[0].enabled and not calendars[1].enabled


async def test_disabled_calendars_are_not_downloaded(sessions, account, settings):
    client = Mock()
    client.calendars = AsyncMock(
        return_value=[RemoteCalendar("https://caldav.yandex.ru/new/", "Новый")]
    )
    client.snapshot = AsyncMock()
    synchronizer = Synchronizer(
        sessions, client, Mock(decrypt=Mock(return_value="secret")), settings
    )
    await synchronizer.sync_user(account[0])
    client.snapshot.assert_not_awaited()
    async with sessions() as session:
        assert all(not c.enabled for c in await user_calendars(session, account[0]))


async def test_reenable_discards_pending_and_silently_replaces_snapshot(sessions, account, now):
    original = snapshot(now + timedelta(hours=1)).events[0]
    removed = replace(original, key="removed", uid="removed")
    await apply(sessions, account, CalendarSnapshot([original, removed], []), now)
    pending = replace(original, fingerprint="pending-update")
    await apply(
        sessions, account, CalendarSnapshot([pending, removed], []), now + timedelta(seconds=1)
    )
    assert await kinds(sessions) == ["updated"]
    async with sessions.begin() as session:
        calendar = await session.get(MyCalendar, account[1])
        calendar.initialized = False
        calendar.last_synced_at = None
    changed = replace(original, fingerprint="changed-offline")
    created = replace(original, key="created-offline", uid="created-offline")
    baseline = now + timedelta(minutes=2)
    await apply(sessions, account, CalendarSnapshot([changed, created], []), baseline)
    async with sessions() as session:
        queue = list(await session.scalars(select(Notification)))
        assert len(queue) == 1 and queue[0].status == "discarded"
        assert (await session.get(MyCalendar, account[1])).notifications_since == baseline
        assert set(await session.scalars(select(Event.remote_key))) == {changed.key, created.key}
    await apply(
        sessions,
        account,
        CalendarSnapshot([replace(changed, fingerprint="new-change"), created], []),
        baseline + timedelta(minutes=1),
    )
    async with sessions() as session:
        pending_kinds = list(
            await session.scalars(select(Notification.kind).where(Notification.status == "pending"))
        )
        assert pending_kinds == ["updated"]
        assert (await session.get(MyCalendar, account[1])).notifications_since == baseline


async def test_reordered_attendees_on_historical_series_do_not_flood_notifications(
    sessions,
    account,
    now,
):
    first = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.test"
    second = "ATTENDEE;PARTSTAT=TENTATIVE:mailto:bob@example.test"

    def payload(attendees, stamp):
        return (
            "BEGIN:VCALENDAR\nVERSION:2.0\nBEGIN:VEVENT\nUID:work-series\n"
            "DTSTART:20221216T091000Z\nSUMMARY:Демо\nRRULE:FREQ=WEEKLY\n"
            f"DTSTAMP:{stamp}\n{attendees}\nEND:VEVENT\nEND:VCALENDAR"
        )

    def parse(raw):
        return parse_snapshot([raw], "Europe/Moscow", now, now + timedelta(days=45))

    await apply(sessions, account, parse(payload(f"{first}\n{second}", "20260918T090000Z")), now)
    for index in range(1, 4):
        attendees = f"{second}\n{first}" if index % 2 else f"{first}\n{second}"
        await apply(
            sessions,
            account,
            parse(payload(attendees, "20260918T091000Z")),
            now + timedelta(minutes=index),
        )
    assert await kinds(sessions) == []
    # A real participant response on an old but ongoing series must still be reported.
    changed = first.replace("ACCEPTED", "DECLINED")
    await apply(
        sessions,
        account,
        parse(payload(f"{second}\n{changed}", "20260918T092000Z")),
        now + timedelta(minutes=4),
    )
    assert await kinds(sessions) == ["updated"]
