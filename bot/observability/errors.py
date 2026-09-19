"""Removable error-only hooks; never inspect messages, credentials or HTTP bodies."""

import logging
import re
from contextvars import ContextVar
from functools import wraps

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter

from CalendarClient.errors import CalendarConnectionError

_reporting = ContextVar("monitor_error_reporting", default=False)
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,119}\Z")
_LOGGER = re.compile(r"[A-Za-z_][A-Za-z0-9_.]{0,199}\Z")
_FILE = re.compile(r"[A-Za-z0-9_.-]{1,120}\.py\Z")
_CODES = {
    "authentication",
    "tls",
    "timeout",
    "proxy",
    "network",
    "caldav",
    "calendar_data",
    "internal",
    "unknown",
}
_TYPE_TEMPLATES = {
    "Telegram handler failed (%s)",
    "Notification cycle failed (%s)",
    "Synchronization cycle failed (%s)",
    "Startup/runtime failure (%s). Check configuration and connectivity.",
}
_SYNC_TEMPLATE = "Synchronization failed for user=%s (%s)"
_MISSING = object()


def _identifier(value, fallback="unknown"):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else fallback


def _filename(value):
    if not isinstance(value, str):
        return "unknown.py"
    name = value.replace("\\", "/").rsplit("/", 1)[-1]
    return name if _FILE.fullmatch(name) else "unknown.py"


def _positive_id(value):
    return value if type(value) is int and value > 0 else None


def _report(monitor, source, event, **fields):
    if _reporting.get():
        return
    token = _reporting.set(True)
    try:
        monitor.error(source, event, **fields)
    except Exception:
        # Error reporting is best effort; never recurse through another logger.
        pass
    finally:
        _reporting.reset(token)


class _ErrorHandler(logging.Handler):
    def __init__(self, monitor):
        super().__init__(logging.ERROR)
        self.monitor = monitor

    def emit(self, record):
        if _reporting.get() or record.levelno < logging.ERROR:
            return
        name = record.name
        if (
            not isinstance(name, str)
            or not _LOGGER.fullmatch(name)
            or name.split(".", 1)[0] not in {"bot", "db", "CalendarClient", "aiogram"}
            or name == "bot.observability"
            or name.startswith("bot.observability.")
        ):
            return
        try:
            fields = {
                "function": _identifier(record.funcName),
                "filename": _filename(record.pathname),
                "line": record.lineno if type(record.lineno) is int else 0,
                "level": "CRITICAL" if record.levelno >= logging.CRITICAL else "ERROR",
            }
            error_type = None
            if record.exc_info and isinstance(record.exc_info[0], type):
                error_type = _identifier(record.exc_info[0].__name__, "Exception")
                frames = []
                traceback = record.exc_info[2]
                while traceback is not None and len(frames) < 64:
                    code = traceback.tb_frame.f_code
                    frames.append(
                        {
                            "filename": _filename(code.co_filename),
                            "function": _identifier(code.co_name),
                            "line": traceback.tb_lineno,
                        }
                    )
                    traceback = traceback.tb_next
                fields["frames"] = frames[-8:]
            elif isinstance(record.msg, str) and isinstance(record.args, tuple):
                candidate = None
                if record.msg in _TYPE_TEMPLATES and len(record.args) == 1:
                    candidate = record.args[0]
                elif record.msg == _SYNC_TEMPLATE and len(record.args) == 2:
                    candidate = record.args[1]
                    database_user_id = _positive_id(record.args[0])
                    if database_user_id is not None:
                        fields["db_user_id"] = database_user_id
                if (
                    isinstance(candidate, str)
                    and _IDENTIFIER.fullmatch(candidate)
                    and (
                        candidate.endswith(("Error", "Exception"))
                        or candidate == "TelegramRetryAfter"
                    )
                ):
                    error_type = candidate
            _report(self.monitor, name, "runtime_error", error_type=error_type, **fields)
        except Exception:
            pass


class _TelegramErrors(BaseRequestMiddleware):
    def __init__(self, monitor):
        self.monitor = monitor

    async def __call__(self, make_request, bot, method):
        chat_id = getattr(method, "chat_id", None)
        if _reporting.get() or (chat_id is not None and chat_id == self.monitor.chat_id):
            # Logging from the HTTP client during a monitor upload is excluded too.
            token = _reporting.set(True)
            try:
                return await make_request(bot, method)
            finally:
                _reporting.reset(token)
        try:
            return await make_request(bot, method)
        except Exception as exc:
            fields = {
                "error_type": _identifier(type(exc).__name__, "Exception"),
                "user_id": _positive_id(chat_id),
                "method": _identifier(getattr(method, "__api_method__", None)),
            }
            if isinstance(exc, TelegramRetryAfter) and type(exc.retry_after) is int:
                fields["retry_after_seconds"] = max(0, exc.retry_after)
            # 403 also covers deactivated accounts and denied access. Only a
            # separate my_chat_member event can confirm that a user blocked us.
            event = "delivery_forbidden" if isinstance(exc, TelegramForbiddenError) else "api_error"
            _report(self.monitor, "telegram", event, **fields)
            raise


def install_error_monitor(client, bot, monitor):
    """Install scoped CalDAV/Telegram hooks and a filtered ERROR logging handler."""
    root = logging.getLogger()
    handler = _ErrorHandler(monitor)
    outgoing = _TelegramErrors(monitor)
    originals = {}
    wrappers = {}

    def observed(original, operation):
        @wraps(original)
        async def request(*args, **kwargs):
            try:
                return await original(*args, **kwargs)
            except Exception as exc:
                fields = {
                    "error_type": _identifier(type(exc).__name__, "Exception"),
                    "operation": operation,
                }
                if isinstance(exc, CalendarConnectionError):
                    fields["code"] = (
                        exc.code if isinstance(exc.code, str) and exc.code in _CODES else "unknown"
                    )
                    status = getattr(exc, "http_status", None)
                    if type(status) is int and 100 <= status <= 599:
                        fields["http_status"] = status
                _report(monitor, "caldav", "operation_failed", **fields)
                raise

        return request

    def cleanup():
        root.removeHandler(handler)
        handler.close()
        if outgoing in bot.session.middleware:
            bot.session.middleware.unregister(outgoing)
        for name, original in originals.items():
            if name in wrappers and getattr(client, name) is wrappers[name]:
                if original is _MISSING:
                    delattr(client, name)
                else:
                    setattr(client, name, original)

    try:
        for name, operation in (("calendars", "discover_calendars"), ("snapshot", "fetch_events")):
            originals[name] = vars(client).get(name, _MISSING)
            wrappers[name] = observed(getattr(client, name), operation)
            setattr(client, name, wrappers[name])
        bot.session.middleware.register(outgoing)
        root.addHandler(handler)
    except Exception:
        cleanup()
        raise
    return cleanup
