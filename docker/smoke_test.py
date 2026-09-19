"""Run inside the built image with --network none and a disposable /data volume.

Use argv[1] = empty, then existing against the same volume to verify persistence.
This script never starts polling or contacts Telegram/Yandex.
"""

import asyncio
import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from sqlalchemy import inspect, select

from bot.config import Settings
from bot.dispatcher import create_dispatcher
from bot.services.security import CredentialVault
from bot.services.synchronizer import Synchronizer
from bot.services.user_notifier import UserNotifier
from CalendarClient import CalendarClient
from db.db_config import create_database, migrate_database
from db.models import Base, User


async def check():
    assert os.getuid() == 10001, "The image must run as the unprivileged application user"
    settings = Settings(
        _env_file=None,
        bot_token="123456789:abcdefghijklmnopqrstuvwxyz123456789",
        encryption_key=Fernet.generate_key().decode(),
    )
    assert settings.database_url == "sqlite+aiosqlite:////data/calendar_bot.db"
    await asyncio.to_thread(migrate_database, settings.database_url)
    engine, sessions = create_database(settings.database_url)
    vault = CredentialVault(settings.encryption_key.get_secret_value())
    assert vault.decrypt(vault.encrypt("smoke-test")) == "smoke-test"
    client = CalendarClient()
    synchronizer = Synchronizer(sessions, client, vault, settings)
    dispatcher = create_dispatcher(sessions, synchronizer, settings)
    UserNotifier(None, sessions, settings)
    try:
        async with engine.connect() as connection:
            tables = await connection.run_sync(lambda conn: inspect(conn).get_table_names())
            assert set(tables) == {*Base.metadata.tables, "alembic_version"}
        async with sessions.begin() as session:
            user = await session.scalar(select(User).where(User.telegram_id == 123456789))
            if sys.argv[1] == "empty":
                assert user is None
                session.add(User(telegram_id=123456789, chat_id=123456789))
            else:
                assert sys.argv[1] == "existing" and user is not None
        print(f"OK: uid=10001; imports; encryption; migrations; SQLite={sys.argv[1]}")
    finally:
        await dispatcher.storage.close()
        await engine.dispose()


if __name__ == "__main__":
    unexpected_files = [
        str(path.relative_to("/app"))
        for path in Path("/app").rglob("*")
        if path.is_file() and path.suffix not in {".py", ".ini", ".mako"}
    ]
    assert not unexpected_files, f"Unexpected files in the image: {unexpected_files}"
    asyncio.run(check())
