from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup


def menu_keyboard(*, connected: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text="📅 Мои календари", callback_data="menu:calendars")],
        [InlineKeyboardButton(text="🗓 Ближайшие события", callback_data="menu:events")],
    ]
    if not connected:
        rows.append(
            [
                InlineKeyboardButton(
                    text="🔗 Подключить Яндекс Аккаунт", callback_data="menu:connect"
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def back_button() -> InlineKeyboardButton:
    return InlineKeyboardButton(text="← Меню", callback_data="menu:home")
