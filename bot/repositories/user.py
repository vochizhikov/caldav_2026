from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from db.models import User


async def get_or_create(session: AsyncSession, telegram_id: int, timezone: str) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
    if user is None:
        user = User(telegram_id=telegram_id, chat_id=telegram_id, timezone=timezone)
        session.add(user)
        await session.flush()
    return user


async def connected_ids(session: AsyncSession) -> list[int]:
    return list(await session.scalars(select(User.id).where(User.encrypted_password.is_not(None))))
