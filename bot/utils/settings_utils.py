from db.models import User

NOTIFICATION_LABELS = {
    "notify_reminder": "Напоминания о встречах",
    "notify_created": "Создание событий",
    "notify_deleted": "Удаление событий",
    "notify_updated": "Изменение событий",
}


def reminder_offsets(user: User) -> list[int]:
    offsets = [0] if user.remind_at_start else []
    if user.advance_minutes is not None:
        offsets.append(user.advance_minutes)
    return offsets
