import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select, update

from bot.observability.snapshots import SnapshotTooLarge, SnapshotTools
from db.db_config import create_database
from db.models import Base, Event, MyCalendar, Notification, Occurrence, User

NOW = datetime(2026, 9, 21, 10, 30, tzinfo=UTC)


@pytest.fixture
async def snapshot_database(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'source.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with sessions.begin() as session:
        for number in (1, 2):
            user = User(
                id=number,
                telegram_id=number * 1000,
                chat_id=number * 1000,
                yandex_login=f"person{number}@yandex.ru",
                encrypted_password=f"ciphertext-{number}",
                created_at=NOW,
            )
            session.add(user)
            await session.flush()
            session.add(
                MyCalendar(
                    id=number,
                    user_id=number,
                    remote_key=f"cal-{number}",
                    name=f"Календарь {number}",
                    url=f"https://caldav.yandex.ru/cal/{number}/",
                    initialized=True,
                    last_synced_at=NOW,
                )
            )
            await session.flush()
            session.add_all(
                [
                    Event(
                        id=number,
                        calendar_id=number,
                        remote_key=f"event-{number}",
                        uid=f"uid-{number}",
                        fingerprint=f"fingerprint-{number}",
                        summary=f"Событие {number}",
                        starts_at=NOW,
                        change_state='{"updated": true}',
                        location="Офис",
                        meeting_url="https://meet.test/",
                    ),
                    Occurrence(
                        id=number,
                        calendar_id=number,
                        remote_key=f"occurrence-{number}",
                        summary=f"Повторение {number}",
                        starts_at=NOW,
                        ends_at=NOW + timedelta(hours=1),
                    ),
                    Notification(
                        id=number,
                        user_id=number,
                        calendar_id=number,
                        dedupe_key=f"notification-{number}",
                        kind="created",
                        payload=json.dumps({"summary": f"Событие {number}"}),
                        created_at=NOW,
                        next_attempt_at=NOW,
                        expires_at=NOW + timedelta(days=1),
                    ),
                ]
            )
    tools = SnapshotTools(engine, sessions, tmp_path / "snapshots")
    yield engine, sessions, tools
    await engine.dispose()


@pytest.mark.parametrize("identifier", [{"telegram_id": 1000}, {"user_id": 1}])
async def test_user_snapshot_includes_all_fields_and_linked_rows_without_credentials(
    snapshot_database, identifier
):
    _, _, tools = snapshot_database
    path = await tools.user_snapshot(
        **identifier, metadata={"first_name": "Тест", "username": "tester", "password": "hidden"}
    )
    raw = path.read_text(encoding="utf-8")
    snapshot = json.loads(raw)
    assert path.parent == tools.directory and path.suffix == ".json"
    assert snapshot["user"]["id"] == 1 and snapshot["user"]["telegram_id"] == 1000
    assert snapshot["user"]["connected"] is True
    assert set(snapshot["user"]) == {
        column.name for column in User.__table__.columns if column.name != "encrypted_password"
    } | {"connected"}
    assert snapshot["user"]["created_at"] == NOW.isoformat()
    assert snapshot["telegram_profile"] == {"first_name": "Тест", "username": "tester"}
    for model in (MyCalendar, Event, Occurrence):
        rows = snapshot[model.__tablename__]
        assert len(rows) == 1 and rows[0]["id"] == 1
        assert set(rows[0]) == {column.name for column in model.__table__.columns}
    assert snapshot["counts"] == {
        "users": 1,
        "calendars": 1,
        "events": 1,
        "occurrences": 1,
    }
    assert "notifications" not in snapshot
    assert "ciphertext-" not in raw and "encrypted_password" not in raw and "hidden" not in raw
    assert "person2" not in raw and "Событие 2" not in raw
    assert snapshot["events"][0]["change_state"] == '{"updated": true}'
    assert snapshot["events"][0]["location"] == "Офис"
    path.unlink()


async def test_unknown_user_creates_no_file_and_stats_are_readonly(snapshot_database):
    _, sessions, tools = snapshot_database
    expected = {"users": 2, "calendars": 2, "events": 2, "occurrences": 2, "notifications": 2}
    assert await tools.stats() == expected
    assert await tools.user_snapshot(9999) is None
    assert await tools.user_snapshot(user_id=9999) is None
    assert not tools.directory.exists()
    async with sessions() as session:
        assert (await session.get(User, 1)).encrypted_password == "ciphertext-1"
    assert await tools.stats() == expected


async def test_unconnected_user_exports_false_connected_flag(snapshot_database):
    _, sessions, tools = snapshot_database
    async with sessions.begin() as session:
        await session.execute(update(User).where(User.id == 1).values(encrypted_password=None))
    path = await tools.user_snapshot(1000)
    assert json.loads(path.read_text(encoding="utf-8"))["user"]["connected"] is False


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"telegram_id": 1000, "user_id": 1},
        {"telegram_id": "1 OR 1=1"},
        {"telegram_id": -1},
        {"user_id": 0},
        {"telegram_id": True},
    ],
)
async def test_snapshot_rejects_ambiguous_or_noninteger_ids(snapshot_database, arguments):
    _, _, tools = snapshot_database
    with pytest.raises(ValueError):
        await tools.user_snapshot(**arguments)
    assert not tools.directory.exists()


