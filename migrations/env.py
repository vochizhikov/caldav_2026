import asyncio
import os

from alembic import context
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine

from db.models import Base, UTCDateTime

load_dotenv()
config = context.config
url = config.attributes.get("database_url") or os.getenv(
    "DATABASE_URL", config.get_main_option("sqlalchemy.url")
)


def render_type(kind, value, autogen_context):
    if kind == "type" and isinstance(value, UTCDateTime):
        return "sa.DateTime(timezone=True)"
    return False


def do_migrations(connection):
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        render_item=render_type,
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_online():
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        await connection.run_sync(do_migrations)
    await engine.dispose()


if context.is_offline_mode():
    context.configure(url=url, target_metadata=Base.metadata, literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(run_online())
