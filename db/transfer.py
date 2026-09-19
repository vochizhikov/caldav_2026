"""Offline database transfer: stop the bot before running this command."""

import argparse
import asyncio
import os
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from dotenv import load_dotenv
from sqlalchemy import func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import create_async_engine

from db.db_config import create_database, migrate_database
from db.models import Base

ROOT = Path(__file__).resolve().parent.parent


def prepare_source(source_url: str, target_url: str):
    source = make_url(source_url)
    target = make_url(target_url)
    if source == target:
        raise ValueError("Исходная и целевая базы должны различаться.")
    if source.get_backend_name() == "sqlite":
        source_file = Path(source.database or "").resolve()
        if not source_file.is_file():
            raise ValueError("Исходный файл SQLite не найден.")
        if target.get_backend_name() == "sqlite":
            if source_file == Path(target.database or "").resolve():
                raise ValueError("Исходная и целевая базы должны различаться.")
        source = source.set(
            database=f"file:{source_file.as_posix()}", query={"mode": "ro", "uri": "true"}
        )
    return source


async def transfer(source_url: str, target_url: str) -> dict[str, int]:
    source = await asyncio.to_thread(prepare_source, source_url, target_url)
    source_engine = create_async_engine(source)
    target_engine = None
    try:
        async with source_engine.connect() as origin:
            config = Config(str(ROOT / "alembic.ini"))
            config.set_main_option("script_location", str(ROOT / "migrations"))
            head = ScriptDirectory.from_config(config).get_current_head()
            version = await origin.scalar(text("SELECT version_num FROM alembic_version"))
            if version != head:
                raise ValueError(
                    "Перед переносом обновите схему исходной базы: alembic upgrade head."
                )
            await asyncio.to_thread(migrate_database, target_url)
            target_engine, _ = create_database(target_url)
            counts = {}
            async with target_engine.begin() as destination:
                for table in Base.metadata.sorted_tables:
                    if await destination.scalar(select(func.count()).select_from(table)):
                        raise ValueError("Целевая база содержит данные. Выберите пустую базу.")
                for table in Base.metadata.sorted_tables:
                    count = 0
                    result = await origin.stream(select(table).order_by(table.c.id))
                    async for batch in result.mappings().partitions(500):
                        await destination.execute(table.insert(), [dict(row) for row in batch])
                        count += len(batch)
                    counts[table.name] = count
                    if destination.dialect.name == "postgresql":
                        # Table names originate only from our static ORM metadata.
                        await destination.execute(
                            text(
                                "SELECT setval(pg_get_serial_sequence(:table_name, 'id'), "
                                f"COALESCE((SELECT MAX(id) FROM {table.name}), 1), "
                                f"EXISTS(SELECT 1 FROM {table.name}))"
                            ),
                            {"table_name": table.name},
                        )
                for table in Base.metadata.sorted_tables:
                    actual = await destination.scalar(select(func.count()).select_from(table))
                    if actual != counts[table.name]:
                        raise ValueError("Проверка числа перенесённых строк не пройдена.")
            return counts
    finally:
        await source_engine.dispose()
        if target_engine is not None:
            await target_engine.dispose()


def main():
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="Source SQLAlchemy async database URL")
    parser.add_argument(
        "--target",
        default=os.getenv("DATABASE_URL"),
        help="Target URL; defaults to DATABASE_URL from .env",
    )
    args = parser.parse_args()
    if not args.target:
        parser.error("Укажите --target или DATABASE_URL в .env.")
    try:
        counts = asyncio.run(transfer(args.source, args.target))
    except ValueError as exc:
        parser.exit(1, f"{exc}\n")
    except Exception as exc:
        # Database exception text can contain connection credentials.
        parser.exit(1, f"Перенос не завершён ({type(exc).__name__}). Проверьте базы и их версии.\n")
    else:
        for table, count in counts.items():
            print(f"{table}: {count}")
        print("Перенос завершён. Используйте прежний ENCRYPTION_KEY.")


if __name__ == "__main__":
    main()
