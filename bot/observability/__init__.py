"""Removable lifecycle/error monitoring. Only bot.bot imports the installer."""


def install_monitor(*, bot, dispatcher, engine, sessions, client, settings):
    from bot.observability.settings import MonitorSettings

    monitor_settings = MonitorSettings()
    if not monitor_settings.enabled:
        return None

    from bot.observability.errors import install_error_monitor
    from bot.observability.service import MonitorService
    from bot.observability.snapshots import SnapshotTools
    from bot.observability.telegram import install_telegram_audit

    monitor = MonitorService(monitor_settings)
    snapshots = SnapshotTools(engine, sessions, monitor.directory / "snapshots")
    try:
        monitor._cleanups.append(install_error_monitor(client, bot, monitor))
        monitor._cleanups.append(
            install_telegram_audit(dispatcher, bot, monitor, sessions, snapshots)
        )
    except Exception:
        for cleanup in reversed(monitor._cleanups):
            cleanup()
        monitor._connection.close()
        raise
    monitor.start(bot)
    return monitor