async def test_size_limit_removes_partial_export_and_preserves_database(snapshot_database):
    engine, sessions, original = snapshot_database
    tools = SnapshotTools(engine, sessions, original.directory, max_bytes=500)
    with pytest.raises(SnapshotTooLarge):
        await tools.user_snapshot(1000)
    assert list(tools.directory.iterdir()) == []
    assert (await tools.stats())["users"] == 2
    async with sessions() as session:
        assert (await session.get(User, 1)).encrypted_password == "ciphertext-1"


async def test_export_streams_multiple_batches_and_uses_unique_filenames(snapshot_database):
    _, sessions, tools = snapshot_database
    async with sessions.begin() as session:
        session.add_all(
            [
                Event(
                    calendar_id=1,
                    remote_key=f"bulk-{number}",
                    uid=f"bulk-{number}",
                    fingerprint="bulk",
                    summary=f"Строка {number}",
                    starts_at=NOW,
                )
                for number in range(250)
            ]
        )
    first = await tools.user_snapshot(1000)
    second = await tools.user_snapshot(1000)
    assert first != second and first.exists() and second.exists()
    snapshot = json.loads(first.read_text(encoding="utf-8"))
    assert len(snapshot["events"]) == snapshot["counts"]["events"] == 251
    assert snapshot["events"][-1]["summary"] == "Строка 249"


async def test_user_snapshot_stays_consistent_when_wal_writer_commits_mid_export(
    snapshot_database, monkeypatch
):
    engine, sessions, tools = snapshot_database
    original = tools._write_table
    changed = False

    async def write_after_concurrent_commit(connection, writer, table, condition):
        nonlocal changed
        if not changed:
            changed = True
            async with sessions.begin() as session:
                await session.execute(
                    update(User).where(User.id == 1).values(timezone="Asia/Tokyo")
                )
                await session.execute(update(Event).where(Event.id == 1).values(summary="После"))
        return await original(connection, writer, table, condition)

    monkeypatch.setattr(tools, "_write_table", write_after_concurrent_commit)
    path = await tools.user_snapshot(1000)
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["user"]["timezone"] == "Europe/Moscow"
    assert snapshot["events"][0]["summary"] == "Событие 1"
    async with engine.connect() as connection:
        assert await connection.scalar(select(User.timezone).where(User.id == 1)) == "Asia/Tokyo"
        assert await connection.scalar(select(Event.summary).where(Event.id == 1)) == "После"


async def test_interrupted_json_export_removes_its_partial_file(snapshot_database, monkeypatch):
    _, _, tools = snapshot_database

    async def cancel_during_export(*args):
        raise asyncio.CancelledError()

    monkeypatch.setattr(tools, "_write_table", cancel_during_export)
    with pytest.raises(asyncio.CancelledError):
        await tools.user_snapshot(1000)
    assert list(tools.directory.iterdir()) == []


async def test_users_table_snapshot_omits_passwords_and_unrelated_tables(snapshot_database):
    _, sessions, tools = snapshot_database
    async with sessions.begin() as session:
        await session.execute(update(User).where(User.id == 2).values(encrypted_password=None))
    path = await tools.users_snapshot()
    raw = path.read_text(encoding="utf-8")
    snapshot = json.loads(raw)
    assert path.name.startswith("users-") and path.suffix == ".json"
    assert set(snapshot) == {"format_version", "created_at", "users", "counts"}
    assert [user["id"] for user in snapshot["users"]] == [1, 2]
    assert [user["connected"] for user in snapshot["users"]] == [True, False]
    assert snapshot["counts"] == {"users": 2}
    assert "ciphertext-" not in raw and "encrypted_password" not in raw
    assert "Событие" not in raw and "Календарь" not in raw
    assert (await tools.stats())["notifications"] == 2


async def test_users_table_export_streams_multiple_batches(snapshot_database):
    _, sessions, tools = snapshot_database
    async with sessions.begin() as session:
        session.add_all(
            [User(telegram_id=number + 10000, chat_id=number + 10000) for number in range(250)]
        )
    path = await tools.users_snapshot()
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    assert snapshot["counts"]["users"] == len(snapshot["users"]) == 252
    assert [row["id"] for row in snapshot["users"]] == list(range(1, 253))
    assert all(not row["connected"] for row in snapshot["users"][2:])


async def test_users_table_export_size_limit_removes_partial_file(snapshot_database):
    engine, sessions, original = snapshot_database
    tools = SnapshotTools(engine, sessions, original.directory, max_bytes=500)
    with pytest.raises(SnapshotTooLarge):
        await tools.users_snapshot()
    assert list(tools.directory.iterdir()) == []
    assert (await tools.stats())["users"] == 2


async def test_empty_users_table_produces_valid_json(tmp_path):
    engine, sessions = create_database("sqlite+aiosqlite:///:memory:")
    try:
        async with engine.begin() as connection:
            await connection.run_sync(Base.metadata.create_all)
        tools = SnapshotTools(engine, sessions, tmp_path / "snapshots")
        path = await tools.users_snapshot()
        snapshot = json.loads(path.read_text(encoding="utf-8"))
        assert snapshot["users"] == [] and snapshot["counts"] == {"users": 0}
    finally:
        await engine.dispose()
