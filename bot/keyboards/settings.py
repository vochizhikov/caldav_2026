from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.utils.settings_utils import NOTIFICATION_LABELS
from db.models import User


def settings_keyboard(user: User) -> InlineKeyboardMarkup:
    def button(text, key, enabled):
        return InlineKeyboardButton(
            text=f"{'✅' if enabled else '⬜'} {text}", callback_data=f"settings:toggle:{key}"
        )

    rows = [[button("Все уведомления", "notifications_enabled", user.notifications_enabled)]]
    for key, label in NOTIFICATION_LABELS.items():
        rows.append([button(label, key, getattr(user, key))])
    rows.append([button("В момент начала", "remind_at_start", user.remind_at_start)])
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if user.advance_minutes == minutes else ''}{minutes} мин.",
                callback_data=f"settings:advance:{minutes}",
            )
            for minutes in (1, 3, 5, 10)
        ]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text=f"{'✓ ' if user.advance_minutes is None else ''}Без напоминания заранее",
                callback_data="settings:advance:off",
            )
        ]
    )
    rows.append(
        [InlineKeyboardButton(text=f"🌍 {user.timezone}", callback_data="settings:timezone")]
    )
    rows.append(
        [
            InlineKeyboardButton(
                text="🗓 Настройка ближайших событий", callback_data="settings:upcoming"
            )
        ]
    )
    rows.append([InlineKeyboardButton(text="← Мои календари", callback_data="menu:calendars")])
    return InlineKeyboardMarkup(inline_keyboard=rows)
