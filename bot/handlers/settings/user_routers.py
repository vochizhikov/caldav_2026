from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update

from bot.keyboards.settings import settings_keyboard
from bot.keyboards.upcoming_events import upcoming_events_keyboard
from bot.states.states import EditSettings
from bot.utils.event_catalog import CATALOG_DAYS
from bot.utils.settings_utils import NOTIFICATION_LABELS
from bot.validators.settings import validate_advance, validate_timezone, validate_upcoming_days
from db.models import MyCalendar, Notification


def settings_text(user):
    enabled = "включены" if user.notifications_enabled else "выключены"
    return (
        f"⚙️ <b>Уведомления {enabled}.</b>\n🌍 Часовой пояс: <i>{escape(user.timezone)}</i>\n\n"
        "🔔 Выберите типы уведомлений.\n"
        "⏱️ Напоминание в момент начала и один интервал заранее настраиваются независимо.\n"
        "🗓 Для событий на весь день начало — 00:00."
    )


def upcoming_events_text(user):
    mode = "Каталог" if user.upcoming_catalog_mode else "Список"
    selection = (
        f"📅 Каталог: <b>{CATALOG_DAYS} дней со встречами</b>.\n"
        "Диапазон списка не используется. Чтобы изменить его, выключите режим каталога.\n"
        if user.upcoming_catalog_mode
        else (
            "Выберите, за сколько ближайших дней со встречами показывать события списком.\n"
            f"✅ Выбрано дней со встречами: <b>{user.upcoming_event_days}</b>\n"
        )
    )
    return (
        "🗓 <b>Настройка ближайших событий</b>\n\n"
        f"📖 Режим показа: <b>{mode}</b>\n\n"
        f"{selection}\n"
        "Настройка применяется к кнопке «Ближайшие события» и команде /events.\n"
        "Дни без встреч пропускаются.\n\n"
        "Список — события за все выбранные дни отправляются сразу.\n"
        "Каталог — события одного дня в одном сообщении. Стрелки под ним "
        f"переключают {CATALOG_DAYS} ближайших дней со встречами, обновляя это же сообщение.\n"
        "Кнопка «Режим каталога» включает и выключает каталог.\n\n"
        "По умолчанию — 1 день, режим «Список».\n"
        f"🌍 Даты считаются в часовом поясе <i>{escape(user.timezone)}</i>."
    )


async def discard_pending(session, user_id):
    await session.execute(
        update(Notification)
        .where(Notification.user_id == user_id, Notification.status == "pending")
        .values(status="discarded")
    )


