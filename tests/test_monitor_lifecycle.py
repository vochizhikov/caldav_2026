import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from bot import bot as bot_runtime


@pytest.mark.parametrize("audit_enabled", [True, False])
async def test_shutdown_finishes_inflight_updates_before_closing_audit_and_resources(
    settings, monkeypatch, audit_enabled
):
    events = []
    handler_started = asyncio.Event()
    release_handler = asyncio.Event()
    polling_returned = asyncio.Event()
    sync_started = asyncio.Event()
    notifier_started = asyncio.Event()
    audit_is_open = True

    async def close_audit():
        nonlocal audit_is_open
        events.append("audit_closed")
        audit_is_open = False

    audit = SimpleNamespace(close=AsyncMock(side_effect=close_audit))
    engine = SimpleNamespace(
        dispose=AsyncMock(side_effect=lambda: events.append("engine_disposed"))
    )
    bot = SimpleNamespace(
        delete_webhook=AsyncMock(),
        session=SimpleNamespace(
            close=AsyncMock(side_effect=lambda: events.append("bot_session_closed"))
        ),
    )
    dispatcher = SimpleNamespace(
        storage=SimpleNamespace(
            close=AsyncMock(side_effect=lambda: events.append("storage_closed"))
        ),
        resolve_used_update_types=lambda: ["message", "callback_query"],
        _handle_update_tasks=set(),
    )

    async def background_worker(name, started):
        started.set()
        try:
            await asyncio.Event().wait()
        finally:
            events.append(f"{name}_stopped")

    async def sync_run():
        await background_worker("sync", sync_started)

    async def notifier_run():
        await background_worker("notifier", notifier_started)

    synchronizer = SimpleNamespace(run=AsyncMock(side_effect=sync_run))
    notifier = SimpleNamespace(run=AsyncMock(side_effect=notifier_run))

    async def user_handler():
        handler_started.set()
        await release_handler.wait()
        # The final reply / commit must still be able to use audit hooks.
        if audit_enabled:
            assert audit_is_open
        events.append("handler_finished")

    async def start_polling(*args, **kwargs):
        assert kwargs["close_bot_session"] is False
        handler = asyncio.create_task(user_handler())
        dispatcher._handle_update_tasks.add(handler)
        handler.add_done_callback(dispatcher._handle_update_tasks.discard)
        await handler_started.wait()
        await sync_started.wait()
        await notifier_started.wait()
        events.append("polling_returned")
        polling_returned.set()

    dispatcher.start_polling = AsyncMock(side_effect=start_polling)
    sessions, client, vault = object(), object(), object()
    monkeypatch.setattr(bot_runtime, "get_settings", lambda: settings)
    monkeypatch.setattr(bot_runtime, "migrate_database", lambda url: None)
    monkeypatch.setattr(bot_runtime, "create_database", lambda url: (engine, sessions))
    monkeypatch.setattr(bot_runtime, "CredentialVault", lambda key: vault)
    monkeypatch.setattr(bot_runtime, "CalendarClient", lambda **kwargs: client)
    monkeypatch.setattr(bot_runtime, "Synchronizer", lambda *args: synchronizer)
    monkeypatch.setattr(bot_runtime, "Bot", lambda *args, **kwargs: bot)
    monkeypatch.setattr(bot_runtime, "create_dispatcher", lambda *args: dispatcher)
    monkeypatch.setattr(bot_runtime, "UserNotifier", lambda *args: notifier)
    monkeypatch.setattr(bot_runtime, "configure_commands", AsyncMock())
    monkeypatch.setattr(
        bot_runtime, "install_monitor", lambda **kwargs: audit if audit_enabled else None
    )

    runner = asyncio.create_task(bot_runtime.run())
    try:
        await asyncio.wait_for(polling_returned.wait(), timeout=1)
        # Let cancellation and the background-task gather finish without a real
        # delay. Only the intentionally blocked user handler should keep run alive.
        for _ in range(10):
            await asyncio.sleep(0)
        assert "sync_stopped" in events and "notifier_stopped" in events
        assert not runner.done()
        audit.close.assert_not_awaited()
        bot.session.close.assert_not_awaited()
        assert dispatcher._handle_update_tasks

        release_handler.set()
        await asyncio.wait_for(runner, timeout=1)
        assert not dispatcher._handle_update_tasks
        assert events.index("polling_returned") < events.index("handler_finished")
        if audit_enabled:
            audit.close.assert_awaited_once()
            assert events.index("handler_finished") < events.index("audit_closed")
            assert events.index("audit_closed") < events.index("storage_closed")
        else:
            audit.close.assert_not_awaited()
        assert events.index("handler_finished") < events.index("bot_session_closed")
        assert events.index("bot_session_closed") < events.index("engine_disposed")
        dispatcher.storage.close.assert_awaited_once()
        bot.session.close.assert_awaited_once()
        engine.dispose.assert_awaited_once()
    finally:
        release_handler.set()
        await asyncio.gather(
            runner, *tuple(dispatcher._handle_update_tasks), return_exceptions=True
        )
