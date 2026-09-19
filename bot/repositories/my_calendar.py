from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from CalendarClient.models import RemoteCalendar
from db.models import MyCalendar


async def user_calendars(session: AsyncSession, user_id: int) -> list[MyCalendar]:
    return list(
        await session.scalars(
            select(MyCalendar).where(MyCalendar.user_id == user_id).order_by(MyCalendar.id)
        )
    )


async def owned_calendar(session: AsyncSession, user_id: int, calendar_id: int):
    return await session.scalar(
        select(MyCalendar).where(MyCalendar.id == calendar_id, MyCalendar.user_id == user_id)
    )


async def reconcile(session: AsyncSession, user_id: int, remote: list[RemoteCalendar]):
    existing = {item.remote_key: item for item in await user_calendars(session, user_id)}
    remote_keys = {item.key for item in remote}
    for item in remote:
        local = existing.get(item.key)
        if local is None:
            session.add(
                MyCalendar(user_id=user_id, remote_key=item.key, url=item.url, name=item.name)
            )
        else:
            local.name = item.name
    for key, local in existing.items():
        if key not in remote_keys:
            # Keep the last snapshot for inspection, but never send stale reminders.
            local.enabled = False
            local.initialized = False
            local.last_error = "⚠️ Календарь удалён или больше недоступен этому аккаунту."
    await session.flush()
