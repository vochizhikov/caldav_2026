"""Local durable journal, bounded Telegram delivery and daily archives.

The append-only files are the source of truth. Telegram delivery is at-least-once:
an ambiguous network failure may repeat a message, but never advances its cursor.
No data is stored in the bot's application database.
"""

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import re
import threading
import time
import uuid
import zipfile
from collections import Counter
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import BufferedInputFile, FSInputFile
from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)
MOSCOW = ZoneInfo("Europe/Moscow")
CATEGORIES = ("conversation", "caldav", "database")
TOPICS = {
    "conversation": "Переписка",
    "caldav": "CalDAV",
    "database": "База данных",
    "archives": "Архивы",
}
CONTEXT: ContextVar[dict | None] = ContextVar("audit_context", default=None)
SECRET_KEYS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "bot_token",
    "token",
    "access_token",
    "refresh_token",
    "encryption_key",
    "api_key",
    "secret",
}
URL_CREDENTIALS = re.compile(r"(https?://)[^/@\s]+:[^/@\s]+@", re.I)
TOKEN_URL = re.compile(r"(api\.telegram\.org/(?:file/)?bot)[^/\s]+", re.I)
QUERY_SECRET = re.compile(
    r"([?&](?:password|passwd|token|access_token|key|secret|auth)=)[^&#\s\"<>]+", re.I
)


def encryption_key(settings, application_key: str) -> bytes:
    if settings.encryption_key is not None:
        return settings.encryption_key.get_secret_value().encode()
    # Domain separation: journal ciphertext cannot be used as a database credential.
    return base64.urlsafe_b64encode(
        hmac.digest(base64.urlsafe_b64decode(application_key), b"caldavbot-audit-v1", "sha256")
    )


