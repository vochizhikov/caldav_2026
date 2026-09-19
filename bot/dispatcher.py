import logging

from aiogram import Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import ErrorEvent

from bot.handlers.admin_router import build_router as admin_router
from bot.handlers.user_router import build_router as user_router
from bot.middlewares.session import SessionMiddleware

logger = logging.getLogger(__name__)


def create_dispatcher(sessions, synchronizer, settings):
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    middleware = SessionMiddleware(sessions, synchronizer, settings)
    dispatcher.message.outer_middleware(middleware)
    dispatcher.callback_query.outer_middleware(middleware)
    dispatcher.include_routers(admin_router(), user_router())

    @dispatcher.errors()
    async def handle_error(event: ErrorEvent):
        # Never log the update, message text, or exception body: they may contain passwords.
        logger.error("Telegram handler failed (%s)", type(event.exception).__name__)
        message = event.update.message
        if event.update.callback_query:
            message = event.update.callback_query.message
        if message:
            try:
                await message.answer(
                    "⚠️ Не удалось выполнить действие. Попробуйте ещё раз или /cancel."
                )
            except Exception:
                logger.warning("Could not deliver handler error message")
        return True

    return dispatcher
