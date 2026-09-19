from zoneinfo import ZoneInfo

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.event_catalog import catalog_keyboard
from bot.keyboards.menu import menu_keyboard
from bot.repositories.events import upcoming, upcoming_on_day
from bot.utils.common import event_text
from bot.utils.event_catalog import (
    CATALOG_DAYS,
    CATALOG_PREFIX,
    CatalogCursor,
    catalog_text,
    timezone_key,
)
from db.models import utcnow


async def send_event_list(message, session, user):
    if not user.encrypted_password:
        await message.answer("🔗 Сначала подключите Яндекс: /connect")
        return
    opened_at = utcnow()
    rows = await upcoming(
        session,
        user.id,
        opened_at,
        days=CATALOG_DAYS if user.upcoming_catalog_mode else user.upcoming_event_days,
        timezone=user.timezone,
    )
    if not rows:
        await message.answer(
            "🗓 В выбранных календарях пока нет ближайших событий.\n"
            "🔄 При необходимости обновите данные: /sync"
        )
        return
    if user.upcoming_catalog_mode:
        zone = ZoneInfo(user.timezone)
        dates = tuple(
            dict.fromkeys(row.Occurrence.starts_at.astimezone(zone).date() for row in rows)
        )
        cursor = CatalogCursor(user.id, opened_at, timezone_key(user.timezone), dates)
        day_rows = [
            row for row in rows if row.Occurrence.starts_at.astimezone(zone).date() == cursor.day
        ]
        await message.answer(
            catalog_text(day_rows, user, cursor), reply_markup=catalog_keyboard(cursor)
        )
        return
    await message.answer(
        "🗓 <b>Ближайшие события</b>\n"
        f"Дней со встречами для показа: <b>{user.upcoming_event_days}</b>."
    )
    blocks = []
    for occurrence, calendar in rows:
        text = event_text(
            occurrence.summary,
            occurrence.starts_at,
            occurrence.all_day,
            user.timezone,
            location=occurrence.location,
            meeting_url=occurrence.meeting_url,
            calendar_name=calendar.name,
        )
        if calendar.last_error:
            text += "\n⚠️ Последнее обновление не удалось; данные могут быть устаревшими."
        blocks.append(text)
    chunk = ""
    for block in blocks:
        if chunk and len(chunk) + len(block) > 3500:
            await message.answer(chunk)
            chunk = ""
        chunk += ("\n\n" if chunk else "") + block
    await message.answer(chunk)


async def show_events(message, session, user):
    await send_event_list(message, session, user)
    await message.answer(
        "🏠 <b>Главное меню</b>",
        reply_markup=menu_keyboard(connected=bool(user.encrypted_password)),
    )


def build_router():
    router = Router(name="events")

    @router.callback_query(F.data.startswith(CATALOG_PREFIX))
    async def catalog_page(query: CallbackQuery, session, user):
        try:
            cursor = CatalogCursor.decode(query.data)
        except ValueError:
            await query.answer("⚠️ Каталог недоступен. Откройте /events заново.", show_alert=True)
            return
        if cursor.user_id != user.id:
            await query.answer("⚠️ Каталог недоступен.", show_alert=True)
            return
        if not user.encrypted_password:
            await query.answer("🔗 Сначала подключите Яндекс: /connect", show_alert=True)
            return
        if cursor.timezone_hash != timezone_key(user.timezone):
            await query.answer("🌍 Часовой пояс изменён. Откройте /events заново.", show_alert=True)
            return
        rows = await upcoming_on_day(session, user.id, cursor.opened_at, cursor.day, user.timezone)
        await query.answer()
        try:
            await query.message.edit_text(
                catalog_text(rows, user, cursor), reply_markup=catalog_keyboard(cursor)
            )
        except TelegramBadRequest as exc:
            if "message is not modified" not in exc.message.lower():
                raise

    @router.message(Command("events"))
    async def events_command(message: Message, state: FSMContext, session, user):
        await state.clear()
        await show_events(message, session, user)

    @router.callback_query(F.data == "menu:events")
    async def events_callback(query: CallbackQuery, state: FSMContext, session, user):
        await state.clear()
        await query.answer()
        await show_events(query.message, session, user)

    return router
