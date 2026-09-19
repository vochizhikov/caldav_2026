from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update

from bot.keyboards.settings import settings_keyboard
from bot.states.states import EditSettings
from bot.utils.settings_utils import NOTIFICATION_LABELS
from bot.validators.settings import validate_advance, validate_timezone
from db.models import MyCalendar, Notification


def settings_text(user):
    enabled = "включены" if user.notifications_enabled else "выключены"
    return (
        f"⚙️ <b>Уведомления {enabled}.</b>\n🌍 Часовой пояс: <i>{escape(user.timezone)}</i>\n\n"
        "🔔 Выберите типы уведомлений.\n"
        "⏱️ Напоминание в момент начала и один интервал заранее настраиваются независимо.\n"
        "🗓 Для событий на весь день начало — 00:00."
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
