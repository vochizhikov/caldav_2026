from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from bot.keyboards.events import events_keyboard
from bot.repositories.events import upcoming
from bot.utils.common import event_text
from db.models import utcnow


async def show_events(message, session, user):
    if not user.encrypted_password:
        await message.answer("🔗 Сначала подключите Яндекс: /connect")
        return
    rows = await upcoming(session, user.id, utcnow())
    if not rows:
        await message.answer(
            "🗓 В выбранных календарях пока нет ближайших встреч.\n"
            "🔄 При необходимости обновите данные: /sync",
            reply_markup=events_keyboard(),
        )
        return
    await message.answer("🗓 <b>Ближайшие встречи на 7 дней</b> (до 20 событий):")
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
    await message.answer(chunk, reply_markup=events_keyboard())


def build_router():
    router = Router(name="events")

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
