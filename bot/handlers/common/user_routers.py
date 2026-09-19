from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.menu import menu_keyboard
from bot.profile import BOT_NAME

WELCOME = (
    f"📅 <b>{BOT_NAME}</b>\n"
    "Ваши встречи из Яндекс Календаря — под рукой в Telegram.\n\n"
    "Покажу ближайшие встречи, напомню заранее и в момент начала, "
    "сообщу о новых событиях, изменениях и отменах.\n\n"
    "<b>Начнём с трёх шагов:</b>\n"
    "1. /connect — подключите Яндекс с паролем приложения «Календарь».\n"
    "2. /calendars — включите нужные календари.\n"
    "3. /settings — настройте напоминания и часовой пояс.\n\n"
    "Встречи создавайте и изменяйте в Яндекс Календаре. "
    "Основной пароль аккаунта не отправляйте.\n\n"
    "Все команды и подробности — /help."
)

HELP = (
    f"📖 <b>{BOT_NAME} · Помощь</b>\n\n"
    "🔗 /connect — подключите Яндекс с паролем приложения «Календарь».\n"
    "📅 /calendars — выберите календари.\n"
    "⚙️ /settings — уведомления, время напоминаний и число дней ближайших событий.\n\n"
    "🗓 /events — события за ближайшие дни со встречами (по умолчанию — 1 день)\n"
    "🔄 /sync — синхронизировать сейчас\n"
    "🏠 /menu — главное меню\n✖️ /cancel — отменить ввод\n"
    "🔌 /disconnect — отключить аккаунт и удалить данные календарей из бота\n\n"
    "✏️ Встречи создавайте и изменяйте в Яндекс Календаре.\n"
    "ℹ️ При первом подключении старые события загружаются без уведомлений о создании."
)


def build_router():
    router = Router(name="common")

    @router.message(CommandStart())
    async def start(message: Message, state: FSMContext, user):
        await state.clear()
        await message.answer(
            WELCOME, reply_markup=menu_keyboard(connected=bool(user.encrypted_password))
        )

    @router.message(Command("help"))
    async def help_message(message: Message, state: FSMContext, user):
        await state.clear()
        await message.answer(
            HELP, reply_markup=menu_keyboard(connected=bool(user.encrypted_password))
        )

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext, user):
        await state.clear()
        await message.answer(
            "✖️ Ввод отменён.",
            reply_markup=menu_keyboard(connected=bool(user.encrypted_password)),
        )

    return router
