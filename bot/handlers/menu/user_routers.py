from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.menu import menu_keyboard


def build_router():
    router = Router(name="menu")

    @router.message(Command("menu"))
    async def menu(message: Message, state: FSMContext):
        await state.clear()
        await message.answer("🏠 <b>Главное меню</b>", reply_markup=menu_keyboard())

    @router.callback_query(F.data == "menu:home")
    async def menu_callback(query: CallbackQuery, state: FSMContext):
        await state.clear()
        await query.answer()
        await query.message.answer("🏠 <b>Главное меню</b>", reply_markup=menu_keyboard())

    return router
