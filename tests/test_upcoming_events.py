from datetime import UTC, date, datetime, timedelta

import pytest

from bot.repositories.events import upcoming, upcoming_on_day
from db.models import MyCalendar, Occurrence, User


async def add_occurrences(sessions, calendar_id, starts):
    async with sessions.begin() as session:
        session.add_all(
            Occurrence(
                calendar_id=calendar_id,
                remote_key=f"meeting-{index}",
                summary=f"Meeting {index}",
                starts_at=starts_at,
                ends_at=starts_at + timedelta(hours=1),
            )
            for index, starts_at in enumerate(starts)
        )


async def test_default_selects_first_occupied_day_even_after_seven_empty_days(
    sessions, account, now
):
    first = now + timedelta(days=10)
    await add_occurrences(
        sessions, account[1], [first, first + timedelta(hours=1), first + timedelta(days=1)]
    )

    async with sessions() as session:
        rows = await upcoming(session, account[0], now)

    assert [row.Occurrence.starts_at for row in rows] == [first, first + timedelta(hours=1)]
    assert all(row.MyCalendar.id == account[1] for row in rows)


@pytest.mark.parametrize("days", range(1, 8))
async def test_counts_nonempty_dates_and_includes_all_their_events(sessions, account, now, days):
    starts = [now + timedelta(days=day * 3, hours=hour) for day in range(8) for hour in (0, 1)]
    # Insert out of chronological order to check the query's ordering as well.
    await add_occurrences(sessions, account[1], reversed(starts))

    async with sessions() as session:
        rows = await upcoming(session, account[0], now, days=days)

    assert [row.Occurrence.starts_at for row in rows] == starts[: days * 2]


async def test_does_not_truncate_full_day_at_twenty_or_stream_batch_size(sessions, account, now):
    starts = [now + timedelta(minutes=minute) for minute in range(125)]
    await add_occurrences(sessions, account[1], [*starts, now + timedelta(days=1)])

    async with sessions() as session:
        rows = await upcoming(session, account[0], now)

    assert [row.Occurrence.starts_at for row in rows] == starts


@pytest.mark.parametrize(
    ("timezone", "now", "starts", "expected"),
    [
        (
            "Europe/Moscow",
            datetime(2026, 9, 18, 20, 0, tzinfo=UTC),
            [
                datetime(2026, 9, 18, 20, 30, tzinfo=UTC),
                datetime(2026, 9, 18, 21, 0, tzinfo=UTC),
            ],
            [0],
        ),
        (
            "America/New_York",
            datetime(2026, 9, 18, 22, 0, tzinfo=UTC),
            [
                datetime(2026, 9, 18, 23, 30, tzinfo=UTC),
                datetime(2026, 9, 19, 3, 59, tzinfo=UTC),
                datetime(2026, 9, 19, 4, 0, tzinfo=UTC),
            ],
            [0, 1],
        ),
        (
            "America/New_York",
            datetime(2026, 11, 1, 0, 0, tzinfo=UTC),
            [
                datetime(2026, 11, 1, 4, 30, tzinfo=UTC),
                datetime(2026, 11, 1, 5, 30, tzinfo=UTC),
                datetime(2026, 11, 1, 6, 30, tzinfo=UTC),
                datetime(2026, 11, 2, 4, 59, tzinfo=UTC),
                datetime(2026, 11, 2, 5, 0, tzinfo=UTC),
            ],
            [0, 1, 2, 3],
        ),
    ],
    ids=["moscow-midnight", "new-york-midnight", "new-york-25-hour-day"],
)
async def test_groups_by_local_date(sessions, account, timezone, now, starts, expected):
    await add_occurrences(sessions, account[1], starts)

    async with sessions() as session:
        rows = await upcoming(session, account[0], now, timezone=timezone)

    assert [row.Occurrence.starts_at for row in rows] == [starts[index] for index in expected]


async def test_excludes_past_disabled_and_other_users_calendars(sessions, account, now):
    async with sessions.begin() as session:
        foreign_user = User(telegram_id=2, chat_id=2)
        session.add(foreign_user)
        await session.flush()
        disabled = MyCalendar(
            user_id=account[0],
            remote_key="disabled",
            name="Disabled",
            url="disabled",
            enabled=False,
        )
        foreign = MyCalendar(
            user_id=foreign_user.id,
            remote_key="foreign",
            name="Foreign",
            url="foreign",
            enabled=True,
        )
        enabled = MyCalendar(
            user_id=account[0], remote_key="second", name="Second", url="second", enabled=True
        )
        session.add_all([disabled, foreign, enabled])
        await session.flush()
        disabled_id, foreign_id, enabled_id = disabled.id, foreign.id, enabled.id

    # Excluded calendars must not consume one of the selected occupied dates.
    await add_occurrences(sessions, disabled_id, [now])
    await add_occurrences(sessions, foreign_id, [now + timedelta(days=1)])
    first = now + timedelta(days=2)
    await add_occurrences(sessions, account[1], [now - timedelta(minutes=1), first])
    await add_occurrences(sessions, enabled_id, [first + timedelta(hours=1)])

    async with sessions() as session:
        rows = await upcoming(session, account[0], now)

    assert [row.Occurrence.starts_at for row in rows] == [first, first + timedelta(hours=1)]
    assert [row.MyCalendar.id for row in rows] == [account[1], enabled_id]


async def test_returns_all_available_dates_when_fewer_than_requested(sessions, account, now):
    await add_occurrences(sessions, account[1], [now, now + timedelta(days=2)])

    async with sessions() as session:
        rows = await upcoming(session, account[0], now, days=7)

    assert len(rows) == 2


async def test_empty_cache_returns_no_events(sessions, account, now):
    async with sessions() as session:
        assert await upcoming(session, account[0], now) == []


async def test_equal_start_times_have_stable_order(sessions, account, now):
    await add_occurrences(sessions, account[1], [now, now, now])

    async with sessions() as session:
        rows = await upcoming(session, account[0], now)

    ids = [row.Occurrence.id for row in rows]
    assert ids == sorted(ids)


@pytest.mark.parametrize("days", [0, 8, -1])
async def test_rejects_out_of_range_day_count(sessions, account, now, days):
    async with sessions() as session:
        with pytest.raises(ValueError, match="between 1 and 7"):
            await upcoming(session, account[0], now, days=days)


@pytest.mark.parametrize(
    ("timezone", "day", "start", "end"),
    [
        (
            "Europe/Moscow",
            date(2026, 9, 19),
            datetime(2026, 9, 18, 21, tzinfo=UTC),
            datetime(2026, 9, 19, 21, tzinfo=UTC),
        ),
        (
            "America/New_York",
            date(2026, 11, 1),
            datetime(2026, 11, 1, 4, tzinfo=UTC),
            datetime(2026, 11, 2, 5, tzinfo=UTC),
        ),
    ],
)
async def test_catalog_day_uses_local_boundaries_and_original_opening_time(
    sessions, account, timezone, day, start, end
):
    starts = [
        start - timedelta(minutes=1),
        start,
        start + timedelta(hours=1),
        end - timedelta(minutes=1),
        end,
    ]
    await add_occurrences(sessions, account[1], starts)
    opened_at = start + timedelta(minutes=30)
    async with sessions() as session:
        rows = await upcoming_on_day(session, account[0], opened_at, day, timezone)
    assert [row.Occurrence.starts_at for row in rows] == starts[2:4]
