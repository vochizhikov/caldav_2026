from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramAPIError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from sqlalchemy import update

from bot.keyboards.menu import menu_keyboard
from bot.keyboards.my_calendars import calendars_keyboard, disconnect_keyboard
from bot.repositories.my_calendar import owned_calendar, reconcile, user_calendars
from bot.services.synchronizer import disconnect
from bot.states.states import ConnectAccount
from bot.validators.settings import validate_login
from CalendarClient.client import CalendarConnectionError
from db.models import Notification

CONNECT_TEXT = (
    "🔗 <b>Подключение Яндекса</b>\n\n"
    "👤 Отправьте логин Яндекса, лучше полный адрес почты вида name@yandex.ru. Затем я попрошу "
    "отдельный пароль приложения «Календарь».\n\n"
    "🔑 Создать пароль: https://id.yandex.ru/security/app-passwords\n"
    "🔒 Пароль будет передан через личный чат Telegram и зашифрован в базе бота. "
    "Используйте бота, владельцу которого доверяете.\n✖️ /cancel — отмена."
)


async def show_calendars(message: Message, session, user):
    if not user.encrypted_password:
        await message.answer("🔗 Сначала подключите Яндекс: /connect", reply_markup=menu_keyboard())
        return
    calendars = await user_calendars(session, user.id)
    text = "📅 Выберите календари для уведомлений.\n"
    page = []
    if not calendars:
        text += "ℹ️ Календарей пока нет. Создайте их в Яндексе и нажмите «Синхронизировать»."
    for calendar in calendars:
        status = "включён" if calendar.enabled else "выключен"
        if calendar.last_error:
            status += f" · {calendar.last_error}"
        elif calendar.last_synced_at:
            status += f" · 🔄 обновлён {calendar.last_synced_at:%d.%m %H:%M} UTC"
        else:
            status += " · ⏳ ожидает синхронизации"
        marker = "✅" if calendar.enabled else "⬜"
        block = f"\n{marker} <b>{escape(calendar.name[:100])}</b>: {escape(status)}"
        if page and (len(text) + len(block) > 3500 or len(page) >= 30):
            await message.answer(text, reply_markup=calendars_keyboard(page))
            text = "📅 Продолжение списка календарей:\n"
            page = []
        text += block
        page.append(calendar)
    await message.answer(text, reply_markup=calendars_keyboard(page))


