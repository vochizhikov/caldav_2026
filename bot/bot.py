import asyncio
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.types import BotCommand
from pydantic import ValidationError

from bot.config import get_settings
from bot.dispatcher import create_dispatcher
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from bot.services.user_notifier import UserNotifier
from CalendarClient import CalendarClient
from db.db_config import create_database, migrate_database

logger = logging.getLogger(__name__)


async def run():
    settings = get_settings()
    await asyncio.to_thread(migrate_database, settings.database_url)
    engine, sessions = create_database(settings.database_url)
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    client = CalendarClient(timeout=settings.caldav_timeout_seconds)
    synchronizer = Synchronizer(sessions, client, vault, settings)
    bot = Bot(
        settings.bot_token.get_secret_value(),
        default=DefaultBotProperties(
            parse_mode=ParseMode.HTML,
            link_preview_is_disabled=True,
        ),
    )
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    notifier = UserNotifier(bot, sessions, settings)
    tasks = []
    try:
        await bot.delete_webhook(drop_pending_updates=False)
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Начать работу"),
                BotCommand(command="connect", description="Подключить Яндекс"),
                BotCommand(command="calendars", description="Выбрать календари"),
                BotCommand(command="events", description="Ближайшие встречи"),
                BotCommand(command="settings", description="Уведомления и часовой пояс"),
                BotCommand(command="sync", description="Синхронизировать сейчас"),
                BotCommand(command="disconnect", description="Отключить Яндекс"),
                BotCommand(command="cancel", description="Отменить ввод"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        tasks = [
            asyncio.create_task(synchronizer.run(), name="calendar-sync"),
            asyncio.create_task(notifier.run(), name="notifications"),
        ]
        await dispatcher.start_polling(
            bot,
            settings=settings,
            calendar_client=client,
            vault=vault,
            synchronizer=synchronizer,
            allowed_updates=dispatcher.resolve_used_update_types(),
            close_bot_session=False,
            tasks_concurrency_limit=50,
        )
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await dispatcher.storage.close()
        await bot.session.close()
        await engine.dispose()


def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    for name in ("caldav", "urllib3", "niquests", "httpx"):
        logging.getLogger(name).setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except ValidationError as exc:
        for error in exc.errors(include_input=False):
            logger.error("Configuration %s: %s", ".".join(map(str, error["loc"])), error["msg"])
        raise SystemExit(1) from None
    except KeyboardInterrupt:
        pass
    except Exception as exc:
        logger.error(
            "Startup/runtime failure (%s). Check configuration and connectivity.",
            type(exc).__name__,
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
