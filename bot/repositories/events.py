from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MyCalendar, Occurrence


async def upcoming(
    session: AsyncSession,
    user_id: int,
    now: datetime,
    *,
    days: int = 1,
    timezone: str = "Europe/Moscow",
):
    """Return every future occurrence from the first occupied local dates."""
    if not 1 <= days <= 7:
        raise ValueError("Upcoming event days must be between 1 and 7")

    zone = ZoneInfo(timezone)
    result = await session.stream(
        select(Occurrence, MyCalendar)
        .join(MyCalendar, MyCalendar.id == Occurrence.calendar_id)
        .where(
            MyCalendar.user_id == user_id,
            MyCalendar.enabled.is_(True),
            Occurrence.starts_at >= now,
        )
        .order_by(Occurrence.starts_at, Occurrence.id)
        .execution_options(yield_per=100)
    )
    selected = []
    occupied_dates = set()
    try:
        async for row in result:
            local_date = row.Occurrence.starts_at.astimezone(zone).date()
            if local_date not in occupied_dates:
                if len(occupied_dates) == days:
                    break
                occupied_dates.add(local_date)
            selected.append(row)
    finally:
        await result.close()
    return selected


async def upcoming_on_day(
    session: AsyncSession, user_id: int, opened_at: datetime, day: date, timezone: str
):
    zone = ZoneInfo(timezone)
    start = datetime.combine(day, time.min, tzinfo=zone).astimezone(UTC)
    end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone).astimezone(UTC)
    return (
        await session.execute(
            select(Occurrence, MyCalendar)
            .join(MyCalendar, MyCalendar.id == Occurrence.calendar_id)
            .where(
                MyCalendar.user_id == user_id,
                MyCalendar.enabled.is_(True),
                Occurrence.starts_at >= max(opened_at, start),
                Occurrence.starts_at < end,
            )
            .order_by(Occurrence.starts_at, Occurrence.id)
        )
    ).all()
