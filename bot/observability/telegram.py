"""Removable lifecycle and private administrator snapshot hooks.

Message text, captions, callback data and outgoing replies are never serialized.
Only allowlisted Telegram metadata and selected database columns enter monitoring.
"""

from __future__ import annotations

from datetime import UTC, datetime

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.enums import ChatType
from aiogram.types import FSInputFile, Message
from aiogram.types import User as TelegramUser
from sqlalchemy import select

from bot.observability.snapshots import SnapshotTooLarge
from db.models import User

_COMMANDS = {
    "logs_setup", "monitor_setup", "logs_status", "monitor_status",
    "db_user", "db_stats", "db_users",
}
_TELEGRAM_FIELDS = (
    "id", "username", "first_name", "last_name", "language_code", "is_premium", "is_bot",
    "added_to_attachment_menu", "allows_write_to_pm",
)
_UNAVAILABLE = object()


def _report(monitor, source, event, error, **fields):
    try:
        monitor.error(source, event, error_type=type(error).__name__, **fields)
    except Exception:
        pass  # A monitor failure must not change application behaviour.


def _telegram_profile(user):
    return TelegramUser.model_validate({
        key: value for key in _TELEGRAM_FIELDS
        if (value := getattr(user, key, None)) is not None
    })


def _contact(monitor, user, db_user, *, kind, event_time):
    try:
        monitor.contact(_telegram_profile(user), db_user, kind=kind, event_time=event_time)
    except Exception as exc:
        _report(monitor, "monitor", "contact_failed", exc, user_id=user.id)


async def _load_user(sessions, *, telegram_id=None, user_id=None):
    selector = User.id == user_id if user_id is not None else User.telegram_id == telegram_id
    statement = select(
        User.id, User.telegram_id, User.created_at, User.timezone, User.yandex_login,
        User.encrypted_password.is_not(None).label("connected"), User.notifications_enabled,
    ).where(selector)
    async with sessions() as session:
        row = (await session.execute(statement)).mappings().one_or_none()
        return dict(row) if row is not None else None


def _command(text):
    if not text or not text.startswith("/"):
        return None, None, ""
    parts = text.split(maxsplit=1)
    name, _, mention = parts[0][1:].partition("@")
    return name, mention or None, parts[1] if len(parts) > 1 else ""


