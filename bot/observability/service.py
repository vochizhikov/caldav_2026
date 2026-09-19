"""Persistent user lifecycle and safe errors, without conversation/HTTP/SQL logs."""

import asyncio
import hashlib
import json
import logging
import re
import sqlite3
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import LinkPreviewOptions

logger = logging.getLogger(__name__)
CURRENT_USER: ContextVar[int | None] = ContextVar("monitor_user", default=None)
MOSCOW = ZoneInfo("Europe/Moscow")
TOPICS = {"users": "Пользователи", "errors": "Ошибки"}
USER_FIELDS = (
    "first_name",
    "last_name",
    "username",
    "language_code",
    "is_premium",
    "is_bot",
    "added_to_attachment_menu",
    "allows_write_to_pm",
)
DB_FIELDS = (
    "id",
    "telegram_id",
    "chat_id",
    "created_at",
    "timezone",
    "yandex_login",
    "connected",
    "notifications_enabled",
)
TITLES = {
    "arrived": "🟢 Новый пользователь",
    "left": "🔴 Пользователь заблокировал бота",
    "returned": "🟢 Пользователь вернулся / разблокировал бота",
    "yandex_connected": "🔗 Подключён Яндекс-аккаунт",
    "yandex_disconnected": "🔌 Отключён Яндекс-аккаунт",
}


def _json(value):
    return json.dumps(value, ensure_ascii=False, default=lambda v: v.isoformat())


