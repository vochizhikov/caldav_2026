import json
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from bot.services.synchronizer import apply_snapshot
from bot.services.user_notifier import UserNotifier
from CalendarClient.client import parse_snapshot
from db.models import Event, MyCalendar, Notification, User


def component(start="20260918T100000Z", extra="", summary="Meeting"):
    return (
        "BEGIN:VEVENT\nUID:meeting\nDTSTAMP:20260901T000000Z\n"
        f"DTSTART:{start}\nSUMMARY:{summary}\n{extra}\nEND:VEVENT"
    )


def parse(components, now, timezone="Europe/Moscow"):
    raw = f"BEGIN:VCALENDAR\nVERSION:2.0\n{components}\nEND:VCALENDAR"
    return parse_snapshot([raw], timezone, now - timedelta(days=1), now + timedelta(days=45))


async def apply(sessions, account, snapshot, now):
    async with sessions.begin() as session:
        await apply_snapshot(
            session,
            await session.get(MyCalendar, account[1]),
            await session.get(User, account[0]),
            snapshot,
            now,
        )


async def updates(sessions):
    async with sessions() as session:
        return list(
            await session.scalars(select(Notification).where(Notification.kind == "updated"))
        )


@pytest.mark.parametrize(
    ("start", "extra", "expected"),
    [
        ("20221216T091000Z", "", False),
        ("20260918T080000Z", "DTEND:20260918T090000Z", False),
        ("20260918T080000Z", "DTEND:20260918T100000Z", True),
        ("20260918T090000Z", "", True),
        ("20260918T100000Z", "", True),
        ("20221216T091000Z", "RRULE:FREQ=DAILY;COUNT=3", False),
        ("20221216T091000Z", "RRULE:FREQ=WEEKLY;UNTIL=20230101T000000Z", False),
        ("20221216T091000Z", "RRULE:FREQ=WEEKLY", True),
        ("20221216T091000Z", "RRULE:FREQ=YEARLY", True),
    ],
)
async def test_updates_only_for_events_that_are_not_over(
    sessions, account, now, start, extra, expected
):
    before = parse(component(start, extra), now)
    after = parse(component(start, extra, "Changed"), now)
    await apply(sessions, account, before, now)
    await apply(sessions, account, after, now)
    assert bool(await updates(sessions)) is expected
    # Historical edits are still saved; repeating the same snapshot remains silent.
    async with sessions() as session:
        event = await session.scalar(select(Event))
        assert event.summary == "Changed" and event.revision == 2
        assert event.fingerprint == after.events[0].fingerprint
    await apply(sessions, account, after, now + timedelta(seconds=1))
    assert len(await updates(sessions)) == int(expected)


@pytest.mark.parametrize("exception_date", ["20260910T100000Z", "20260919T100000Z"])
@pytest.mark.parametrize("operation", ["add", "edit", "remove"])
async def test_changes_to_only_a_past_exception_are_silent(
    sessions, account, now, exception_date, operation
):
    master = component("20260101T100000Z", "RRULE:FREQ=DAILY")
    original = component(exception_date, f"RECURRENCE-ID:{exception_date}", "Exception")
    changed = component(exception_date, f"RECURRENCE-ID:{exception_date}", "Changed")
    before = master if operation == "add" else master + "\n" + original
    after = master if operation == "remove" else master + "\n" + changed
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert bool(await updates(sessions)) is (exception_date == "20260919T100000Z")


@pytest.mark.parametrize("property_name", ["EXDATE", "RDATE"])
@pytest.mark.parametrize("entry", ["20260910T110000Z", "20260919T110000Z"])
@pytest.mark.parametrize("remove", [False, True])
async def test_past_recurrence_dates_do_not_notify(
    sessions, account, now, property_name, entry, remove
):
    original = component("20260101T100000Z", "RRULE:FREQ=DAILY")
    changed = component("20260101T100000Z", f"RRULE:FREQ=DAILY\n{property_name}:{entry}")
    before, after = (changed, original) if remove else (original, changed)
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert bool(await updates(sessions)) is (entry == "20260919T110000Z")