class AuditService:
    def __init__(self, settings, application_key: str, *, secrets=(), clock=None):
        self.settings = settings
        self.directory = Path(settings.directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self._fernet = Fernet(encryption_key(settings, application_key))
        self._lock = threading.RLock()
        self._setup_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._secrets: dict[str, None] = {}
        self._permanent_secrets: set[str] = set()
        self._secret_bytes = 0
        self._secret_cache: tuple[str, ...] = ()
        self._active_files: dict[tuple[str, str], Path] = {}
        self._last_send = 0.0
        self._last_warning = float("-inf")
        self._stopped = asyncio.Event()
        self._task = None
        self._cleanups = []
        self._state = self._load_state()
        self._disk_bytes = sum(p.stat().st_size for p in self.directory.glob("*/*.jsonl"))
        self.dropped_records = 0
        for secret in secrets:
            self.register_secret(secret)
        self.register_secret(application_key)
        if settings.encryption_key:
            self.register_secret(settings.encryption_key.get_secret_value())
        self._permanent_secrets.update(self._secrets)

    def _load_state(self):
        path = self.directory / "state.json"
        if not path.exists():
            return {"topics": {}, "cursors": {}, "archives": {}, "mode": "normal"}
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(state, dict):
                raise ValueError("Invalid audit state")
            for key in ("topics", "cursors", "archives"):
                if not isinstance(state.get(key), dict):
                    raise ValueError("Invalid audit state")
            chat_id = state.get("chat_id")
            if chat_id is not None and (type(chat_id) is not int or chat_id >= 0):
                raise ValueError("Invalid audit chat ID")
            if any(
                category not in TOPICS or type(topic) is not int or topic < 1
                for category, topic in state["topics"].items()
            ):
                raise ValueError("Invalid audit topic ID")
            if state["topics"] and chat_id is None:
                raise ValueError("Topics require a chat ID")
            if state.get("mode", "normal") not in {"normal", "detailed"}:
                raise ValueError("Invalid audit mode")
            timestamps = []
            if state.get("mode") == "detailed":
                timestamps.append(state.get("detailed_until"))
            for relative, offset in state["cursors"].items():
                self._validate_relative(relative)
                if type(offset) is not int or offset < 0:
                    raise ValueError("Invalid audit cursor")
                path = self.directory / relative
                if path.exists() and offset > path.stat().st_size:
                    raise ValueError("Audit cursor exceeds file size")
            for relative, ack in state["archives"].items():
                self._validate_relative(relative)
                if not isinstance(ack, dict):
                    raise ValueError("Invalid archive acknowledgement")
                timestamps.append(ack.get("sent_at"))
            for timestamp in timestamps:
                if (
                    not isinstance(timestamp, str)
                    or datetime.fromisoformat(timestamp).tzinfo is None
                ):
                    raise ValueError("Invalid audit timestamp")
            return state
        except (ValueError, OSError, TypeError) as exc:
            # Do not silently discard the destination or delivery acknowledgements.
            raise RuntimeError("Cannot read audit state.json") from exc

    @staticmethod
    def _validate_relative(relative):
        if not isinstance(relative, str) or not re.fullmatch(
            r"\d{4}-\d{2}-\d{2}/(?:conversation|caldav|database)-\d{6}\.jsonl", relative
        ):
            raise ValueError("Invalid audit journal path")

    def _save_state(self):
        with self._lock:
            temporary = self.directory / "state.json.tmp"
            temporary.write_text(json.dumps(self._state, ensure_ascii=False), encoding="utf-8")
            temporary.replace(self.directory / "state.json")

    @property
    def chat_id(self):
        return self._state.get("chat_id")

    @property
    def mode(self):
        if self._state.get("mode") == "detailed":
            until = self._state.get("detailed_until")
            if until and self._clock() < datetime.fromisoformat(until):
                return "detailed"
        return "normal"

    def set_mode(self, mode, minutes=30):
        if mode not in {"normal", "detailed"} or not 1 <= minutes <= 1440:
            raise ValueError("Invalid logging mode or duration")
        with self._lock:
            self._state["mode"] = mode
            self._state["detailed_until"] = (
                (self._clock() + timedelta(minutes=minutes)).isoformat()
                if mode == "detailed"
                else None
            )
            self._save_state()

    @contextmanager
    def operation(self, user_id=None, operation_id=None):
        context = dict(CONTEXT.get() or {})
        context["operation_id"] = (
            operation_id or context.get("operation_id") or uuid.uuid4().hex[:12]
        )
        if user_id is not None:
            context["user_id"] = user_id
        token = CONTEXT.set(context)
        try:
            yield context["operation_id"]
        finally:
            CONTEXT.reset(token)

    def register_secret(self, secret):
        if secret:
            with self._lock:
                secret = str(secret)
                if secret in self._secrets:
                    self._secrets.pop(secret)
                else:
                    self._secret_bytes += len(secret)
                self._secrets[secret] = None
                while len(self._secrets) > 1024 or self._secret_bytes > 2 * 1024 * 1024:
                    oldest = next(iter(self._secrets))
                    self._secrets.pop(oldest)
                    self._secret_bytes -= len(oldest)
                self._secret_cache = tuple(
                    sorted(self._permanent_secrets | self._secrets.keys(), key=len, reverse=True)
                )

    def encrypt(self, value):
        return "fernet:" + self._fernet.encrypt(str(value).encode()).decode()

    def sanitize(self, value):
        """Sanitize before any file, transaction buffer or Telegram queue sees a value."""
        with self._lock:
            return self._sanitize(value)

    def _sanitize(self, value, depth=0):
        if depth > 12:
            return "[nested value omitted]"
        if isinstance(value, dict):
            result = {}
            for key, item in list(value.items())[:256]:
                original_name = str(key)
                lowered = original_name.lower().replace("-", "_")
                name = self._sanitize(original_name, depth + 1)
                if not isinstance(name, str):
                    name = "[oversized field name]"
                if lowered in {k.replace("-", "_") for k in SECRET_KEYS}:
                    result[name] = "[REDACTED]"
                elif lowered in {"password", "passwd"}:
                    result[name] = (
                        (item if self._is_ciphertext(item) else self.encrypt(item))
                        if item
                        else item
                    )
                else:
                    result[name] = self._sanitize(item, depth + 1)
            if len(value) > 256:
                result["_omitted_fields"] = len(value) - 256
            return result
        if isinstance(value, (list, tuple, set)):
            values = list(value)
            result = [self._sanitize(item, depth + 1) for item in values[:256]]
            if len(values) > 256:
                result.append({"omitted_items": len(values) - 256})
            return result
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        elif not isinstance(value, str):
            value = value.isoformat() if isinstance(value, datetime) else str(value)
        if self._is_ciphertext(value):
            return value
        for secret in self._secret_cache:
            value = value.replace(secret, "[REDACTED]")
        value = URL_CREDENTIALS.sub(r"\1[REDACTED]@", value)
        value = TOKEN_URL.sub(r"\1[REDACTED]", value)
        value = QUERY_SECRET.sub(r"\1[REDACTED]", value)
        if len(value) > self.settings.max_text_chars:
            return {
                "text": value[: self.settings.max_text_chars],
                "truncated": True,
                "original_chars": len(value),
            }
        return value

    def _is_ciphertext(self, value):
        if not isinstance(value, str) or not value.startswith("fernet:"):
            return False
        try:
            token = value.removeprefix("fernet:").encode()
            # Fernet's permissive decoder ignores bytes after Base64 padding.
            # Authenticate only a canonical encoding of the ENTIRE input value.
            decoded = base64.b64decode(token, altchars=b"-_", validate=True)
            if base64.urlsafe_b64encode(decoded) != token:
                return False
            self._fernet.extract_timestamp(token)
            return True
        except (InvalidToken, ValueError, TypeError):
            return False

    def _warn(self, message):
        if time.monotonic() - self._last_warning > 60:
            logger.error(message)
            self._last_warning = time.monotonic()

    def emit(self, category, event, **fields):
        try:
            if category not in CATEGORIES:
                return
            with self._lock:
                now = self._clock().astimezone(MOSCOW)
                record = {
                    "time": now.isoformat(),
                    "id": uuid.uuid4().hex[:12],
                    "category": category,
                    "event": event,
                    **(CONTEXT.get() or {}),
                    "data": self._sanitize(fields),
                }
                raw = (json.dumps(record, ensure_ascii=False) + "\n").encode()
                if len(raw) > self.settings.segment_bytes:
                    record["data"] = {
                        "truncated": True,
                        "original_record_bytes": len(raw),
                        "preview": json.dumps(record["data"], ensure_ascii=False)[
                            : self.settings.segment_bytes // 8
                        ],
                    }
                    raw = (json.dumps(record, ensure_ascii=False) + "\n").encode()
                if self._disk_bytes + len(raw) > self.settings.max_disk_mb * 1024 * 1024:
                    self.dropped_records += 1
                    self._warn("Audit disk budget reached; new records are being dropped")
                    return
                day = now.date().isoformat()
                key = day, category
                path = self._active_files.get(key)
                if path is None or (
                    path.exists() and path.stat().st_size >= self.settings.segment_bytes
                ):
                    folder = self.directory / day
                    folder.mkdir(exist_ok=True)
                    existing = sorted(folder.glob(f"{category}-*.jsonl"))
                    if existing and existing[-1].stat().st_size < self.settings.segment_bytes:
                        path = existing[-1]
                    else:
                        index = int(existing[-1].stem.rsplit("-", 1)[1]) + 1 if existing else 1
                        path = folder / f"{category}-{index:06d}.jsonl"
                    self._active_files[key] = path
                with path.open("a+b") as stream:
                    if stream.tell():
                        stream.seek(-1, 2)
                        needs_separator = stream.read(1) != b"\n"
                        stream.seek(0, 2)
                        if needs_separator:
                            stream.write(b"\n")
                            self._disk_bytes += 1
                    stream.write(raw)
                self._disk_bytes += len(raw)
        except Exception:
            # Observability must not prevent delivery or a database commit.
            self.dropped_records += 1
            self._warn("Could not persist audit record; check audit storage")

    async def setup(self, bot, chat_id):
        async with self._setup_lock:
            if self.chat_id is not None and self.chat_id != chat_id:
                raise ValueError("Audit is already bound to another group")
            with self._lock:
                self._state["chat_id"] = chat_id
                self._save_state()
            # Persist each created topic: a partial failure can be retried safely.
            for category, name in TOPICS.items():
                if category not in self._state["topics"]:
                    topic = await bot.create_forum_topic(chat_id, name, request_timeout=20)
                    with self._lock:
                        self._state["topics"][category] = topic.message_thread_id
                        self._save_state()
            return dict(self._state["topics"])

    def status_text(self):
        mode = "подробный" if self.mode == "detailed" else "обычный (пачки раз в минуту)"
        until = self._state.get("detailed_until")
        expiry = f"\nДо: {until}" if self.mode == "detailed" else ""
        return (
            f"Логи: {mode}{expiry}\nГруппа: {self.chat_id or 'не привязана'}"
            f"\nТемы: {len(self._state['topics'])}/4"
            f"\nЛокально: {self._disk_bytes / 1024 / 1024:.1f} МБ"
            f"\nПропущено записей за этот запуск: {self.dropped_records}"
        )

    @staticmethod
    def format_record(record):
        context = " ".join(
            f"{key}={record[key]}" for key in ("user_id", "operation_id") if key in record
        )
        return f"{record['time']} | {record['event']} | id={record['id']} {context}\n" + json.dumps(
            record["data"], ensure_ascii=False, indent=2
        )

    def _next_batch(self, category):
        """Drain a bounded batch; long batches become a readable file, not lost previews."""
        text = []
        updates = {}
        length = 0
        with self._lock:
            for path in sorted(self.directory.glob(f"*/{category}-*.jsonl")):
                relative = path.relative_to(self.directory).as_posix()
                cursor = self._state["cursors"].get(relative, 0)
                with path.open("rb") as stream:
                    stream.seek(cursor)
                    while raw := stream.readline():
                        if not raw.endswith(b"\n"):
                            break
                        try:
                            record = json.loads(raw)
                            preview = self.format_record(record)
                        except (ValueError, KeyError, TypeError):
                            preview = f"Повреждённая запись в {relative}; см. архив."
                        if text and (length + len(preview) + 2 > 1024 * 1024 or len(text) >= 1000):
                            return self._pack_batch(category, text, updates)
                        text.append(preview)
                        length += len(preview) + 2
                        updates[relative] = stream.tell()
            return self._pack_batch(category, text, updates)

    @staticmethod
    def _pack_batch(category, records, updates):
        text = "\n\n".join(records)
        if len(text.encode("utf-16-le")) // 2 <= 3500:
            return text, updates, None
        caption = (
            f"{TOPICS[category]}: {len(records)} записей.\n"
            "Полный текст пачки — в приложенном файле."
        )
        return caption, updates, text.encode("utf-8")

    async def _paced(self, call):
        async with self._send_lock:
            delay = max(0.0, 3.2 - (time.monotonic() - self._last_send))
            if delay:
                await asyncio.sleep(delay)
            try:
                return await call()
            except TelegramRetryAfter as exc:
                self._last_send = time.monotonic() + exc.retry_after
                raise
            finally:
                self._last_send = max(self._last_send, time.monotonic())

    async def deliver(self, bot):
        if not self.chat_id or len(self._state["topics"]) != 4:
            return
        # One message per category per pass gives all streams a fair share.
        for category in CATEGORIES:
            try:
                await self._deliver_category(bot, category)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    "Audit topic %s failed (%s); files retained", category, type(exc).__name__
                )

    async def _deliver_category(self, bot, category):
        text, updates, document = await asyncio.to_thread(self._next_batch, category)
        if not text:
            return
        if document is None:
            await self._paced(
                lambda c=category, t=text: bot.send_message(
                    self.chat_id,
                    t,
                    message_thread_id=self._state["topics"][c],
                    parse_mode=None,
                    disable_notification=True,
                    request_timeout=20,
                )
            )
        else:
            filename = f"{category}-{self._clock():%Y%m%d-%H%M%S}-{uuid.uuid4().hex[:6]}.log"
            await self._paced(
                lambda c=category, t=text, d=document, n=filename: bot.send_document(
                    self.chat_id,
                    BufferedInputFile(d, filename=n),
                    caption=t,
                    message_thread_id=self._state["topics"][c],
                    parse_mode=None,
                    disable_notification=True,
                    request_timeout=30,
                )
            )
        with self._lock:
            self._state["cursors"].update(updates)
            self._save_state()

    def _build_archive(self, path):
        relative = path.relative_to(self.directory).as_posix()
        target = path.with_name(f"{path.parent.name}-{path.stem}.zip")
        counts = Counter()
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(path, arcname=path.name)
            with archive.open(path.with_suffix(".log").name, "w") as readable:
                with path.open("rb") as source:
                    for raw in source:
                        try:
                            record = json.loads(raw)
                            counts[record["event"]] += 1
                            text = self.format_record(record)
                        except (ValueError, KeyError, TypeError):
                            text = "[Повреждённая запись; оригинальные байты сохранены в JSONL]"
                        readable.write((text + "\n\n").encode())
            archive.writestr(
                "summary.txt",
                json.dumps(
                    {
                        "source": relative,
                        "timezone": "Europe/Moscow",
                        "events": dict(counts),
                        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
        return target

    async def send_archives(self, bot, include_today=False, max_files=None):
        if include_today:
            raise ValueError("Only closed days can be archived")
        if not self.chat_id or len(self._state["topics"]) != 4:
            return
        # Previously acknowledged files can expire even when a new upload fails.
        await asyncio.to_thread(self.cleanup)
        now = self._clock().astimezone(MOSCOW)
        # Midnight rotation; send the previous day starting at 00:05 Moscow.
        cutoff = (now - timedelta(minutes=5)).date().isoformat()
        sent = 0
        for path in sorted(self.directory.glob("*/*.jsonl")):
            if path.parent.name >= cutoff:
                continue
            relative = path.relative_to(self.directory).as_posix()
            if relative in self._state["archives"]:
                continue
            archive = await asyncio.to_thread(self._build_archive, path)
            try:
                message = await self._paced(
                    lambda p=archive, r=relative: bot.send_document(
                        self.chat_id,
                        FSInputFile(p),
                        message_thread_id=self._state["topics"]["archives"],
                        caption=f"Логи: {r}\nJSONL + читаемый LOG + статистика. Время: Москва.",
                        parse_mode=None,
                        disable_notification=True,
                        request_timeout=60,
                    )
                )
                with self._lock:
                    self._state["archives"][relative] = {
                        "sent_at": self._clock().isoformat(),
                        "message_id": message.message_id,
                    }
                    self._save_state()
            finally:
                # This ZIP is reproducible. Keep its JSONL source until acknowledged + retention.
                archive.unlink(missing_ok=True)
            sent += 1
            if max_files is not None and sent >= max_files:
                await asyncio.to_thread(self.cleanup)
                return True
        await asyncio.to_thread(self.cleanup)
        return False

    def cleanup(self):
        now = self._clock()
        with self._lock:
            for relative, ack in list(self._state["archives"].items()):
                if now - datetime.fromisoformat(ack["sent_at"]) < timedelta(
                    days=self.settings.retention_days
                ):
                    continue
                path = (self.directory / relative).resolve()
                if not path.is_relative_to(self.directory.resolve()) or path.suffix != ".jsonl":
                    continue
                if path.exists():
                    # An acknowledged archive contains even records awaiting their live preview.
                    self._disk_bytes -= path.stat().st_size
                    path.unlink()
                self._state["archives"].pop(relative, None)
                self._state["cursors"].pop(relative, None)
                if path.parent.exists() and not any(path.parent.iterdir()):
                    path.parent.rmdir()
            self._save_state()

    async def run(self, bot):
        next_delivery = 0.0
        next_archive = 0.0
        reported_drops = 0
        while not self._stopped.is_set():
            try:
                now = time.monotonic()
                if self.mode == "detailed" or now >= next_delivery:
                    await self.deliver(bot)
                    next_delivery = time.monotonic() + self.settings.batch_seconds
                if now >= next_archive:
                    more = await self.send_archives(bot, max_files=1)
                    next_archive = time.monotonic() + (5 if more else 60)
                if self.dropped_records != reported_drops and self.chat_id:
                    await self._paced(
                        lambda: bot.send_message(
                            self.chat_id,
                            f"⚠️ Журнал неполный: пропущено {self.dropped_records} записей. "
                            "Проверьте свободное место и AUDIT_MAX_DISK_MB.",
                            message_thread_id=self._state["topics"].get("archives"),
                            parse_mode=None,
                            request_timeout=20,
                        )
                    )
                    reported_drops = self.dropped_records
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning("Audit delivery failed (%s); files retained", type(exc).__name__)
            try:
                await asyncio.wait_for(self._stopped.wait(), timeout=5)
            except TimeoutError:
                pass

    def start(self, bot):
        self._task = asyncio.create_task(self.run(bot), name="audit-delivery")

    async def close(self):
        self._stopped.set()
        if self._task:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        for cleanup in reversed(self._cleanups):
            try:
                cleanup()
            except Exception as exc:
                logger.warning("Audit detach failed (%s)", type(exc).__name__)
        try:
            self._save_state()
        except OSError:
            logger.error("Could not save audit state during shutdown")
