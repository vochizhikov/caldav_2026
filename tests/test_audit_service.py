import asyncio
import json
import zipfile
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cryptography.fernet import Fernet

from bot.observability.service import AuditService, encryption_key
from bot.observability.settings import AuditSettings


@pytest.fixture
def journal(tmp_path):
    now = [datetime(2026, 9, 20, 12, tzinfo=UTC)]
    config = AuditSettings(_env_file=None, directory=tmp_path / "audit")
    key = Fernet.generate_key().decode()
    audit = AuditService(config, key, secrets=("secret-bot-token",), clock=lambda: now[0])

    async def unpaced(call):
        return await call()

    audit._paced = unpaced
    return audit, now, key


def bound(audit):
    audit._state["chat_id"] = -100123
    audit._state["topics"] = {"conversation": 1, "caldav": 2, "database": 3, "archives": 4}
    audit._save_state()


def transport():
    bot = SimpleNamespace(send_message=AsyncMock(), send_document=AsyncMock())
    bot.send_document.return_value = SimpleNamespace(message_id=50)
    return bot


def records(audit):
    return [
        json.loads(line)
        for p in audit.directory.glob("*/*.jsonl")
        for line in p.read_text(encoding="utf-8").splitlines()
    ]


def test_secrets_are_sanitized_before_disk_and_ciphertext_is_authenticated(journal):
    audit, _, key = journal
    password = "password-marker-long"
    audit.register_secret(password)
    ciphertext = audit.encrypt(password)
    audit.emit(
        "conversation",
        "message",
        password=password,
        token="token-marker",
        text=f"fernet:{password}",
        text_encrypted=ciphertext,
        headers={"Authorization": "Basic credential-marker", password: password},
        url="https://user:pass@example.test/?token=link-secret",
    )
    record = records(audit)[0]
    raw = json.dumps(record)
    for value in (password, "token-marker", "credential-marker", "user:pass", "link-secret"):
        assert value not in raw
    assert record["data"]["text_encrypted"] == ciphertext
    fernet = Fernet(encryption_key(audit.settings, key))
    assert (
        fernet.decrypt(record["data"]["password"].removeprefix("fernet:").encode()).decode()
        == password
    )
    assert "truncated" in audit.sanitize("fernet:" + "x" * 300000)
    for suffix in (password, "?password=" + password, "\n" + password):
        assert password not in audit.sanitize(audit.encrypt("a") + suffix)


async def test_archive_budget_leaves_room_for_live_delivery(journal):
    audit, now, _ = journal
    bound(audit)
    audit.emit("conversation", "first")
    audit.emit("database", "second")
    now[0] += timedelta(days=1)
    bot = transport()
    assert await audit.send_archives(bot, max_files=1) is True
    assert bot.send_document.await_count == 1
    audit.emit("conversation", "live-message")
    await audit.deliver(bot)
    assert any("live-message" in c.args[1] for c in bot.send_message.await_args_list)
    await audit.send_archives(bot, max_files=1)
    assert bot.send_document.await_count == 2


def test_dynamic_secrets_bounded_but_config_secrets_retained(journal):
    audit, _, _ = journal
    for index in range(1500):
        audit.register_secret(f"ephemeral-input-{index:05d}")
    assert len(audit._secrets) <= 1024
    assert audit.sanitize("secret-bot-token") == "[REDACTED]"


async def test_context_crosses_threads_and_mode_expires_after_restart(journal):
    audit, now, key = journal
    with audit.operation(user_id=74529696, operation_id="example"):
        await asyncio.to_thread(audit.emit, "caldav", "http_request", method="REPORT")
    assert records(audit)[0]["operation_id"] == "example"
    assert records(audit)[0]["user_id"] == 74529696
    audit.set_mode("detailed", minutes=5)
    restored = AuditService(audit.settings, key, clock=lambda: now[0])
    assert restored.mode == "detailed"
    now[0] += timedelta(minutes=6)
    assert restored.mode == "normal"


async def test_setup_partial_failure_recovers_without_duplicate_topics(journal):
    audit, _, key = journal
    bot = SimpleNamespace(
        create_forum_topic=AsyncMock(
            side_effect=[
                SimpleNamespace(message_thread_id=1),
                RuntimeError("temporary"),
            ]
        )
    )
    with pytest.raises(RuntimeError):
        await audit.setup(bot, -100123)
    restored = AuditService(audit.settings, key)
    bot.create_forum_topic = AsyncMock(
        side_effect=[SimpleNamespace(message_thread_id=i) for i in (2, 3, 4)]
    )
    topics = await restored.setup(bot, -100123)
    assert topics == {"conversation": 1, "caldav": 2, "database": 3, "archives": 4}
    await restored.setup(bot, -100123)
    assert bot.create_forum_topic.await_count == 3
    with pytest.raises(ValueError):
        await restored.setup(bot, -100456)


async def test_delivery_routes_all_categories_and_ack_survives_restart(journal):
    audit, _, key = journal
    bound(audit)
    for category in ("conversation", "caldav", "database"):
        audit.emit(category, "test", text="hello")
    bot = transport()
    await audit.deliver(bot)
    assert [c.kwargs["message_thread_id"] for c in bot.send_message.await_args_list] == [1, 2, 3]
    assert all(c.kwargs["parse_mode"] is None for c in bot.send_message.await_args_list)
    restored = AuditService(audit.settings, key)
    await restored.deliver(bot)
    assert bot.send_message.await_count == 3


