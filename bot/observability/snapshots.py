"""On-demand, consistent database exports.

JSON exports omit credential material. These helpers do not alter source records,
the application session factory, or configuration. The caller owns successful
output files and should delete them after sending.
"""

import json
import tempfile
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select

from db.models import Event, MyCalendar, Notification, Occurrence, User

TABLES = (
    User.__table__,
    MyCalendar.__table__,
    Event.__table__,
    Occurrence.__table__,
    Notification.__table__,
)
PROFILE_FIELDS = {
    "first_name",
    "last_name",
    "username",
    "language_code",
    "is_premium",
    "first_seen_at",
    "last_seen_at",
    "status",
    "added_to_attachment_menu",
    "allows_write_to_pm",
}
STREAM_BATCH_SIZE = 100


class SnapshotError(RuntimeError):
    """A requested snapshot could not be generated safely."""


class SnapshotUnsupported(SnapshotError):
    """This snapshot format is unavailable for the configured database."""


class SnapshotTooLarge(SnapshotError):
    """The requested export exceeds the configured upload/disk limit."""


def _json_default(value):
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"Unsupported snapshot value: {type(value).__name__}")


class _BoundedJSON:
    def __init__(self, stream, max_bytes):
        self.stream = stream
        self.max_bytes = max_bytes
        self.written = 0
        self.encoder = json.JSONEncoder(ensure_ascii=False, default=_json_default)

    def write(self, text):
        data = text.encode("utf-8")
        if self.written + len(data) > self.max_bytes:
            raise SnapshotTooLarge(
                f"Снимок превышает предел {self.max_bytes} байт; частичный файл удалён."
            )
        self.stream.write(data)
        self.written += len(data)

    def json(self, value):
        for part in self.encoder.iterencode(value):
            self.write(part)


class SnapshotTools:
    def __init__(self, engine, sessions, directory, max_bytes=45 * 1024 * 1024):
        if type(max_bytes) is not int or max_bytes < 1:
            raise ValueError("max_bytes must be a positive integer")
        self.engine = engine
        self.sessions = sessions
        self.directory = Path(directory)
        self.max_bytes = max_bytes

    @asynccontextmanager
    async def _read_snapshot(self):
        async with self.engine.connect() as connection:
            async with connection.begin():
                if self.engine.dialect.name == "sqlite":
                    # SQLite's legacy transaction mode does not BEGIN on SELECT.
                    # Explicit BEGIN keeps every table on the same WAL snapshot.
                    await connection.exec_driver_sql("BEGIN")
                elif self.engine.dialect.name == "postgresql":
                    await connection.exec_driver_sql(
                        "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                    )
                else:
                    raise SnapshotUnsupported("Снимки поддерживаются для SQLite и PostgreSQL.")
                yield connection

    async def stats(self) -> dict[str, int]:
        """Return consistent row counts for the five application tables."""
        async with self._read_snapshot() as connection:
            return {
                table.name: int(await connection.scalar(select(func.count()).select_from(table)))
                for table in TABLES
            }

    async def _write_table(self, connection, writer, table, condition):
        statement = select(table).where(condition).order_by(table.c.id)
        return await self._write_query(connection, writer, table.name, statement)

    async def _write_query(self, connection, writer, name, statement):
        writer.write(f',\n  "{name}": [')
        count = 0
        async with connection.stream(
            statement.execution_options(yield_per=STREAM_BATCH_SIZE)
        ) as rows:
            async for batch in rows.mappings().partitions(STREAM_BATCH_SIZE):
                for row in batch:
                    writer.write(",\n    " if count else "\n    ")
                    writer.json(dict(row))
                    count += 1
        writer.write("\n  ]" if count else "]")
        return count

    @staticmethod
    def _users_query():
        columns = [
            column for column in User.__table__.columns if column.name != "encrypted_password"
        ]
        connected = (User.encrypted_password.is_not(None) & (User.encrypted_password != "")).label(
            "connected"
        )
        return select(*columns, connected).order_by(User.id)

    async def users_snapshot(self) -> Path:
        """Export the users table as JSON with connected flags instead of passwords."""
        path = None
        try:
            async with self._read_snapshot() as connection:
                self.directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb", dir=self.directory, prefix="users-", suffix=".json", delete=False
                ) as stream:
                    path = Path(stream.name)
                    writer = _BoundedJSON(stream, self.max_bytes)
                    writer.write('{\n  "format_version": 1,\n  "created_at": ')
                    writer.json(datetime.now(UTC))
                    count = await self._write_query(
                        connection, writer, "users", self._users_query()
                    )
                    writer.write(',\n  "counts": ')
                    writer.json({"users": count})
                    writer.write("\n}\n")
            return path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise

    async def user_snapshot(self, telegram_id=None, *, user_id=None, metadata=None) -> Path | None:
        """Export exactly one user and linked rows; return None for an unknown ID.

        Provide either a Telegram ID or the application's users.id. Database
        passwords are never selected: only the derived connected flag is read.
        Export construction is bounded by max_bytes and streams related rows.
        """
        if (telegram_id is None) == (user_id is None):
            raise ValueError("Укажите один ID: telegram_id или user_id.")
        identifier = telegram_id if telegram_id is not None else user_id
        if type(identifier) is not int or identifier <= 0:
            raise ValueError("ID должен быть положительным целым числом.")
        condition = (
            User.telegram_id == identifier if telegram_id is not None else User.id == identifier
        )
        profile = {
            key: value
            for key, value in (metadata or {}).items()
            if key in PROFILE_FIELDS and (value is None or isinstance(value, (str, bool, int)))
        }
        path = None
        try:
            async with self._read_snapshot() as connection:
                result = await connection.execute(self._users_query().where(condition))
                user = result.mappings().one_or_none()
                if user is None:
                    return None
                internal_id = user["id"]
                calendars = select(MyCalendar.id).where(MyCalendar.user_id == internal_id)
                linked = (
                    (MyCalendar.__table__, MyCalendar.user_id == internal_id),
                    (Event.__table__, Event.calendar_id.in_(calendars)),
                    (Occurrence.__table__, Occurrence.calendar_id.in_(calendars)),
                )
                self.directory.mkdir(parents=True, exist_ok=True)
                with tempfile.NamedTemporaryFile(
                    mode="wb",
                    dir=self.directory,
                    prefix=f"user-{internal_id}-",
                    suffix=".json",
                    delete=False,
                ) as stream:
                    path = Path(stream.name)
                    writer = _BoundedJSON(stream, self.max_bytes)
                    writer.write('{\n  "format_version": 1,\n  "created_at": ')
                    writer.json(datetime.now(UTC))
                    writer.write(',\n  "user": ')
                    writer.json(dict(user))
                    writer.write(',\n  "telegram_profile": ')
                    writer.json(profile)
                    counts = {"users": 1}
                    for table, filter_condition in linked:
                        counts[table.name] = await self._write_table(
                            connection, writer, table, filter_condition
                        )
                    writer.write(',\n  "counts": ')
                    writer.json(counts)
                    writer.write("\n}\n")
            return path
        except BaseException:
            if path is not None:
                path.unlink(missing_ok=True)
            raise
