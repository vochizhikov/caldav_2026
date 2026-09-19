"""Public bot identity and Telegram command menu."""

from pathlib import Path

from aiogram import Bot
from aiogram.types import BotCommand, MenuButtonCommands

BOT_NAME = "Календарь рядом"
BOT_SHORT_DESCRIPTION = (
    "Яндекс Календарь в Telegram: ближайшие встречи, напоминания и уведомления об изменениях."
)
BOT_DESCRIPTION = (
    "Ваш Яндекс Календарь — рядом, в Telegram.\n\n"
    "Показываю ближайшие встречи, напоминаю заранее и в момент начала. "
    "Сообщаю о новых событиях, изменениях и отменах в выбранных календарях.\n\n"
    "Для подключения нужен пароль приложения Яндекса типа «Календарь». "
    "События создавайте и редактируйте в Яндекс Календаре.\n\n"
    "Нажмите «Начать», чтобы подключить календарь."
)
AVATAR_PATH = Path(__file__).resolve().parent / "assets" / "avatar.jpg"
REQUEST_TIMEOUT_SECONDS = 20
PROFILE_LANGUAGES = ("", "ru")
COMMANDS = (
    ("start", "Начать работу"),
    ("menu", "Главное меню"),
    ("connect", "Подключить Яндекс аккаунт"),
    ("calendars", "Выбрать календари"),
    ("events", "Ближайшие встречи"),
    ("settings", "Уведомления и часовой пояс"),
    ("sync", "Синхронизировать сейчас"),
    ("disconnect", "Отключить Яндекс аккаунт"),
    ("cancel", "Отменить ввод"),
    ("help", "Помощь"),
)


async def configure_commands(bot: Bot) -> None:
    """Make the command menu available in private chats, including Russian clients."""
    commands = [BotCommand(command=command, description=text) for command, text in COMMANDS]
    for language_code in PROFILE_LANGUAGES:
        await bot.set_my_commands(
            commands,
            language_code=language_code,
            request_timeout=REQUEST_TIMEOUT_SECONDS,
        )
    await bot.set_chat_menu_button(
        menu_button=MenuButtonCommands(), request_timeout=REQUEST_TIMEOUT_SECONDS
    )
