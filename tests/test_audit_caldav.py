import json
from base64 import b64encode
from types import SimpleNamespace

import caldav.davclient
import pytest

from bot.observability.caldav import install_caldav_audit
from CalendarClient.client import CalendarClient


class MemoryAudit:
    def __init__(self):
        self.secrets = {"123456789:bot-token-value"}
        self.records = []

    def register_secret(self, value):
        self.secrets.add(value)

    def sanitize(self, value):
        if isinstance(value, dict):
            return {key: self.sanitize(item) for key, item in value.items()}
        if isinstance(value, str):
            for secret in self.secrets:
                value = value.replace(secret, "[ENCRYPTED]")
        return value

    def emit(self, category, event, **fields):
        self.records.append({"category": category, "event": event, **fields})


def response(status=200, body=b"calendar data", headers=None, history=None, request=None):
    return SimpleNamespace(
        status_code=status,
        content=body,
        text=body.decode(),
        headers={"Content-Type": "text/plain", **(headers or {})},
        reason="test response",
        history=history or [],
        request=request,
    )


class FakeSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        result = next(self.responses)
        if isinstance(result, Exception):
            raise result
        if result.request is None:
            result.request = SimpleNamespace(
                method=method,
                url=url,
                body=kwargs.get("data"),
                headers={**kwargs.get("headers", {}), "Authorization": "Basic credential-token"},
            )
        return result

    def close(self):
        pass


def make_client(monkeypatch, *sessions):
    pending = iter(sessions)
    monkeypatch.setattr(caldav.davclient.requests, "Session", lambda **kwargs: next(pending))
    client = CalendarClient()
    audit = MemoryAudit()
    cleanup = install_caldav_audit(client, audit)
    return client, audit, cleanup


def test_request_and_raw_response_are_logged_without_credentials(monkeypatch):
    body = b"event password=super-secret token=123456789:bot-token-value"
    session = FakeSession([response(207, body, {"Set-Cookie": "session-cookie"})])
    client, audit, cleanup = make_client(monkeypatch, session)
    with client._client("person@yandex.ru", "super-secret") as dav:
        result = dav.request(
            "https://caldav.yandex.ru/cal/test/",
            "REPORT",
            "query super-secret",
            {"Depth": "1", "Authorization": "Basic exposed-basic-token"},
        )
    assert result.status == 207
    assert result._raw == body
    request, received = audit.records
    assert request["event"] == "http_request"
    assert request["category"] == "caldav"
    assert request["method"] == "REPORT"
    assert request["headers"]["Depth"] == "1"
    assert received["event"] == "http_response"
    assert received["request_id"] == request["request_id"]
    assert received["status"] == 207
    assert received["duration_ms"] >= 0
    assert received["body_bytes"] == len(body)
    assert "[ENCRYPTED]" in received["body"]
    serialized = json.dumps(audit.records)
    for secret in (
        "super-secret",
        "123456789:bot-token-value",
        "exposed-basic-token",
        "credential-token",
        "session-cookie",
    ):
        assert secret not in serialized
    cleanup()


def test_transport_exception_type_logged_without_its_message(monkeypatch):
    error = TimeoutError("super-secret https://example.test/?token=private-token")
    session = FakeSession([error])
    client, audit, cleanup = make_client(monkeypatch, session)
    with client._client("person@yandex.ru", "super-secret") as dav:
        with pytest.raises(TimeoutError) as caught:
            dav.request("https://caldav.yandex.ru/", "PROPFIND")
    assert caught.value is error
    assert [record["event"] for record in audit.records] == ["http_request", "http_error"]
    assert audit.records[-1]["error_type"] == "TimeoutError"
    assert "private-token" not in json.dumps(audit.records)
    cleanup()


def test_auth_retry_with_replaced_session_records_both_exchanges(monkeypatch):
    challenge = {"WWW-Authenticate": 'Basic realm="Calendar"'}
    first = FakeSession([response(401, b"authentication required", challenge)])
    second = FakeSession([response(200, b"events")])
    client, audit, cleanup = make_client(monkeypatch, first, second)
    with client._client("person@yandex.ru", "super-secret") as dav:
        result = dav.request("https://caldav.yandex.ru/", "PROPFIND")
    assert result.status == 200
    assert len(first.calls) == len(second.calls) == 1
    assert [r["status"] for r in audit.records if r["event"] == "http_response"] == [401, 200]
    assert len({r["request_id"] for r in audit.records}) == 2
    cleanup()


