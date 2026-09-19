import json

import pytest
from sqlalchemy import ForeignKey, String, delete, func, insert, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from bot.observability.database import MAX_STATEMENTS, install_database_audit
from db.db_config import create_database


class AuditBase(DeclarativeBase):
    pass


class AuditRecord(AuditBase):
    __tablename__ = "audit_test_records"

    id: Mapped[int] = mapped_column(primary_key=True)
    value: Mapped[str] = mapped_column(String)
    password: Mapped[str | None] = mapped_column(String)


class AuditChild(AuditBase):
    __tablename__ = "audit_test_children"

    id: Mapped[int] = mapped_column(primary_key=True)
    record_id: Mapped[int] = mapped_column(
        ForeignKey("audit_test_records.id", deferrable=True, initially="DEFERRED")
    )


class FakeAudit:
    mode = "normal"

    def __init__(self):
        self.events = []

    def sanitize(self, value):
        if isinstance(value, dict):
            return {
                key: "[encrypted]" if "password" in key else self.sanitize(item)
                for key, item in value.items()
            }
        if isinstance(value, (tuple, list)):
            return [self.sanitize(item) for item in value]
        return value

    def emit(self, category, event, **fields):
        self.events.append({"category": category, "event": event, **fields})


@pytest.fixture
async def audited_database(tmp_path):
    engine, sessions = create_database(f"sqlite+aiosqlite:///{tmp_path / 'audit.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(AuditBase.metadata.create_all)
    audit = FakeAudit()
    cleanup = install_database_audit(engine, sessions, audit)
    yield engine, sessions, audit, cleanup
    cleanup()
    await engine.dispose()


def transactions(audit, status="committed"):
    return [event for event in audit.events if event["event"] == f"transaction_{status}"]


async def test_flush_is_reported_only_after_commit_and_sanitized(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions() as session:
        record = AuditRecord(value="created", password="private-test-value")
        session.add(record)
        await session.flush()
        assert audit.events == []
        assert "private-test-value" not in repr(session.sync_session.info)
        await session.commit()
        record.value = "updated"
        await session.commit()
        await session.delete(record)
        await session.commit()
    events = transactions(audit)
    assert len(events) == 3
    assert [item["statements"][0]["operation"] for item in events] == ["INSERT", "UPDATE", "DELETE"]
    assert len({event["transaction_id"] for event in events}) == 3
    assert "private-test-value" not in json.dumps(audit.events)
    assert events[0]["statements"][0]["parameters"][0]["password"] == "[encrypted]"


@pytest.mark.parametrize("explicit_rollback", [True, False])
async def test_rollback_or_close_never_reports_success(audited_database, explicit_rollback):
    _, sessions, audit, _ = audited_database
    async with sessions() as session:
        session.add(AuditRecord(value="discarded"))
        await session.flush()
        if explicit_rollback:
            await session.rollback()
    assert not transactions(audit)
    assert len(transactions(audit, "rolled_back")) == 1
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AuditRecord)) == 0


async def test_commit_time_constraint_failure_is_not_success(audited_database):
    _, sessions, audit, _ = audited_database
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(AuditChild(record_id=999))
            await session.flush()
            assert not audit.events
    assert not transactions(audit)
    assert len(transactions(audit, "rolled_back")) == 1
    assert any(event["event"] == "statement_failed" for event in audit.events)
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AuditChild)) == 0