@pytest.mark.parametrize(
    ("old_start", "new_start"),
    [
        ("20260910T100000Z", "20260919T100000Z"),
        ("20260919T100000Z", "20260910T100000Z"),
    ],
)
async def test_moving_between_past_and_future_notifies(
    sessions, account, now, old_start, new_start
):
    await apply(sessions, account, parse(component(old_start), now), now)
    await apply(sessions, account, parse(component(new_start), now), now)
    assert len(await updates(sessions)) == 1


async def test_past_range_exception_affecting_future_still_notifies(sessions, account, now):
    master = component("20260101T100000Z", "RRULE:FREQ=DAILY")
    exception = component("20260910T110000Z", "RECURRENCE-ID;RANGE=THISANDFUTURE:20260910T100000Z")
    before = master + "\n" + exception
    after = master + "\n" + exception.replace("SUMMARY:Meeting", "SUMMARY:Changed")
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert len(await updates(sessions)) == 1


async def test_editing_finished_exception_moved_from_future_is_silent(sessions, account, now):
    master = component("20260101T100000Z", "RRULE:FREQ=DAILY")
    exception = component("20260910T100000Z", "RECURRENCE-ID:20260925T100000Z")
    before = master + "\n" + exception
    after = master + "\n" + exception.replace("SUMMARY:Meeting", "SUMMARY:Changed")
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert await updates(sessions) == []


@pytest.mark.parametrize("remove", [False, True])
async def test_moving_future_slot_into_past_and_restoring_it_notifies(
    sessions, account, now, remove
):
    master = component("20260101T100000Z", "RRULE:FREQ=DAILY")
    exception = component("20260910T100000Z", "RECURRENCE-ID:20260925T100000Z")
    before, after = master, master + "\n" + exception
    if remove:
        before, after = after, before
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert len(await updates(sessions)) == 1


@pytest.mark.parametrize("day", ["20260917", "20260918", "20260919"])
async def test_all_day_event_uses_local_end_of_day(sessions, account, now, day):
    before = component().replace("DTSTART:20260918T100000Z", f"DTSTART;VALUE=DATE:{day}")
    after = before.replace("SUMMARY:Meeting", "SUMMARY:Changed")
    await apply(sessions, account, parse(before, now), now)
    await apply(sessions, account, parse(after, now), now)
    assert bool(await updates(sessions)) is (day != "20260917")


async def test_old_database_snapshot_gets_silent_change_baseline(sessions, account, now):
    await apply(sessions, account, parse(component(), now), now)
    async with sessions.begin() as session:
        event = await session.scalar(select(Event))
        event.change_state = None
    changed = parse(component(summary="Changed"), now)
    await apply(sessions, account, changed, now)
    assert await updates(sessions) == []
    await apply(sessions, account, parse(component(summary="Changed again"), now), now)
    assert len(await updates(sessions)) == 1


@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("future", [False, True])
async def test_delivery_discards_past_updates_including_old_queue(
    sessions, account, settings, now, legacy, future
):
    boundary = now + timedelta(hours=1 if future else -1)
    payload = {
        "summary": "Meeting",
        "starts_at": boundary.isoformat(),
        "all_day": False,
        "calendar": "Work",
    }
    if not legacy:
        payload["relevant_until"] = boundary.isoformat()
    async with sessions.begin() as session:
        (await session.get(MyCalendar, account[1])).initialized = True
        session.add(
            Notification(
                user_id=account[0],
                calendar_id=account[1],
                kind="updated",
                dedupe_key="update",
                payload=json.dumps(payload),
                created_at=now,
                next_attempt_at=now,
                expires_at=now + timedelta(days=1),
            )
        )
    bot = AsyncMock()
    await UserNotifier(bot, sessions, settings).deliver_pending(now)
    assert bot.send_message.await_count == int(future)
    assert (await updates(sessions))[0].status == ("sent" if future else "discarded")


async def test_update_expires_when_meeting_ends_while_waiting_for_delivery(
    sessions, account, settings, now
):
    original = component(extra="DTEND:20260918T110000Z")
    await apply(sessions, account, parse(original, now), now)
    await apply(sessions, account, parse(original.replace("Meeting", "Changed"), now), now)
    assert len(await updates(sessions)) == 1
    bot = AsyncMock()
    await UserNotifier(bot, sessions, settings).deliver_pending(now + timedelta(hours=2))
    bot.send_message.assert_not_awaited()
    assert (await updates(sessions))[0].status == "discarded"
