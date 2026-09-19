"""Removable, instance-scoped observation of CalDAV HTTP exchanges.

Observe the HTTP session rather than DAVClient's return value: DAVClient may
retry authentication recursively or raise for a response it already received.
"""

from base64 import b64encode
from collections.abc import Callable, Mapping
from functools import wraps
from threading import RLock
from time import perf_counter
from uuid import uuid4
from weakref import WeakKeyDictionary

from CalendarClient.client import CalendarClient

_MISSING = object()
_SENSITIVE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "cookie",
    "set-cookie",
    "x-api-key",
    "x-auth-token",
}


def _headers(headers) -> dict:
    if not isinstance(headers, Mapping):
        return {}
    return {
        str(key): "[REDACTED]" if str(key).lower() in _SENSITIVE_HEADERS else value
        for key, value in headers.items()
    }


def _body_fields(body) -> dict:
    # Never iterate an upload stream or stringify an arbitrary credential object.
    if body is None:
        return {"body": "", "body_bytes": 0}
    if isinstance(body, bytes):
        return {"body": body.decode("utf-8", errors="replace"), "body_bytes": len(body)}
    if isinstance(body, str):
        return {"body": body, "body_bytes": len(body.encode("utf-8"))}
    return {"body_omitted": "stream or unsupported body type"}


def _emit(audit, event: str, **fields) -> None:
    try:
        # Filtering happens before handing a record to any queue/file transport.
        audit.emit("caldav", event, **audit.sanitize(fields))
    except Exception:
        # Observability must not turn a successful calendar operation into a failure.
        pass


def install_caldav_audit(client: CalendarClient, audit) -> Callable[[], None]:
    """Attach to one CalendarClient; return an idempotent uninstall function.

    The audit's emit/sanitize/register_secret methods must be thread safe because
    CalendarClient runs its synchronous DAVClient sessions via asyncio.to_thread.
    Request/response bodies pass through the audit's bounded sanitizer, with their
    original byte length retained so truncation is visible in the archive.
    """
    lock = RLock()
    originals = WeakKeyDictionary()
    marker = object()
    active = True
    original_factory = client._client

    def replace(instance, name, wrapper):
        with lock:
            if not active:
                return
            if getattr(getattr(instance, name), "_caldav_audit_marker", None) is marker:
                return
            originals.setdefault(instance, {})[name] = vars(instance).get(name, _MISSING)
            wrapper._caldav_audit_marker = marker
            setattr(instance, name, wrapper)

    def observe_session(session):
        original_request = session.request
        if getattr(original_request, "_caldav_audit_marker", None) is marker:
            return

        @wraps(original_request)
        def request(method, url, **kwargs):
            if not active:
                return original_request(method, url, **kwargs)
            request_id = uuid4().hex
            started = perf_counter()
            _emit(
                audit,
                "http_request",
                request_id=request_id,
                method=method,
                url=str(url),
                headers=_headers(kwargs.get("headers")),
                **_body_fields(kwargs.get("data")),
            )
            try:
                response = original_request(method, url, **kwargs)
            except Exception as exc:
                _emit(
                    audit,
                    "http_error",
                    request_id=request_id,
                    method=method,
                    url=str(url),
                    error_type=type(exc).__name__,
                    duration_ms=round((perf_counter() - started) * 1000, 2),
                )
                raise
            duration_ms = round((perf_counter() - started) * 1000, 2)
            try:
                # Session.request follows redirects internally. Include every
                # returned hop and its actual prepared request in the audit.
                exchanges = [*response.history, response]
                for hop, result in enumerate(exchanges):
                    prepared = getattr(result, "request", None)
                    actual_method = getattr(prepared, "method", method)
                    actual_url = getattr(prepared, "url", url)
                    hop_id = f"{request_id}:{hop}" if hop else request_id
                    if hop:
                        _emit(
                            audit,
                            "http_request",
                            request_id=hop_id,
                            redirect_from=request_id,
                            method=actual_method,
                            url=str(actual_url),
                            headers=_headers(getattr(prepared, "headers", None)),
                            **_body_fields(getattr(prepared, "body", None)),
                        )
                    if kwargs.get("stream", False):
                        # Reading a streaming response here could consume it or
                        # change error timing. DAVClient itself uses buffered I/O.
                        body = vars(result).get("_content", _MISSING)
                    else:
                        body = result.content
                    _emit(
                        audit,
                        "http_response",
                        request_id=hop_id,
                        method=actual_method,
                        url=str(actual_url),
                        status=result.status_code,
                        headers=_headers(result.headers),
                        request_headers=_headers(getattr(prepared, "headers", None)),
                        duration_ms=duration_ms,
                        duration_scope="request including redirects",
                        **_body_fields(body),
                    )
            except Exception as exc:
                _emit(audit, "capture_error", request_id=request_id, error_type=type(exc).__name__)
            return response

        replace(session, "request", request)

    @wraps(original_factory)
    def factory(login, password):
        if not active:
            return original_factory(login, password)
        audit.register_secret(password)
        # A server/proxy can echo the encoded Authorization value into an
        # otherwise innocent response header or body. Treat that as a secret too.
        audit.register_secret(b64encode(f"{login}:{password}".encode()).decode())
        dav = original_factory(login, password)
        original_request = dav.request

        @wraps(original_request)
        def request(*args, **kwargs):
            # CalDAV can replace session during an authentication retry. Its
            # recursive self.request call will pass through here again.
            if active:
                observe_session(dav.session)
            return original_request(*args, **kwargs)

        replace(dav, "request", request)
        return dav

    replace(client, "_client", factory)

    def uninstall():
        nonlocal active
        with lock:
            active = False
            for instance, attributes in list(originals.items()):
                for name, original in attributes.items():
                    if getattr(getattr(instance, name), "_caldav_audit_marker", None) is not marker:
                        continue
                    if original is _MISSING:
                        delattr(instance, name)
                    else:
                        setattr(instance, name, original)
            originals.clear()

    return uninstall
