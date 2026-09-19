"""Optional, removable audit extension. Only bot.bot imports this package."""


def install_audit(*, bot, dispatcher, engine, sessions, client, settings):
    from bot.observability.settings import AuditSettings

    audit_settings = AuditSettings()
    if not audit_settings.enabled:
        return None

    from bot.observability.caldav import install_caldav_audit
    from bot.observability.database import install_database_audit
    from bot.observability.service import AuditService
    from bot.observability.telegram import install_telegram_audit

    audit = AuditService(
        audit_settings,
        settings.encryption_key.get_secret_value(),
        secrets=(settings.bot_token.get_secret_value(),),
    )
    try:
        audit._cleanups.append(install_database_audit(engine, sessions, audit))
        audit._cleanups.append(install_caldav_audit(client, audit))
        audit._cleanups.append(install_telegram_audit(dispatcher, bot, audit))
    except Exception:
        for cleanup in reversed(audit._cleanups):
            cleanup()
        raise
    audit.start(bot)
    return audit
