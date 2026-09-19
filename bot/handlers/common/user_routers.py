from aiogram import Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import Message

from bot.keyboards.menu import menu_keyboard

HELP = (
    "👋 Я слежу за встречами в Яндекс Календаре и присылаю уведомления.\n\n"
    "🔗 /connect — подключите Яндекс с паролем приложения «Календарь».\n"
    "📅 /calendars — выберите календари.\n"
    "⚙️ /settings — выберите уведомления и время напоминаний.\n\n"
    "🗓 /events — встречи на 7 дней\n🔄 /sync — синхронизировать сейчас\n"
    "🏠 /menu — главное меню\n✖️ /cancel — отменить ввод\n"
    "🔌 /disconnect — отключить аккаунт и удалить данные календарей из бота\n\n"
    "✏️ Встречи создавайте и изменяйте в Яндекс Календаре.\n"
    "ℹ️ При первом подключении старые события загружаются без уведомлений о создании."
)


def build_router():
    router = Router(name="common")

    @router.message(CommandStart())
    @router.message(Command("help"))
    async def start(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(HELP, reply_markup=menu_keyboard())

    @router.message(Command("cancel"))
    async def cancel(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("✖️ Ввод отменён.", reply_markup=menu_keyboard())

    return router
