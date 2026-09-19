import asyncio
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import MetaData, Table, inspect, select

from db.db_config import create_database, migrate_database
from db.models import Base, Event, MyCalendar, Notification, User
from db.transfer import transfer

ROOT = Path(__file__).resolve().parent.parent


async def test_migrations_are_repeatable_and_match_models(tmp_path):
    url = f"sqlite+aiosqlite:///{tmp_path / 'migrated.db'}"
    await asyncio.to_thread(migrate_database, url)
    await asyncio.to_thread(migrate_database, url)
    engine, sessions = create_database(url)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert set(tables) == {*Base.metadata.tables, "alembic_version"}
        config = Config(str(ROOT / "alembic.ini"))
        config.attributes["database_url"] = url
        await asyncio.to_thread(command.check, config)
    finally:
        await engine.dispose()


async def test_transfer_preserves_data_and_rejects_nonempty_target(tmp_path):
    source_url = f"sqlite+aiosqlite:///{tmp_path / 'source.db'}"
    target_url = f"sqlite+aiosqlite:///{tmp_path / 'target.db'}"
    await asyncio.to_thread(migrate_database, source_url)
    engine, sessions = create_database(source_url)
    async with sessions.begin() as session:
        user = User(
            telegram_id=9_999_999_999,
            chat_id=9_999_999_999,
            encrypted_password="ciphertext",
            advance_minutes=10,
        )
        session.add(user)
        await session.flush()
        session.add(
            MyCalendar(
                user_id=user.id, name="Работа", url="https://example.test", remote_key="calendar"
            )
        )
    await engine.dispose()
    assert (await transfer(source_url, target_url))["users"] == 1
    target, sessions = create_database(target_url)
    try:
        async with sessions() as session:
            user = await session.scalar(select(User))
            assert user.telegram_id == 9_999_999_999 and user.advance_minutes == 10
            assert user.encrypted_password == "ciphertext" and user.created_at.tzinfo
            assert (await session.scalar(select(MyCalendar))).user_id == user.id
        with pytest.raises(ValueError, match="содержит данные"):
            await transfer(source_url, target_url)
        with pytest.raises(ValueError, match="должны различаться"):
            await transfer(source_url, source_url)
    finally:
        await target.dispose()


def test_postgresql_migration_compiles_without_database():
    output = StringIO()
    config = Config(str(ROOT / "alembic.ini"), output_buffer=output)
    config.attributes["database_url"] = "postgresql+asyncpg://example:example@localhost/example"
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()
    assert "CREATE TABLE users" in sql and "BIGINT" in sql and "TIMESTAMP WITH TIME ZONE" in sql


async def test_upgrading_existing_database_preserves_calendar_selection(tmp_path, now):
    url = f"sqlite+aiosqlite:///{tmp_path / 'upgrade.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    await asyncio.to_thread(command.upgrade, config, "2dc34cb21c87")
    engine, sessions = create_database(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(User.__table__.insert().values(id=1, telegram_id=1, chat_id=1))
            await connection.execute(
                MyCalendar.__table__.insert().values(
                    id=1,
                    user_id=1,
                    name="Выбранный",
                    url="https://example.test/",
                    remote_key="selected",
                    enabled=True,
                    initialized=True,
                )
            )
            for status in ("pending", "sent"):
                await connection.execute(
                    Notification.__table__.insert().values(
                        user_id=1,
                        calendar_id=1,
                        kind="updated",
                        dedupe_key=status,
                        payload="{}",
                        status=status,
                        expires_at=now,
                    )
                )
        await asyncio.to_thread(migrate_database, url)
        async with sessions() as session:
            calendar = await session.get(MyCalendar, 1)
            assert calendar.enabled and not calendar.initialized
            assert calendar.notifications_since is None
            statuses = dict(
                (await session.execute(select(Notification.dedupe_key, Notification.status))).all()
            )
            assert statuses == {"pending": "discarded", "sent": "sent"}
        async with sessions.begin() as session:
            (await session.get(MyCalendar, 1)).initialized = True
        await asyncio.to_thread(migrate_database, url)
        async with sessions() as session:
            assert (await session.get(MyCalendar, 1)).initialized
    finally:
        await engine.dispose()


async def test_event_metadata_migrations_preserve_existing_events(tmp_path, now):
    url = f"sqlite+aiosqlite:///{tmp_path / 'change-state-upgrade.db'}"
    config = Config(str(ROOT / "alembic.ini"))
    config.attributes["database_url"] = url
    await asyncio.to_thread(command.upgrade, config, "79843f9da12b")
    engine, sessions = create_database(url)
    try:
        async with engine.begin() as connection:
            await connection.execute(User.__table__.insert().values(id=1, telegram_id=1, chat_id=1))
            await connection.execute(
                MyCalendar.__table__.insert().values(
                    id=1,
                    user_id=1,
                    remote_key="calendar",
                    url="https://example.test/",
                    name="Work",
                    enabled=True,
                    initialized=True,
                    notifications_since=now,
                )
            )
            legacy_events = await connection.run_sync(
                lambda conn: Table("events", MetaData(), autoload_with=conn)
            )
            await connection.execute(
                legacy_events.insert().values(
                    id=1,
                    calendar_id=1,
                    remote_key="event",
                    uid="event",
                    fingerprint="old",
                    summary="Existing event",
                    starts_at=now,
                    revision=1,
                    all_day=False,
                    cancelled=False,
                )
            )
        await asyncio.to_thread(migrate_database, url)
        async with sessions() as session:
            event = await session.get(Event, 1)
            assert event.summary == "Existing event" and event.fingerprint == "old"
            assert event.change_state is None
            assert event.location == "" and event.meeting_url == ""
            calendar = await session.get(MyCalendar, 1)
            assert calendar.enabled and calendar.initialized
            assert calendar.notifications_since == now
    finally:
        await engine.dispose()
