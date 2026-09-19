from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from caldav.lib.error import AuthorizationError, PropfindError
from niquests.exceptions import ConnectionError, ProxyError, SSLError, Timeout

from CalendarClient import CalendarClient, CalendarConnectionError

SYNTHETIC_SECRET = "synthetic-test-secret"


@pytest.mark.parametrize(
    ("failure", "code"),
    [
        (AuthorizationError(reason=SYNTHETIC_SECRET), "authentication"),
        (Timeout(SYNTHETIC_SECRET), "timeout"),
        (TimeoutError(SYNTHETIC_SECRET), "timeout"),
        (SSLError(SYNTHETIC_SECRET), "tls"),
        (ProxyError(SYNTHETIC_SECRET), "proxy"),
        (ConnectionError(SYNTHETIC_SECRET), "network"),
        (PropfindError(reason=SYNTHETIC_SECRET), "caldav"),
        (AttributeError(SYNTHETIC_SECRET), "internal"),
    ],
)
async def test_connection_failure_is_classified_without_exposing_secrets(
    monkeypatch,
    caplog,
    failure,
    code,
):
    client = CalendarClient()

    def fail(*args):
        raise failure

    monkeypatch.setattr(client, "_client", fail)
    with pytest.raises(CalendarConnectionError) as caught:
        await client.calendars("test@yandex.ru", SYNTHETIC_SECRET)
    assert caught.value.code == code
    assert SYNTHETIC_SECRET not in str(caught.value)
    assert SYNTHETIC_SECRET not in caplog.text
    assert "test@yandex.ru" not in caplog.text
    assert f"code={code}" in caplog.text
    assert all(record.exc_info is None for record in caplog.records)


def test_client_uses_explicit_basic_auth_and_verifies_tls():
    # Constructing a DAVClient for an explicit URL does not make a network request.
    with CalendarClient(timeout=12)._client("test@yandex.ru", SYNTHETIC_SECRET) as client:
        assert client.auth_type == "basic"
        assert client.ssl_verify_cert is True
        assert client.timeout == 12


async def test_discovery_maps_calendars(monkeypatch):
    client = CalendarClient()
    dav = MagicMock()
    dav.__enter__.return_value = dav
    dav.principal.return_value.calendars.return_value = [
        SimpleNamespace(url="https://caldav.yandex.ru/test/", name="Работа")
    ]
    monkeypatch.setattr(client, "_client", lambda *_: dav)
    calendars = await client.calendars("test@yandex.ru", SYNTHETIC_SECRET)
    assert calendars[0].name == "Работа"
    dav.__exit__.assert_called_once()


async def test_invalid_calendar_is_distinct_from_authentication(monkeypatch, now, caplog):
    client = CalendarClient()
    dav = MagicMock()
    dav.__enter__.return_value = dav
    dav.calendar.return_value.events.return_value = [SimpleNamespace(data=SYNTHETIC_SECRET)]
    monkeypatch.setattr(client, "_client", lambda *_: dav)
    with pytest.raises(CalendarConnectionError) as caught:
        await client.snapshot(
            "test@yandex.ru",
            SYNTHETIC_SECRET,
            "https://caldav.yandex.ru/test/",
            "Europe/Moscow",
            now,
            now,
        )
    assert caught.value.code == "calendar_data"
    assert SYNTHETIC_SECRET not in caplog.text
