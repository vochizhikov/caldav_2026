from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.keyboards.menu import back_button


def calendars_keyboard(calendars) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if c.enabled else '⬜'} {c.name[:50]}",
                callback_data=f"calendar:toggle:{c.id}",
            )
        ]
        for c in calendars
    ]
    rows.append([InlineKeyboardButton(text="🔄 Синхронизировать", callback_data="calendar:sync")])
    rows.append(
        [InlineKeyboardButton(text="⚙️ Настройки уведомлений", callback_data="menu:settings")]
    )
    rows.append(
        [InlineKeyboardButton(text="Отключить Яндекс аккаунт", callback_data="account:disconnect")]
    )
    rows.append([back_button()])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def disconnect_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Да, отключить и удалить данные бота",
                    callback_data="account:disconnect:confirm",
                )
            ],
            [back_button()],
        ]
    )
