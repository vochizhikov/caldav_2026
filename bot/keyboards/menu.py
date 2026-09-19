from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📅 Мои календари", callback_data="menu:calendars")],
            [InlineKeyboardButton(text="🗓 Ближайшие встречи", callback_data="menu:events")],
            [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="menu:settings")],
            [InlineKeyboardButton(text="🔗 Подключить Яндекс", callback_data="menu:connect")],
        ]
    )


def back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="← Меню", callback_data="menu:home")