def build_router():
    router = Router(name="settings")

    @router.message(Command("settings"))
    async def settings_command(message: Message, state: FSMContext, user):
        await state.clear()
        await message.answer(settings_text(user), reply_markup=settings_keyboard(user))

    @router.callback_query(F.data == "menu:settings")
    async def settings_callback(query: CallbackQuery, state: FSMContext, user):
        await state.clear()
        await query.answer()
        await query.message.answer(settings_text(user), reply_markup=settings_keyboard(user))

    @router.callback_query(F.data == "settings:upcoming")
    async def upcoming_settings(query: CallbackQuery, state: FSMContext, user):
        await state.clear()
        await query.answer()
        await query.message.answer(
            upcoming_events_text(user), reply_markup=upcoming_events_keyboard(user)
        )

    @router.callback_query(F.data.startswith("settings:upcoming:days:"))
    async def upcoming_days(query: CallbackQuery, state: FSMContext, session, user):
        if user.upcoming_catalog_mode:
            await state.clear()
            await query.answer(f"📖 В режиме каталога всегда {CATALOG_DAYS} дней со встречами.")
            keyboard = upcoming_events_keyboard(user)
            current_keyboard = query.message.reply_markup
            if current_keyboard is None or current_keyboard.model_dump() != keyboard.model_dump():
                await query.message.edit_text(upcoming_events_text(user), reply_markup=keyboard)
            return
        try:
            days = validate_upcoming_days(query.data.rsplit(":", 1)[1])
        except ValueError as exc:
            await query.answer(str(exc))
            return
        await state.clear()
        changed = user.upcoming_event_days != days
        if changed:
            user.upcoming_event_days = days
            await session.commit()
        await query.answer("✅ Сохранено" if changed else "ℹ️ Уже выбрано")
        keyboard = upcoming_events_keyboard(user)
        current_keyboard = query.message.reply_markup
        if current_keyboard is None or current_keyboard.model_dump() != keyboard.model_dump():
            await query.message.edit_text(upcoming_events_text(user), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("settings:upcoming:catalog:"))
    async def upcoming_catalog(query: CallbackQuery, state: FSMContext, session, user):
        value = query.data.removeprefix("settings:upcoming:catalog:")
        if value not in {"on", "off"}:
            await query.answer("⚠️ Неизвестный режим показа")
            return
        await state.clear()
        catalog_mode = value == "on"
        changed = user.upcoming_catalog_mode != catalog_mode
        if changed:
            user.upcoming_catalog_mode = catalog_mode
            await session.commit()
        await query.answer("✅ Сохранено" if changed else "ℹ️ Уже выбрано")
        keyboard = upcoming_events_keyboard(user)
        current_keyboard = query.message.reply_markup
        if current_keyboard is None or current_keyboard.model_dump() != keyboard.model_dump():
            await query.message.edit_text(upcoming_events_text(user), reply_markup=keyboard)

    @router.callback_query(F.data.startswith("settings:toggle:"))
    async def toggle(query: CallbackQuery, session, user):
        key = query.data.rsplit(":", 1)[1]
        if key not in {*NOTIFICATION_LABELS, "notifications_enabled", "remind_at_start"}:
            await query.answer("⚠️ Неизвестная настройка")
            return
        setattr(user, key, not getattr(user, key))
        # Disabled notifications must never replay on the next enable.
        if not getattr(user, key):
            statement = update(Notification).where(
                Notification.user_id == user.id, Notification.status == "pending"
            )
            if key.startswith("notify_"):
                statement = statement.where(Notification.kind == key.removeprefix("notify_"))
            elif key == "remind_at_start":
                statement = statement.where(
                    Notification.kind == "reminder", Notification.offset_minutes == 0
                )
            await session.execute(statement.values(status="discarded"))
        await session.commit()
        await query.answer("✅ Сохранено")
        await query.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user))

    @router.callback_query(F.data.startswith("settings:advance:"))
    async def advance(query: CallbackQuery, session, user):
        try:
            value = validate_advance(query.data.rsplit(":", 1)[1])
        except ValueError:
            await query.answer("⏱️ Выберите 1, 3, 5 или 10 минут")
            return
        if user.advance_minutes == value:
            await query.answer("ℹ️ Уже выбрано")
            return
        user.advance_minutes = value
        await session.execute(
            update(Notification)
            .where(
                Notification.user_id == user.id,
                Notification.status == "pending",
                Notification.kind == "reminder",
                Notification.offset_minutes != 0,
            )
            .values(status="discarded")
        )
        await session.commit()
        await query.answer("✅ Сохранено")
        await query.message.edit_text(settings_text(user), reply_markup=settings_keyboard(user))

    @router.callback_query(F.data == "settings:timezone")
    async def timezone_prompt(query: CallbackQuery, state: FSMContext):
        await state.clear()
        await state.set_state(EditSettings.timezone)
        await query.answer()
        await query.message.answer(
            "🌍 Отправьте часовой пояс IANA, например Europe/Moscow, "
            "Asia/Yekaterinburg или Asia/Tbilisi. /cancel — отмена."
        )

    @router.message(EditSettings.timezone)
    async def timezone_save(message: Message, state: FSMContext, session, user):
        try:
            timezone = validate_timezone(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        user.timezone = timezone
        await discard_pending(session, user.id)
        await session.execute(
            update(MyCalendar).where(MyCalendar.user_id == user.id).values(last_synced_at=None)
        )
        await session.commit()
        await state.clear()
        await message.answer(
            "✅ Часовой пояс сохранён.\n🔄 Время встреч обновится при синхронизации.",
            reply_markup=settings_keyboard(user),
        )

    return router