class MonitorService:
    def __init__(self, settings, *, clock=None):
        self.settings = settings
        self.directory = Path(settings.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._lock = threading.RLock()
        self._setup_lock = asyncio.Lock()
        self._last_send = 0.0
        self._last_warning = float("-inf")
        self._stop = asyncio.Event()
        self._task = None
        self._cleanups = []
        self._closed = False
        self._connection = sqlite3.connect(
            self.directory / "monitor.sqlite3", check_same_thread=False, timeout=3
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.executescript("""
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS config (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS contacts (
                telegram_id INTEGER PRIMARY KEY, profile TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending (
                id INTEGER PRIMARY KEY AUTOINCREMENT, payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS errors (
                fingerprint TEXT PRIMARY KEY, payload TEXT NOT NULL,
                count INTEGER NOT NULL, sent_count INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, updated_at REAL NOT NULL
            );
        """)

    def _get(self, key, default=None):
        row = self._connection.execute("SELECT value FROM config WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def _set(self, key, value):
        self._connection.execute(
            "INSERT INTO config(key,value) VALUES (?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, _json(value)),
        )

    @property
    def chat_id(self):
        with self._lock:
            return self._get("chat_id")

    @contextmanager
    def operation(self, user_id=None):
        token = CURRENT_USER.set(user_id)
        try:
            yield
        finally:
            CURRENT_USER.reset(token)

    def profile(self, telegram_id):
        with self._lock:
            row = self._connection.execute(
                "SELECT profile FROM contacts WHERE telegram_id=?", (telegram_id,)
            ).fetchone()
            return json.loads(row[0]) if row else None

    def _warn(self):
        if time.monotonic() - self._last_warning > 60:
            logger.warning("Admin monitor could not persist an event; check storage/queue limits")
            self._last_warning = time.monotonic()

    def _drop(self):
        self._set("dropped", self._get("dropped", 0) + 1)
        self._warn()

    def contact(self, user, db_user=None, *, kind, event_time):
        """Deduplicate transitions, preserving only explicitly allowed user fields."""
        if kind not in {*TITLES, "seen"}:
            return
        try:
            with self._lock, self._connection:
                old = self.profile(user.id)
                when = event_time.astimezone(UTC).isoformat()
                profile = dict(old or {})
                profile["telegram_id"] = user.id
                if when >= profile.get("profile_at", ""):
                    for field in USER_FIELDS:
                        value = getattr(user, field, None)
                        if value is not None:
                            profile[field] = value[:320] if isinstance(value, str) else value
                        else:
                            profile.pop(field, None)
                    profile["profile_at"] = when
                profile.setdefault("first_seen_at", when)
                profile["last_seen_at"] = max(profile.get("last_seen_at", when), when)
                current_db = db_user is not None and when >= profile.get("connection_at", "")
                if current_db:
                    profile["db_user"] = {
                        key: (value[:500] if isinstance(value, str) else value)
                        for key in DB_FIELDS
                        if (value := db_user.get(key)) is not None
                    }
                    profile["connection_at"] = when
                notification = None
                status_at = profile.get("status_at", "")
                if kind in {"left", "returned"} and when >= status_at:
                    status = "blocked" if kind == "left" else "active"
                    if profile.get("status") != status:
                        notification = kind
                    profile["status"], profile["status_at"] = status, when
                elif kind in {"arrived", "seen"}:
                    if when >= status_at:
                        if profile.get("status") == "blocked":
                            notification = "returned"
                        profile["status"], profile["status_at"] = "active", when
                    if kind == "arrived" and not profile.get("arrival_recorded"):
                        notification = "arrived"
                    # Existing DB users establish a silent baseline after deployment.
                    profile["arrival_recorded"] = True
                elif kind in {"yandex_connected", "yandex_disconnected"}:
                    connected = kind == "yandex_connected"
                    previous = old.get("db_user", {}).get("connected") if old else None
                    if current_db and previous != connected:
                        notification = kind
                self._connection.execute(
                    "INSERT INTO contacts VALUES (?,?) "
                    "ON CONFLICT(telegram_id) DO UPDATE SET profile=excluded.profile",
                    (user.id, _json(profile)),
                )
                if notification:
                    count = self._connection.execute("SELECT count(*) FROM pending").fetchone()[0]
                    if count >= self.settings.max_pending:
                        self._drop()
                    else:
                        self._connection.execute(
                            "INSERT INTO pending(payload) VALUES (?)",
                            (_json({"event": notification, "time": when, "profile": profile}),),
                        )
        except Exception:
            self._warn()

    def error(self, source, event, *, error_type=None, user_id=None, **fields):
        """No exception messages, raw arguments, request bodies or arbitrary fields."""
        try:

            def identifier(value):
                value = str(value or "")
                return value[:160] if re.fullmatch(r"[\w.:-]{1,160}", value) else "unknown"

            data = {
                "source": identifier(source),
                "event": identifier(event),
                "error_type": identifier(error_type) if error_type else None,
                "user_id": user_id if user_id is not None else CURRENT_USER.get(),
            }
            for name in ("code", "operation", "function", "filename", "level", "method"):
                if fields.get(name) is not None:
                    data[name] = identifier(fields[name])
            for name in (
                "line",
                "http_status",
                "notification_id",
                "calendar_id",
                "db_user_id",
                "retry_after_seconds",
            ):
                if type(fields.get(name)) is int:
                    data[name] = fields[name]
            frames = fields.get("frames")
            if isinstance(frames, list):
                data["frames"] = [
                    {
                        "filename": identifier(f.get("filename")),
                        "function": identifier(f.get("function")),
                        "line": f.get("line") if type(f.get("line")) is int else None,
                    }
                    for f in frames[-8:]
                    if isinstance(f, dict)
                ]
            fingerprint = hashlib.sha256(_json(data).encode()).hexdigest()
            now = self._clock()
            data["time"] = now.isoformat()
            with self._lock, self._connection:
                row = self._connection.execute(
                    "SELECT count FROM errors WHERE fingerprint=?", (fingerprint,)
                ).fetchone()
                if row:
                    self._connection.execute(
                        "UPDATE errors SET count=count+1,payload=?,updated_at=? "
                        "WHERE fingerprint=?",
                        (_json(data), now.timestamp(), fingerprint),
                    )
                else:
                    self._connection.execute(
                        "DELETE FROM errors WHERE count=sent_count AND updated_at<?",
                        (now.timestamp() - 86400,),
                    )
                    count = self._connection.execute("SELECT count(*) FROM errors").fetchone()[0]
                    if count >= self.settings.max_pending:
                        self._drop()
                    else:
                        self._connection.execute(
                            "INSERT INTO errors(fingerprint,payload,count,updated_at) "
                            "VALUES (?,?,1,?)",
                            (fingerprint, _json(data), now.timestamp()),
                        )
        except Exception:
            self._warn()

    async def setup(self, bot, chat_id):
        async with self._setup_lock:
            with self._lock, self._connection:
                if self.chat_id is not None and self.chat_id != chat_id:
                    raise ValueError("Monitor is bound to another group")
                self._set("chat_id", chat_id)
                topics = self._get("topics", {})
            for category, title in TOPICS.items():
                if category not in topics:
                    topic = await bot.create_forum_topic(chat_id, title, request_timeout=20)
                    topics[category] = topic.message_thread_id
                    with self._lock, self._connection:
                        self._set("topics", topics)
            return topics

    def status_text(self):
        with self._lock:
            pending = self._connection.execute("SELECT count(*) FROM pending").fetchone()[0]
            errors = self._connection.execute(
                "SELECT coalesce(sum(count-sent_count),0) FROM errors"
            ).fetchone()[0]
            return (
                f"Мониторинг: пользователи и ошибки\nГруппа: {self.chat_id or 'не подключена'}"
                f"\nТемы: {len(self._get('topics', {}))}/2"
                f"\nСобытий пользователей в очереди: {pending}"
                f"\nОшибок ожидают отправки/сводки: {errors}"
                f"\nПропущено из-за переполнения: {self._get('dropped', 0)}"
            )

    @staticmethod
    def format_contact(data):
        p = data["profile"]
        db = p.get("db_user", {})
        when = datetime.fromisoformat(data["time"]).astimezone(MOSCOW).strftime("%d.%m.%Y %H:%M:%S")
        lines = [TITLES[data["event"]], f"Время: {when} МСК", f"Telegram ID: {p['telegram_id']}"]
        for label, field in (
            ("Имя", "first_name"),
            ("Фамилия", "last_name"),
            ("Язык Telegram", "language_code"),
        ):
            lines.append(f"{label}: {p.get(field) or 'не указан'}")
        username = p.get("username")
        lines.append(f"Ник: @{username}" if username else "Ник: не указан")
        lines.append(
            f"Профиль: https://t.me/{username}"
            if username
            else f"Профиль: tg://user?id={p['telegram_id']}"
        )
        lines.append(f"Premium: {'да' if p.get('is_premium') else 'не указан'}")
        for label, field in (
            ("В меню вложений", "added_to_attachment_menu"),
            ("Разрешены личные сообщения", "allows_write_to_pm"),
        ):
            if field in p:
                lines.append(f"{label}: {'да' if p[field] else 'нет'}")
        if db:
            lines.extend(
                [
                    f"ID в БД: {db.get('id', '—')}",
                    f"Зарегистрирован: {db.get('created_at', '—')}",
                    f"Часовой пояс: {db.get('timezone', '—')}",
                    f"Яндекс: {'подключён' if db.get('connected') else 'не подключён'}",
                ]
            )
            if db.get("yandex_login"):
                lines.append(f"Логин Яндекса: {db['yandex_login']}")
            lines.append(
                f"Уведомления: {'включены' if db.get('notifications_enabled') else 'выключены'}"
            )
        lines.append(f"Слепок в личном чате: /db_user {p['telegram_id']}")
        return "\n".join(lines)

    @staticmethod
    def format_error(data, count):
        labels = {
            "source": "Источник",
            "event": "Событие",
            "error_type": "Тип ошибки",
            "user_id": "Telegram ID",
            "code": "Код",
            "operation": "Операция",
            "function": "Функция",
            "filename": "Файл",
            "line": "Строка",
            "http_status": "HTTP",
            "notification_id": "Уведомление ID",
            "calendar_id": "Календарь ID",
            "db_user_id": "Пользователь БД ID",
            "method": "Метод Telegram",
            "retry_after_seconds": "Повтор через, с",
        }
        lines = [
            "⚠️ Ошибка",
            f"Последнее событие: {data['time']}",
            f"Повторений в этой сводке: {count}",
        ]
        lines.extend(
            f"{label}: {data[key]}" for key, label in labels.items() if data.get(key) is not None
        )
        for frame in data.get("frames", []):
            lines.append(f"  {frame['filename']}:{frame['line']} · {frame['function']}")
        return "\n".join(lines)

    async def _send(self, bot, category, text):
        with self._lock:
            chat_id, topics = self.chat_id, self._get("topics", {})
        if not chat_id or category not in topics:
            return False
        delay = max(0, 3.2 - (time.monotonic() - self._last_send))
        if delay:
            await asyncio.sleep(delay)
        try:
            if len(text.encode("utf-16-le")) > 7000:
                text = text.encode("utf-16-le")[:6900].decode("utf-16-le", errors="ignore")
                text += "\n… сообщение сокращено"
            await bot.send_message(
                chat_id,
                text,
                message_thread_id=topics[category],
                parse_mode=None,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                request_timeout=20,
            )
        except TelegramRetryAfter as exc:
            self._last_send = time.monotonic() + exc.retry_after
            raise
        finally:
            self._last_send = max(self._last_send, time.monotonic())
        return True

    async def deliver(self, bot):
        for category in TOPICS:
            try:
                with self._lock:
                    if category == "users":
                        row = self._connection.execute(
                            "SELECT * FROM pending ORDER BY id LIMIT 1"
                        ).fetchone()
                    else:
                        row = self._connection.execute(
                            "SELECT * FROM errors WHERE count>sent_count AND next_at<=? "
                            "ORDER BY next_at,updated_at LIMIT 1",
                            (self._clock().timestamp(),),
                        ).fetchone()
                if row is None:
                    continue
                payload = json.loads(row["payload"])
                text = (
                    self.format_contact(payload)
                    if category == "users"
                    else self.format_error(payload, row["count"] - row["sent_count"])
                )
                if await self._send(bot, category, text):
                    with self._lock, self._connection:
                        if category == "users":
                            self._connection.execute("DELETE FROM pending WHERE id=?", (row["id"],))
                        else:
                            self._connection.execute(
                                "UPDATE errors SET sent_count=?,next_at=? WHERE fingerprint=?",
                                (
                                    row["count"],
                                    self._clock().timestamp() + self.settings.error_repeat_seconds,
                                    row["fingerprint"],
                                ),
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Admin monitor delivery failed (%s)", type(exc).__name__)

    async def run(self, bot):
        while not self._stop.is_set():
            await self.deliver(bot)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=3)
            except TimeoutError:
                pass

    def start(self, bot):
        self._task = asyncio.create_task(self.run(bot), name="admin-monitor")

    async def close(self):
        if self._closed:
            return
        self._stop.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except Exception:
                self._warn()
        with self._lock:
            self._connection.close()
            self._closed = True
