from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from db.models import User


def upcoming_events_keyboard(user: User) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(
                text=f"{'✅' if user.upcoming_catalog_mode else '⬜'} Режим каталога",
                callback_data=(
                    f"settings:upcoming:catalog:{'off' if user.upcoming_catalog_mode else 'on'}"
                ),
            )
        ]
    ]
    if not user.upcoming_catalog_mode:
        buttons = [
            InlineKeyboardButton(
                text=f"{'✅ ' if user.upcoming_event_days == days else ''}"
                f"{days} {'день' if days == 1 else 'дня' if days < 5 else 'дней'}",
                callback_data=f"settings:upcoming:days:{days}",
            )
            for days in range(1, 8)
        ]
        rows.extend([buttons[:4], buttons[4:]])
    rows.append(
        [InlineKeyboardButton(text="← Настройки уведомлений", callback_data="menu:settings")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)
