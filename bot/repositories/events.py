from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import MyCalendar, Occurrence


async def upcoming(session: AsyncSession, user_id: int, now: datetime):
    return (
        await session.execute(
            select(Occurrence, MyCalendar)
            .join(MyCalendar, MyCalendar.id == Occurrence.calendar_id)
            .where(
                MyCalendar.user_id == user_id,
                MyCalendar.enabled.is_(True),
                Occurrence.starts_at >= now,
                Occurrence.starts_at < now + timedelta(days=7),
            )
            .order_by(Occurrence.starts_at)
            .limit(20)
        )
    ).all()
