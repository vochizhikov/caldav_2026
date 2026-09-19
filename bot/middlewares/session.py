from aiogram import BaseMiddleware
from aiogram.enums import ChatType
from aiogram.types import CallbackQuery, Message

from bot.repositories.user import get_or_create


class SessionMiddleware(BaseMiddleware):
    def __init__(self, sessions, synchronizer, settings):
        self.sessions = sessions
        self.synchronizer = synchronizer
        self.settings = settings

    async def __call__(self, handler, event, data):
        message = event.message if isinstance(event, CallbackQuery) else event
        if not isinstance(message, Message) or not event.from_user:
            return
        if message.chat.type != ChatType.PRIVATE:
            if isinstance(event, CallbackQuery):
                await event.answer("💬 Откройте личный чат с ботом.", show_alert=True)
            elif message.text and message.text.startswith("/start"):
                await message.answer("💬 Для подключения календаря откройте личный чат с ботом.")
            return
        async with self.sessions() as session:
            user = await get_or_create(session, event.from_user.id, self.settings.default_timezone)
            await session.commit()
            async with self.synchronizer.locks[user.id]:
                await session.refresh(user)
                data.update(session=session, user=user)
                try:
                    result = await handler(event, data)
                    await session.commit()
                    return result
                except BaseException:
                    await session.rollback()
                    raise
