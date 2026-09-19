from aiogram import Router
from aiogram.types import Message

from bot.handlers.common.user_routers import build_router as common_router
from bot.handlers.events.user_routers import build_router as events_router
from bot.handlers.menu.user_routers import build_router as menu_router
from bot.handlers.my_calendars.user_routers import build_router as calendars_router
from bot.handlers.settings.user_routers import build_router as settings_router


def build_router():
    router = Router(name="user")
    router.include_routers(
        common_router(), menu_router(), events_router(), settings_router(), calendars_router()
    )
    fallback = Router(name="fallback")

    @fallback.message()
    async def unknown(message: Message):
        await message.answer("🧭 Откройте /menu или /help. Для отмены ввода — /cancel.")

    router.include_router(fallback)
    return router
