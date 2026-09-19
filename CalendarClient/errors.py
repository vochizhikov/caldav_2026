import logging
import ssl

from caldav.lib.error import AuthorizationError, DAVError
from niquests.exceptions import ConnectionError, ProxyError, RequestException, SSLError, Timeout

logger = logging.getLogger(__name__)


class CalendarConnectionError(Exception):
    """Only static, sanitized messages may cross the CalDAV/UI boundary."""

    def __init__(self, message: str, *, code: str = "unknown"):
        super().__init__(message)
        self.code = code


def connection_error(exc: Exception, *, operation: str) -> CalendarConnectionError:
    if isinstance(exc, AuthorizationError):
        code = "authentication"
        message = (
            "Яндекс отклонил авторизацию. Проверьте полный адрес почты и пароль приложения "
            "типа «Календарь» именно для этого аккаунта. Новый пароль может начать работать "
            "через 2–3 часа после создания."
        )
    elif isinstance(exc, (SSLError, ssl.SSLError)):
        code = "tls"
        message = (
            "Не удалось проверить защищённое соединение с Яндексом. "
            "Проверьте дату и время компьютера, сертификаты и настройки прокси."
        )
    elif isinstance(exc, (Timeout, TimeoutError)):
        code = "timeout"
        message = "Яндекс не ответил вовремя. Повторите подключение позже."
    elif isinstance(exc, ProxyError):
        code = "proxy"
        message = "Не удалось подключиться через прокси. Проверьте настройки сети на сервере бота."
    elif isinstance(exc, (ConnectionError, RequestException, OSError)):
        code = "network"
        message = (
            "Нет соединения с Яндексом. Проверьте интернет, DNS и доступ к "
            "caldav.yandex.ru с компьютера, где работает бот."
        )
    elif isinstance(exc, DAVError):
        code = "caldav"
        message = (
            "Яндекс вернул ошибку CalDAV. Повторите попытку позже; "
            "если ошибка сохраняется, передайте код владельцу бота."
        )
    elif operation == "parse_events":
        code = "calendar_data"
        message = "Не удалось разобрать данные календаря. Последняя успешная копия сохранена."
    else:
        code = "internal"
        message = "Внутренняя ошибка CalDAV-клиента бота. Передайте код владельцу бота."
    # No str(exc), repr(exc), traceback or request/response body: any may contain secrets.
    logger.warning(
        "CalDAV failure operation=%s code=%s exception=%s", operation, code, type(exc).__name__
    )
    return CalendarConnectionError(f"⚠️ {message}\n🔎 Код: {code}.", code=code)