def test_redirect_history_includes_each_prepared_request(monkeypatch):
    initial = response(302, b"redirect", {"Location": "https://caldav.yandex.ru/new"})
    initial.request = SimpleNamespace(
        method="REPORT", url="https://caldav.yandex.ru/old", headers={}, body=b"query"
    )
    final = response(
        200,
        b"events",
        history=[initial],
        request=SimpleNamespace(
            method="GET",
            url="https://caldav.yandex.ru/new",
            headers={"Authorization": "Basic hidden-redirect-token"},
            body=None,
        ),
    )
    client, audit, cleanup = make_client(monkeypatch, FakeSession([final]))
    with client._client("person@yandex.ru", "super-secret") as dav:
        dav.request("https://caldav.yandex.ru/old", "REPORT", "query")
    responses = [r for r in audit.records if r["event"] == "http_response"]
    assert [r["status"] for r in responses] == [302, 200]
    assert [r["method"] for r in responses] == ["REPORT", "GET"]
    assert [r["url"] for r in responses] == [
        "https://caldav.yandex.ru/old", "https://caldav.yandex.ru/new"
    ]
    assert "hidden-redirect-token" not in json.dumps(audit.records)
    cleanup()


def test_uninstall_restores_factory_dav_and_session_instances(monkeypatch):
    session = FakeSession([response(), response()])
    client, audit, cleanup = make_client(monkeypatch, session)
    dav = client._client("person@yandex.ru", "super-secret")
    dav.request("https://caldav.yandex.ru/")
    assert "_client" in vars(client)
    assert "request" in vars(dav)
    assert "request" in vars(session)
    cleanup()
    cleanup()
    assert "_client" not in vars(client)
    assert "request" not in vars(dav)
    assert "request" not in vars(session)
    dav.request("https://caldav.yandex.ru/")
    assert len(audit.records) == 2


def test_capture_failure_does_not_break_successful_http_call(monkeypatch):
    client, audit, cleanup = make_client(monkeypatch, FakeSession([response()]))

    def broken_emit(*args, **kwargs):
        raise OSError("disk is full")

    audit.emit = broken_emit
    with client._client("person@yandex.ru", "super-secret") as dav:
        assert dav.request("https://caldav.yandex.ru/").status == 200
    cleanup()


def test_response_is_recorded_even_when_caldav_rejects_http_status(monkeypatch):
    client, audit, cleanup = make_client(monkeypatch, FakeSession([response(403, b"forbidden")]))
    with client._client("person@yandex.ru", "super-secret") as dav:
        with pytest.raises(caldav.lib.error.AuthorizationError):
            dav.request("https://caldav.yandex.ru/")
    assert audit.records[-1]["event"] == "http_response"
    assert audit.records[-1]["status"] == 403
    assert audit.records[-1]["body"] == "forbidden"
    cleanup()


def test_streaming_response_is_not_consumed_by_observer(monkeypatch):
    class StreamingResponse:
        status_code = 200
        history = []
        headers = {"Content-Type": "text/calendar"}
        request = None

        @property
        def content(self):
            raise AssertionError("observer consumed the response stream")

    streamed = StreamingResponse()
    session = FakeSession([response(), streamed])
    client, audit, cleanup = make_client(monkeypatch, session)
    with client._client("person@yandex.ru", "super-secret") as dav:
        dav.request("https://caldav.yandex.ru/")
        assert dav.session.request("GET", "https://caldav.yandex.ru/", stream=True) is streamed
    assert audit.records[-1]["event"] == "http_response"
    assert audit.records[-1]["body_omitted"] == "stream or unsupported body type"
    cleanup()


def test_basic_credentials_echoed_in_response_are_filtered(monkeypatch):
    basic = b64encode(b"person@yandex.ru:super-secret").decode()
    echoed = response(200, f"Authorization: Basic {basic}".encode(), {"X-Debug": basic})
    client, audit, cleanup = make_client(monkeypatch, FakeSession([echoed]))
    with client._client("person@yandex.ru", "super-secret") as dav:
        result = dav.request("https://caldav.yandex.ru/")
    assert basic.encode() in result._raw
    assert audit.records[-1]["headers"]["X-Debug"] == "[ENCRYPTED]"
    assert "[ENCRYPTED]" in audit.records[-1]["body"]
    assert basic not in json.dumps(audit.records)
    cleanup()