def build_router():
    router = Router(name="my_calendars")

    @router.message(Command("connect"))
    async def connect_command(message: Message, state: FSMContext):
        await state.clear()
        await state.set_state(ConnectAccount.login)
        await message.answer(CONNECT_TEXT)

    @router.callback_query(F.data == "menu:connect")
    async def connect_callback(query: CallbackQuery, state: FSMContext):
        await query.answer()
        await connect_command(query.message, state)

    @router.message(Command("calendars"))
    async def calendars_command(message: Message, state: FSMContext, session, user):
        await state.clear()
        await show_calendars(message, session, user)

    @router.callback_query(F.data == "menu:calendars")
    async def calendars_callback(query: CallbackQuery, state: FSMContext, session, user):
        await state.clear()
        await query.answer()
        await show_calendars(query.message, session, user)

    @router.callback_query(F.data.startswith("calendar:toggle:"))
    async def toggle_calendar(query: CallbackQuery, session, user):
        raw_id = query.data.rsplit(":", 1)[1]
        calendar = (
            await owned_calendar(session, user.id, int(raw_id)) if raw_id.isdecimal() else None
        )
        if calendar is None:
            await query.answer("⚠️ Календарь недоступен", show_alert=True)
            return
        calendar.enabled = not calendar.enabled
        calendar.initialized = False
        calendar.notifications_since = None
        calendar.last_synced_at = None
        await session.execute(
            update(Notification)
            .where(Notification.calendar_id == calendar.id, Notification.status == "pending")
            .values(status="discarded")
        )
        await session.commit()
        await query.answer(
            "✅ Календарь включён. Первое обновление — без уведомлений."
            if calendar.enabled
            else "⬜ Календарь выключен"
        )
        await show_calendars(query.message, session, user)

    async def sync(message, state, session, user, synchronizer):
        await state.clear()
        if not user.encrypted_password:
            await message.answer("🔗 Сначала подключите Яндекс: /connect")
            return
        await session.commit()
        await message.answer("🔄 Синхронизирую календари…")
        await synchronizer.sync_user(user.id, already_locked=True)
        session.expire_all()
        await session.refresh(user)
        await show_calendars(message, session, user)

    @router.message(Command("sync"))
    async def sync_command(message: Message, state: FSMContext, session, user, synchronizer):
        await sync(message, state, session, user, synchronizer)

    @router.callback_query(F.data == "calendar:sync")
    async def sync_callback(query: CallbackQuery, state: FSMContext, session, user, synchronizer):
        await query.answer()
        await sync(query.message, state, session, user, synchronizer)

    @router.message(Command("disconnect"))
    async def disconnect_prompt(message: Message, state: FSMContext):
        await state.clear()
        await message.answer(
            "🔌 <b>Отключить аккаунт?</b>\n\n"
            "🗑 Удалить сохранённый пароль, копии календарей и очередь уведомлений из бота?\n"
            "☁️ Ваши события в Яндексе сохранятся.",
            reply_markup=disconnect_keyboard(),
        )

    @router.callback_query(F.data == "account:disconnect")
    async def disconnect_callback(query: CallbackQuery, state: FSMContext):
        await query.answer()
        await disconnect_prompt(query.message, state)

    @router.callback_query(F.data == "account:disconnect:confirm")
    async def disconnect_confirm(query: CallbackQuery, state: FSMContext, session, user):
        await disconnect(session, user)
        await session.commit()
        await state.clear()
        await query.answer("✅ Аккаунт отключён")
        await query.message.answer(
            "✅ Данные календарей и пароль удалены из бота.", reply_markup=menu_keyboard()
        )

    @router.message(ConnectAccount.login)
    async def receive_login(message: Message, state: FSMContext):
        try:
            login = validate_login(message.text or "")
        except ValueError as exc:
            await message.answer(str(exc))
            return
        await state.update_data(login=login)
        await state.set_state(ConnectAccount.password)
        await message.answer(
            f"👤 Логин: {escape(login)}\n🔑 Отправьте пароль приложения «Календарь».\n"
            "🔒 Я постараюсь сразу удалить сообщение с паролем.\n✖️ /cancel — отмена."
        )

    @router.message(ConnectAccount.password)
    async def receive_password(
        message: Message, state: FSMContext, session, user, calendar_client, vault
    ):
        password = (message.text or "").strip()
        try:
            await message.delete()
        except TelegramAPIError:
            await message.answer("⚠️ Не удалось удалить сообщение. Удалите его вручную.")
        if not password or len(password) > 200:
            await message.answer("🔑 Отправьте пароль приложения текстом или /cancel для отмены.")
            return
        data = await state.get_data()
        login = data.get("login")
        if not login:
            await state.clear()
            await message.answer("🔗 Начните подключение заново: /connect")
            return
        await session.commit()
        await message.answer("⏳ Проверяю подключение к Яндексу…")
        try:
            calendars = await calendar_client.calendars(login, password)
        except CalendarConnectionError as exc:
            if exc.code == "authentication":
                await message.answer(
                    f"{exc}\n\n👤 Чтобы изменить логин, отправьте /connect.\n"
                    "🔑 Для повторной проверки отправьте пароль приложения.\n✖️ /cancel — отмена."
                )
            else:
                await state.clear()
                await message.answer(f"{exc}\n\n🔗 Для повторной попытки используйте /connect.")
            return
        await disconnect(session, user)
        user.yandex_login = login
        user.encrypted_password = vault.encrypt(password)
        await reconcile(session, user.id, calendars)
        await session.commit()
        await state.clear()
        await message.answer(
            "✅ Яндекс подключён.\n📅 Календари выключены по умолчанию — включите нужные в списке."
        )
        await show_calendars(message, session, user)

    return router