class TelegramMonitorMiddleware(BaseMiddleware):
    def __init__(self, monitor, sessions, snapshots):
        self.monitor = monitor
        self.sessions = sessions
        self.snapshots = snapshots

    async def _profile(self, telegram_id):
        try:
            return await _load_user(self.sessions, telegram_id=telegram_id)
        except Exception as exc:
            _report(self.monitor, "database", "profile_lookup_failed", exc, user_id=telegram_id)
            return _UNAVAILABLE

    async def __call__(self, handler, event, data):
        if event.my_chat_member:
            if await self._membership(event.my_chat_member, data["bot"]):
                return None
            return await handler(event, data)
        message = event.message
        query = event.callback_query
        if message and await self._admin_command(message, data["bot"]):
            return None
        subject = query or message
        if subject is None:
            return await handler(event, data)
        message = query.message if query else message
        if (
            not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE
            or message.chat.id == self.monitor.chat_id
        ):
            return await handler(event, data)
        user = subject.from_user
        if not user or user.is_bot or user.id != message.chat.id:
            return await handler(event, data)
        event_time = datetime.now(UTC) if query else message.date
        with self.monitor.operation(user_id=user.id):
            return await self._observe_contact(handler, event, data, user, event_time)

    async def _observe_contact(self, handler, event, data, user, event_time):
        before = await self._profile(user.id)
        if before is not None and before is not _UNAVAILABLE:
            _contact(self.monitor, user, before, kind="seen", event_time=event_time)
        try:
            return await handler(event, data)
        except Exception as exc:
            _report(self.monitor, "telegram", "handler_failed", exc, user_id=user.id)
            raise
        finally:
            # SessionMiddleware has committed or rolled back before this read.
            after = await self._profile(user.id)
            if after is not None and after is not _UNAVAILABLE:
                if before is None:
                    _contact(self.monitor, user, after, kind="arrived", event_time=event_time)
                elif before is _UNAVAILABLE:
                    # Unknown pre-state must never turn an old user into a new one.
                    _contact(self.monitor, user, after, kind="seen", event_time=event_time)
                if before is not _UNAVAILABLE and bool(after["connected"]) != bool(
                    before and before["connected"]
                ):
                    kind = "yandex_connected" if after["connected"] else "yandex_disconnected"
                    _contact(self.monitor, user, after, kind=kind, event_time=event_time)
                elif before is not None and before is not _UNAVAILABLE:
                    _contact(self.monitor, user, after, kind="seen", event_time=event_time)

    async def _membership(self, event, bot):
        if event.chat.type != ChatType.PRIVATE:
            return False
        if (
            event.from_user.id != event.chat.id or event.from_user.is_bot
            or event.new_chat_member.user.id != bot.id
        ):
            return True
        old = event.old_chat_member.status
        new = event.new_chat_member.status
        if old == new:
            return True
        if new == "kicked" and old == "member":
            kind = "left"
        elif old == "kicked" and new == "member":
            kind = "returned"
        else:
            return True
        with self.monitor.operation(user_id=event.chat.id):
            profile = await self._profile(event.chat.id)
            _contact(
                self.monitor, event.from_user, None if profile is _UNAVAILABLE else profile,
                kind=kind, event_time=event.date,
            )
        return True

    async def _admin_command(self, message, bot):
        command, mention, arguments = _command(message.text)
        if command not in _COMMANDS:
            return False
        if mention:
            me = await bot.me()
            if mention.lower() != (me.username or "").lower():
                return False
        user = message.from_user
        if (
            not user or user.is_bot or message.sender_chat is not None
            or user.id not in self.monitor.settings.admin_ids
        ):
            await message.answer("⛔ Команда доступна только администратору.", parse_mode=None)
            return True
        if command in {"db_user", "db_stats", "db_users"}:
            if message.chat.type != ChatType.PRIVATE or message.chat.id != user.id:
                await message.answer(
                    "Команды базы данных доступны только в личном чате с ботом.", parse_mode=None,
                )
                return True
        if command in {"logs_setup", "monitor_setup"}:
            if (
                message.chat.type != ChatType.SUPERGROUP or not message.chat.is_forum
                or arguments
            ):
                await message.answer(
                    "Отправьте /monitor_setup без аргументов в закрытой группе с темами. "
                    "Боту нужно право управления темами.", parse_mode=None,
                )
                return True
            try:
                await self.monitor.setup(bot, message.chat.id)
            except Exception as exc:
                _report(self.monitor, "monitor", "setup_failed", exc, user_id=user.id)
                await message.answer(
                    "Не удалось настроить темы. Проверьте право бота на управление темами "
                    "и повторите /monitor_setup.", parse_mode=None,
                )
            else:
                await message.answer(
                    "✅ Группа подключена. Темы: Пользователи, Ошибки.\n"
                    "/monitor_status — состояние мониторинга.\n"
                    "В личном чате: /db_user <telegram_id>, /db_user id:<id_в_БД>, "
                    "/db_stats, /db_users.", parse_mode=None,
                )
            return True
        if (
            message.chat.type != ChatType.PRIVATE
            and message.chat.id != self.monitor.chat_id
        ):
            await message.answer(
                "Управление мониторингом доступно в личном чате с ботом "
                "или в подключённой группе.", parse_mode=None,
            )
            return True
        if command == "db_user":
            await self._user_snapshot(message, arguments)
        elif arguments:
            await message.answer("Эта команда не принимает аргументы.", parse_mode=None)
        elif command == "db_users":
            await self._snapshot(message)
        elif command == "db_stats":
            try:
                counts = await self.snapshots.stats()
                labels = {
                    "users": "Пользователи", "calendars": "Календари", "events": "События",
                    "occurrences": "Экземпляры событий", "notifications": "Уведомления",
                }
                text = "Статистика базы данных:\n" + "\n".join(
                    f"{label}: {int(counts[key])}" for key, label in labels.items()
                )
                await message.answer(text, parse_mode=None)
            except Exception as exc:
                _report(self.monitor, "database", "stats_failed", exc, user_id=user.id)
                await message.answer("Не удалось получить статистику базы.", parse_mode=None)
        else:
            await message.answer(self.monitor.status_text(), parse_mode=None)
        return True

    async def _user_snapshot(self, message, arguments):
        internal = arguments.startswith("id:")
        raw = arguments[3:] if internal else arguments
        if (
            not raw or not raw.isascii() or not raw.isdecimal() or len(raw) > 19
            or not 0 < int(raw) <= 2**63 - 1
        ):
            await message.answer(
                "Используйте /db_user <telegram_id> или /db_user id:<id_в_БД>.", parse_mode=None,
            )
            return
        await self._snapshot(message, {"user_id" if internal else "telegram_id": int(raw)})

    async def _snapshot(self, message, selector=None):
        path = None
        try:
            if selector is None:
                path = await self.snapshots.users_snapshot()
                caption = "Таблица users без сохранённых паролей"
            else:
                profile = await _load_user(self.sessions, **selector)
                if profile is None:
                    await message.answer("Пользователь не найден.", parse_mode=None)
                    return
                path = await self.snapshots.user_snapshot(
                    **selector, metadata=self.monitor.profile(profile["telegram_id"]),
                )
                caption = "Снимок данных пользователя из базы."
            if path is None:
                await message.answer("Пользователь не найден.", parse_mode=None)
                return
            await message.answer_document(FSInputFile(path), caption=caption, parse_mode=None)
        except Exception as exc:
            _report(
                self.monitor, "database", "snapshot_failed", exc, user_id=message.from_user.id,
            )
            if isinstance(exc, SnapshotTooLarge):
                text = "Снимок превышает настроенный предел размера."
            else:
                text = "Не удалось подготовить или отправить снимок базы."
            await message.answer(text, parse_mode=None)
        finally:
            if path is not None:
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    _report(self.monitor, "database", "snapshot_cleanup_failed", exc)


def install_telegram_audit(dispatcher, bot, monitor, sessions, snapshots):
    """Install optional hooks and polling subscription; return idempotent cleanup."""
    incoming = TelegramMonitorMiddleware(monitor, sessions, snapshots)
    dispatcher.update.outer_middleware.register(incoming)

    async def membership_subscription(event):
        return UNHANDLED

    dispatcher.my_chat_member.register(membership_subscription)

    def cleanup():
        if incoming in dispatcher.update.outer_middleware:
            dispatcher.update.outer_middleware.unregister(incoming)
        dispatcher.my_chat_member.handlers[:] = [
            handler for handler in dispatcher.my_chat_member.handlers
            if handler.callback is not membership_subscription
        ]

    return cleanup