async def test_bulk_sql_is_captured(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions.begin() as session:
        await session.execute(insert(AuditRecord), [{"value": "first"}, {"value": "second"}])
        await session.execute(update(AuditRecord).values(value="updated"))
        await session.execute(delete(AuditRecord))
    statements = transactions(audit)[0]["statements"]
    assert [statement["operation"] for statement in statements] == ["INSERT", "UPDATE", "DELETE"]
    assert statements[1]["rowcount"] == 2
    assert statements[2]["rowcount"] == 2


async def test_savepoint_release_waits_for_outer_commit(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions() as session:
        session.add(AuditRecord(value="outer"))
        await session.flush()
        async with session.begin_nested():
            session.add(AuditRecord(value="inner"))
            await session.flush()
        assert not audit.events
        await session.rollback()
    assert not transactions(audit)
    assert len(transactions(audit, "rolled_back")[0]["statements"]) == 2


async def test_savepoint_rollback_does_not_taint_outer_commit(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions.begin() as session:
        session.add(AuditRecord(value="outer"))
        await session.flush()
        nested = await session.begin_nested()
        session.add(AuditRecord(value="inner"))
        await session.flush()
        await nested.rollback()
    rolled_back = transactions(audit, "rolled_back")
    committed = transactions(audit)
    assert len(rolled_back) == len(committed) == 1
    assert rolled_back[0]["parent_transaction_id"] == committed[0]["transaction_id"]
    assert rolled_back[0]["statements"][0]["parameters"][0]["value"] == "inner"
    assert committed[0]["statements"][0]["parameters"][0]["value"] == "outer"


async def test_detailed_reads_factory_isolation_and_cleanup(audited_database):
    engine, sessions, audit, cleanup = audited_database
    other_sessions = async_sessionmaker(engine, expire_on_commit=False)
    async with other_sessions.begin() as session:
        session.add(AuditRecord(value="unrelated factory"))
    assert audit.events == []
    async with sessions.begin() as session:
        await session.scalars(select(AuditRecord))
    assert audit.events == []
    audit.mode = "detailed"
    async with sessions.begin() as session:
        await session.scalars(select(AuditRecord))
    assert transactions(audit)[0]["statements"][0]["operation"] == "SELECT"
    audit.events.clear()
    cleanup()
    cleanup()  # Teardown is deliberately idempotent.
    async with sessions.begin() as session:
        session.add(AuditRecord(value="after detach"))
    assert audit.events == []


async def test_raw_sql_literals_are_not_logged(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions.begin() as session:
        await session.execute(
            text("INSERT INTO audit_test_records(value, password) VALUES ('x', 'secret-literal')")
        )
    assert len(transactions(audit)) == 1
    assert "secret-literal" not in json.dumps(audit.events)


async def test_statement_failure_omits_exception_message(audited_database):
    _, sessions, audit, _ = audited_database
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            await session.execute(insert(AuditRecord).values(id=1, value="ok"))
            await session.execute(
                insert(AuditRecord).values(id=1, value="conflict", password="private-test-value")
            )
    assert not transactions(audit)
    assert "private-test-value" not in json.dumps(audit.events)
    assert any(event["event"] == "statement_failed" for event in audit.events)


async def test_flush_failure_cannot_turn_previous_writes_into_success(audited_database):
    _, sessions, audit, _ = audited_database
    with pytest.raises(IntegrityError):
        async with sessions.begin() as session:
            session.add(AuditRecord(id=1, value="first"))
            await session.flush()
            session.add(AuditRecord(id=1, value="duplicate"))
            await session.flush()
    assert not transactions(audit)
    assert len(transactions(audit, "rolled_back")) == 1


async def test_capture_error_does_not_break_database_commit(audited_database, monkeypatch):
    _, sessions, audit, _ = audited_database

    def failed_sanitizer(value):
        raise ValueError("sensitive error details")

    monkeypatch.setattr(audit, "sanitize", failed_sanitizer)
    async with sessions.begin() as session:
        session.add(AuditRecord(value="still persisted"))
    async with sessions() as session:
        assert await session.scalar(select(func.count()).select_from(AuditRecord)) == 1
    assert any(event["event"] == "capture_failed" for event in audit.events)
    assert "sensitive error details" not in json.dumps(audit.events)


async def test_transaction_buffer_is_bounded(audited_database):
    _, sessions, audit, _ = audited_database
    async with sessions.begin() as session:
        for _ in range(MAX_STATEMENTS + 3):
            await session.execute(insert(AuditRecord).values(value="batch"))
    event = transactions(audit)[0]
    assert len(event["statements"]) == MAX_STATEMENTS
    assert event["omitted_statements"] == 3
