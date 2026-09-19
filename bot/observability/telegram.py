"""Optional Telegram audit hooks, removable with one cleanup call.

Only selected message fields are recorded: sender/chat/message IDs, username, FSM
state, text, caption, callback data and attachment type. Raw Updates, file bytes,
contact details, locations, credentials in method dumps and exception messages
are never serialized. Unknown free text and command arguments are encrypted.
"""

from __future__ import annotations

import logging
from contextlib import nullcontext
from contextvars import ContextVar

from aiogram import BaseMiddleware
from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.enums import ChatType
from aiogram.types import Message

from bot.states.states import ConnectAccount, EditSettings
from bot.validators.settings import validate_login, validate_timezone

logger = logging.getLogger(__name__)

_CONTROL_COMMANDS = {"logs_setup", "logs_mode", "logs_status"}
_SAFE_COMMANDS = {
    "start", "help", "cancel", "menu", "connect", "disconnect", "calendars", "events",
    "settings", "sync",
}
_PLAIN_STATES = {
    ConnectAccount.login.state: validate_login,
    EditSettings.timezone.state: validate_timezone,
}
_OUTGOING_METHODS = {
    "sendMessage", "sendPhoto", "sendDocument", "sendAudio", "sendVoice", "sendVideo",
    "sendVideoNote", "sendAnimation", "sendSticker", "sendMediaGroup", "sendLocation",
    "sendVenue", "sendContact", "sendPoll", "sendDice", "copyMessage", "copyMessages",
    "forwardMessage", "forwardMessages", "editMessageText", "editMessageCaption",
    "editMessageMedia", "editMessageReplyMarkup", "deleteMessage", "deleteMessages",
    "answerCallbackQuery",
}
_conversation: ContextVar[dict | None] = ContextVar("telegram_audit_conversation", default=None)
_control: ContextVar[bool] = ContextVar("telegram_audit_control", default=False)


def _emit(audit, event, **fields):
    try:
        audit.emit("conversation", event, **fields)
    except Exception as exc:
        # Observability must not prevent delivery or expose values in error logs.
        logger.warning("Telegram audit write failed (%s)", type(exc).__name__)


def _encrypt(audit, text):
    try:
        audit.register_secret(text)
        return audit.encrypt(text)
    except Exception as exc:
        logger.warning("Telegram audit encryption failed (%s)", type(exc).__name__)
        return "[ENCRYPTION_UNAVAILABLE]"


def _split_command(text):
    if not text or not text.startswith("/"):
        return None, None, ""
    parts = text.split(maxsplit=1)
    command, _, mention = parts[0][1:].partition("@")
    return command, mention or None, parts[1] if len(parts) > 1 else ""


def _input_fields(audit, message, raw_state):
    fields = {
        "chat_id": message.chat.id,
        "message_id": message.message_id,
        "content_type": getattr(message.content_type, "value", message.content_type),
        "state": raw_state,
    }
    if message.text is not None:
        command, _, arguments = _split_command(message.text)
        if raw_state == ConnectAccount.password.state:
            fields["text_encrypted"] = _encrypt(audit, message.text)
        elif command in _SAFE_COMMANDS:
            fields["command"] = f"/{command}"
            if arguments:
                fields["arguments_encrypted"] = _encrypt(audit, arguments)
        elif command is None and raw_state in _PLAIN_STATES:
            try:
                _PLAIN_STATES[raw_state](message.text)
            except ValueError:
                fields["text_encrypted"] = _encrypt(audit, message.text)
            else:
                fields["text"] = audit.sanitize(message.text)
        else:
            fields["text_encrypted"] = _encrypt(audit, message.text)
    if message.caption is not None:
        fields["caption_encrypted"] = _encrypt(audit, message.caption)
    return fields


