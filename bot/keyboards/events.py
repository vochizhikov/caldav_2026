from aiogram.types import InlineKeyboardMarkup

from bot.keyboards.menu import back_button


def events_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[back_button()]])
