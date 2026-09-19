from datetime import UTC, datetime

import pytest
from cryptography.fernet import Fernet

from bot.config import Settings
from db.db_config import create_database
from db.models import Base, MyCalendar, User


@pytest.fixture
def now():
    return datetime(2026, 9, 18, 9, 0, tzinfo=UTC)


@pytest.fixture
def settings():
    return Settings(
        _env_file=None,
        bot_token="123456789:abcdefghijklmnopqrstuvwxyz123456789",
        encryption_key=Fernet.generate_key().decode(),
    )


@pytest.fixture
async def sessions(tmp_path):
    engine, factory = create_database(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield factory
    await engine.dispose()


@pytest.fixture
async def account(sessions, now):
    async with sessions.begin() as session:
        user = User(
            telegram_id=5_000_000_001,
            chat_id=5_000_000_001,
            yandex_login="test@yandex.ru",
            encrypted_password="encrypted",
        )
        session.add(user)
        await session.flush()
        calendar = MyCalendar(
            user_id=user.id,
            remote_key="test",
            name="Рабочий",
            enabled=True,
            url="https://caldav.yandex.ru/cal/test/",
            last_synced_at=now,
        )
        session.add(calendar)
        await session.flush()
        return user.id, calendar.id