class TelegramAuditMiddleware(BaseMiddleware):
    def __init__(self, audit):
        self.audit = audit

    async def __call__(self, handler, event, data):
        # Appended to update middleware after aiogram's FSM middleware: raw_state
        # is read while FSM event isolation is held, before private-only sessions.
        message = event.message or event.edited_message
        query = event.callback_query
        if message and await self._admin_command(message, data["bot"]):
            return None
        subject = query or message
        if subject is None:
            return await handler(event, data)
        message = query.message if query else message
        if not isinstance(message, Message) or message.chat.type != ChatType.PRIVATE:
            return await handler(event, data)
        if message.chat.id == self.audit.chat_id:
            return await handler(event, data)
        user = subject.from_user
        if not user or user.is_bot:
            return await handler(event, data)
        context = {"chat_id": message.chat.id, "user_id": user.id}
        if query:
            context["callback_query_id"] = query.id
        token = _conversation.set(context)
        try:
            with self.audit.operation(user_id=user.id, operation_id=f"tg-{event.update_id}"):
                metadata = {"user_id": user.id, "username": user.username}
                if query:
                    _emit(
                        self.audit, "user_callback", **metadata,
                        chat_id=message.chat.id, message_id=message.message_id,
                        callback_data=self.audit.sanitize(query.data),
                    )
                else:
                    _emit(
                        self.audit,
                        "user_message_edited" if event.edited_message else "user_message",
                        **metadata, **_input_fields(self.audit, message, data.get("raw_state")),
                    )
                try:
                    return await handler(event, data)
                except Exception as exc:
                    _emit(self.audit, "handler_failed", error_type=type(exc).__name__)
                    raise
        finally:
            _conversation.reset(token)

    async def _admin_command(self, message, bot):
        command, mention, arguments = _split_command(message.text)
        if command not in _CONTROL_COMMANDS:
            return False
        if mention and mention.lower() != (await bot.me()).username.lower():
            return False
        token = _control.set(True)
        try:
            user = message.from_user
            if (
                not user or user.is_bot or message.sender_chat is not None
                or user.id not in self.audit.settings.admin_ids
            ):
                await message.answer("⛔ Команда доступна только администратору.", parse_mode=None)
                return True
            if command == "logs_setup":
                if (
                    message.chat.type != ChatType.SUPERGROUP
                    or not message.chat.is_forum
                    or arguments
                ):
                    await message.answer(
                        "Отправьте /logs_setup без аргументов в закрытой группе с темами. "
                        "Боту нужно право управления темами.", parse_mode=None,
                    )
                    return True
                try:
                    await self.audit.setup(bot, message.chat.id)
                except Exception as exc:
                    logger.warning("Telegram audit setup failed (%s)", type(exc).__name__)
                    await message.answer(
                        "Не удалось настроить темы. Проверьте право бота на управление "
                        "темами и повторите /logs_setup. "
                        f"Тип ошибки: {type(exc).__name__}.", parse_mode=None,
                    )
                else:
                    await message.answer(
                        "✅ Группа подключена. Темы: Переписка, CalDAV, База данных, Архивы.\n"
                        "/logs_mode normal — обычный режим\n"
                        "/logs_mode detailed 30 — подробный на 30 минут\n"
                        "/logs_status — состояние журналов", parse_mode=None,
                    )
                return True
            if (
                message.chat.type != ChatType.PRIVATE
                and message.chat.id != self.audit.chat_id
            ):
                await message.answer(
                    "Управление журналами доступно в личном чате с ботом "
                    "или в подключённой группе.", parse_mode=None,
                )
                return True
            if command == "logs_mode":
                parts = arguments.split()
                valid = bool(parts) and parts[0] in {"normal", "detailed"}
                minutes = self.audit.settings.detailed_minutes
                if valid and parts[0] == "normal":
                    valid = len(parts) == 1
                elif valid:
                    valid = len(parts) in {1, 2}
                    if valid and len(parts) == 2:
                        valid = parts[1].isascii() and parts[1].isdecimal()
                        minutes = int(parts[1]) if valid and len(parts[1]) <= 4 else 0
                        valid = valid and 1 <= minutes <= 1440
                if not valid:
                    await message.answer(
                        "Используйте /logs_mode normal или /logs_mode detailed [минуты]. "
                        "Срок подробного режима: от 1 до 1440 минут.", parse_mode=None,
                    )
                    return True
                self.audit.set_mode(parts[0], minutes=minutes)
            elif arguments:
                await message.answer("Используйте /logs_status без аргументов.", parse_mode=None)
                return True
            await message.answer(self.audit.status_text(), parse_mode=None)
            return True
        finally:
            _control.reset(token)


class TelegramReplyAuditMiddleware(BaseRequestMiddleware):
    def __init__(self, audit):
        self.audit = audit

    async def __call__(self, make_request, bot, method):
        name = method.__api_method__
        context = _conversation.get()
        chat_id = getattr(method, "chat_id", None)
        if name == "answerCallbackQuery" and context:
            if method.callback_query_id == context.get("callback_query_id"):
                chat_id = context["chat_id"]
        if (
            _control.get() or name not in _OUTGOING_METHODS
            or chat_id is None or chat_id == self.audit.chat_id
            or not isinstance(chat_id, int) or chat_id <= 0
        ):
            return await make_request(bot, method)
        fields = {"method": name, "chat_id": chat_id}
        for key in ("text", "caption", "message_id", "message_ids", "show_alert"):
            value = getattr(method, key, None)
            if value is not None:
                fields[key] = self.audit.sanitize(value)
        keyboard = getattr(method, "reply_markup", None)
        if getattr(keyboard, "inline_keyboard", None):
            fields["buttons"] = self.audit.sanitize([
                [{key: value for key in ("text", "callback_data", "url")
                  if (value := getattr(button, key, None)) is not None} for button in row]
                for row in keyboard.inline_keyboard
            ])
        media = getattr(method, "media", None)
        if isinstance(media, list):
            fields["media"] = [
                {
                    "type": getattr(item.type, "value", item.type),
                    "caption": self.audit.sanitize(item.caption),
                }
                for item in media
            ]
        elif getattr(media, "type", None) is not None:
            fields["media"] = {
                "type": getattr(media.type, "value", media.type),
                "caption": self.audit.sanitize(getattr(media, "caption", None)),
            }
        operation = nullcontext() if context else self.audit.operation(user_id=chat_id)
        with operation:
            try:
                result = await make_request(bot, method)
            except Exception as exc:
                _emit(self.audit, "bot_reply_failed", **fields, error_type=type(exc).__name__)
                raise
            result_id = getattr(result, "message_id", None)
            if result_id is not None:
                fields["result_message_id"] = result_id
            _emit(self.audit, "bot_reply", **fields)
            return result


def install_telegram_audit(dispatcher, bot, audit):
    """Install two removable middleware hooks; return idempotent sync cleanup."""
    incoming = TelegramAuditMiddleware(audit)
    outgoing = TelegramReplyAuditMiddleware(audit)
    dispatcher.update.outer_middleware.register(incoming)
    bot.session.middleware.register(outgoing)

    def cleanup():
        if incoming in dispatcher.update.outer_middleware:
            dispatcher.update.outer_middleware.unregister(incoming)
        if outgoing in bot.session.middleware:
            bot.session.middleware.unregister(outgoing)

    return cleanup
