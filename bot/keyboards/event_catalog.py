from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.event_catalog import CatalogCursor


def catalog_keyboard(cursor: CatalogCursor) -> InlineKeyboardMarkup | None:
    buttons = []
    if cursor.page > 0:
        buttons.append(
            InlineKeyboardButton(text="←", callback_data=cursor.callback(cursor.page - 1))
        )
    if cursor.page + 1 < len(cursor.dates):
        buttons.append(
            InlineKeyboardButton(text="→", callback_data=cursor.callback(cursor.page + 1))
        )
    return InlineKeyboardMarkup(inline_keyboard=[buttons]) if buttons else None