async def test_failed_topic_does_not_ack_or_block_other_categories(journal):
    audit, _, _ = journal
    bound(audit)
    for category in ("conversation", "caldav", "database"):
        audit.emit(category, "test")
    bot = transport()
    bot.send_message.side_effect = [RuntimeError("topic closed"), None, None]
    await audit.deliver(bot)
    assert bot.send_message.await_count == 3
    assert not any("conversation" in name for name in audit._state["cursors"])
    bot.send_message.side_effect = None
    await audit.deliver(bot)
    assert bot.send_message.await_count == 4
    assert any("conversation" in name for name in audit._state["cursors"])


async def test_long_batches_deliver_full_text_as_file(journal):
    audit, _, _ = journal
    bound(audit)
    body = "BEGIN:VCALENDAR\n" + "EVENT-DATA\n" * 5000 + "END:VCALENDAR"
    audit.emit("caldav", "http_response", body=body)
    bot = transport()
    await audit.deliver(bot)
    bot.send_document.assert_awaited_once()
    call = bot.send_document.await_args
    assert call.kwargs["message_thread_id"] == 2
    assert "END:VCALENDAR" in call.args[1].data.decode()
    assert "полный текст в ежедневном архиве" not in call.args[1].data.decode()
    await audit.deliver(bot)
    assert bot.send_document.await_count == 1


async def test_archives_only_closed_day_after_0005_and_delete_after_ack_retention(journal):
    audit, now, key = journal
    bound(audit)
    audit.emit("conversation", "user_message", text="/start")
    source = next(audit.directory.glob("*/*.jsonl"))
    now[0] = datetime(2026, 9, 20, 21, 4, tzinfo=UTC)  # 00:04 Moscow next day
    bot = transport()
    await audit.send_archives(bot)
    bot.send_document.assert_not_awaited()
    archived = {}

    async def capture(*args, **kwargs):
        with zipfile.ZipFile(args[1].path) as archive:
            archived.update({name: archive.read(name).decode() for name in archive.namelist()})
        return SimpleNamespace(message_id=99)

    bot.send_document.side_effect = capture
    now[0] += timedelta(minutes=1)
    await audit.send_archives(bot)
    assert any(n.endswith(".jsonl") for n in archived)
    assert any(n.endswith(".log") for n in archived)
    assert "user_message" in archived["summary.txt"]
    assert source.exists()
    restored = AuditService(audit.settings, key, clock=lambda: now[0])
    await restored.send_archives(bot)
    assert bot.send_document.await_count == 1
    now[0] += timedelta(days=3)
    restored.cleanup()
    assert not source.exists()
    assert restored._disk_bytes == 0


async def test_failed_archive_keeps_source_and_retries(journal):
    audit, now, _ = journal
    bound(audit)
    audit.settings.retention_days = 0
    audit.emit("database", "transaction_committed")
    source = next(audit.directory.glob("*/*.jsonl"))
    now[0] += timedelta(days=1)
    bot = transport()
    bot.send_document.side_effect = RuntimeError("network down")
    with pytest.raises(RuntimeError):
        await audit.send_archives(bot)
    assert source.exists()
    assert not audit._state["archives"]
    assert not list(audit.directory.glob("*/*.zip"))
    bot.send_document.side_effect = None
    await audit.send_archives(bot)
    assert not source.exists()


async def test_expired_acknowledged_sources_cleaned_even_when_new_upload_fails(journal):
    audit, now, _ = journal
    bound(audit)
    audit.emit("conversation", "old")
    old = next(audit.directory.glob("*/*.jsonl"))
    now[0] += timedelta(days=1)
    bot = transport()
    await audit.send_archives(bot)
    now[0] += timedelta(days=4)
    audit.emit("database", "new")
    new = next(audit.directory.glob("*/database-*.jsonl"))
    now[0] += timedelta(days=1)
    bot.send_document.side_effect = RuntimeError("network unavailable")
    with pytest.raises(RuntimeError):
        await audit.send_archives(bot)
    assert not old.exists()
    assert new.exists()


def test_partial_record_at_restart_does_not_swallow_next_record(journal):
    audit, _, key = journal
    audit.emit("conversation", "first")
    source = next(audit.directory.glob("*/*.jsonl"))
    with source.open("ab") as stream:
        stream.write(b'{"incomplete":')
    restored = AuditService(audit.settings, key)
    restored.emit("conversation", "after_restart")
    lines = source.read_text(encoding="utf-8").splitlines()
    assert json.loads(lines[-1])["event"] == "after_restart"
    text, updates, document = restored._next_batch("conversation")
    assert "after_restart" in text
    assert "Повреждённая" in text
    assert updates
    assert document is None


def test_storage_limits_explicitly_count_dropped_and_oversized_records(journal):
    audit, _, _ = journal
    audit.settings.segment_bytes = 65536
    audit.emit("caldav", "large", bodies=["x" * 40000] * 10)
    record = records(audit)[0]
    assert record["data"]["truncated"] is True
    assert record["data"]["original_record_bytes"] > 65536
    audit._disk_bytes = audit.settings.max_disk_mb * 1024 * 1024
    audit.emit("database", "new")
    assert audit.dropped_records == 1
    assert len(records(audit)) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("cursors", {"../../outside.jsonl": 1}),
        ("cursors", {"2026-09-20/database-000001.jsonl": -1}),
        ("topics", {"conversation": "not-an-id"}),
        ("archives", {"2026-09-20/database-000001.jsonl": {"sent_at": "yesterday"}}),
    ],
)
def test_invalid_state_fails_clearly_instead_of_breaking_worker_forever(journal, field, value):
    audit, _, key = journal
    audit._state[field] = value
    audit._save_state()
    with pytest.raises(RuntimeError, match="state.json"):
        AuditService(audit.settings, key)
