from aiogram import Router
from aiogram.filters import Command
from aiogram.types import Message
from sqlalchemy import func, select

from db.models import MyCalendar, User


def build_router():
    router = Router(name="admin")

    @router.message(Command("stats"))
    async def stats(message: Message, session, settings):
        if message.from_user.id not in settings.admin_ids:
            await message.answer("🔒 Команда доступна администратору.")
            return
        users = await session.scalar(select(func.count(User.id)))
        calendars = await session.scalar(select(func.count(MyCalendar.id)))
        await message.answer(
            f"📊 <b>Статистика бота</b>\n👥 Пользователей: {users}\n📅 Календарей: {calendars}"
        )

    return router
