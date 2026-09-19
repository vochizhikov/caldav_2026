import asyncio
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import caldav
import recurring_ical_events
from icalendar import Calendar

from CalendarClient.changes import instant_until
from CalendarClient.errors import CalendarConnectionError as CalendarConnectionError
from CalendarClient.errors import connection_error
from CalendarClient.event_details import meeting_link, meeting_location
from CalendarClient.models import (
    CalendarSnapshot,
    RemoteCalendar,
    RemoteEvent,
    RemoteOccurrence,
    stable_key,
)

YANDEX_CALDAV_URL = "https://caldav.yandex.ru/"


def as_utc(value: date | datetime, timezone: ZoneInfo) -> datetime:
    if not isinstance(value, datetime):
        value = datetime.combine(value, time.min, timezone)
    elif value.tzinfo is None:
        value = value.replace(tzinfo=timezone)
    return value.astimezone(UTC)


def semantic_fingerprint(components) -> str:
    def normalize(component):
        for name in ("DTSTAMP", "LAST-MODIFIED", "CREATED", "SEQUENCE"):
            component.pop(name, None)
        for name, value in list(component.items()):
            if isinstance(value, list):
                # CalDAV may reorder repeated properties (especially ATTENDEE) on every read.
                # Parameters matter too: changing PARTSTAT/CN/ROLE is a real event change.
                component[name] = sorted(
                    value, key=lambda item: (item.to_ical(), item.params.to_ical())
                )
        for child in component.subcomponents:
            normalize(child)
        component.subcomponents.sort(key=lambda child: child.to_ical())

    canonical = []
    for component in components:
        component = deepcopy(component)
        normalize(component)
        canonical.append(component.to_ical().decode())
    return stable_key(*sorted(canonical))


def component_end(component, timezone: ZoneInfo) -> datetime:
    starts = component.decoded("DTSTART")
    if "DTEND" in component:
        return as_utc(component.decoded("DTEND"), timezone)
    duration = component.decoded(
        "DURATION", timedelta(days=1) if not isinstance(starts, datetime) else timedelta()
    )
    return as_utc(starts, timezone) + duration


def component_until(component, timezone: ZoneInfo) -> datetime:
    return max(
        component_end(component, timezone),
        instant_until(as_utc(component.decoded("DTSTART"), timezone)),
    )


def change_state(calendar, series, timezone, end, expanded) -> dict:
    """Persist hashes and relevance dates, not raw ICS or recurrence rules.

    Detached exceptions and RDATE/EXDATE entries have their own time boundaries.
    Changing an old exception must not look like changing every future recurrence.
    """
    master = next((c for c in series if "RECURRENCE-ID" not in c), series[0])
    recurring = any("RRULE" in c or "RDATE" in c for c in series)
    series_until = max(
        (component_until(c, timezone) for c in expanded),
        default=component_until(master, timezone),
    )
    if recurring:
        # A yearly/sparse series can have its next occurrence beyond the reminder window.
        single_series = Calendar()
        for component in calendar.subcomponents:
            if component.name == "VTIMEZONE":
                single_series.add_component(deepcopy(component))
        for component in series:
            single_series.add_component(deepcopy(component))
        following = next(
            recurring_ical_events.of(single_series).after(end.astimezone(timezone)), None
        )
        if following is not None:
            series_until = max(series_until, component_until(following, timezone))

    components = {}
    for component in series:
        recurrence = component.get("RECURRENCE-ID")
        key = as_utc(recurrence.dt, timezone).isoformat() if recurrence else "master"
        normalized = deepcopy(component)
        for name in ("EXDATE", "RDATE"):
            normalized.pop(name, None)
        affects_series = (recurring and recurrence is None) or bool(
            recurrence and recurrence.params.get("RANGE") == "THISANDFUTURE"
        )
        part = {
            "fingerprint": semantic_fingerprint([normalized]),
            "series": affects_series,
            "until": component_until(component, timezone).isoformat(),
        }
        if recurrence:
            # The original slot matters when adding/removing an exception. Subsequent
            # edits to an already-finished moved meeting must use its actual dates.
            duration = component_end(component, timezone) - as_utc(
                component.decoded("DTSTART"), timezone
            )
            part["original_until"] = (
                as_utc(recurrence.dt, timezone) + max(duration, timedelta(microseconds=1))
            ).isoformat()
        components[key] = part
        for name in ("EXDATE", "RDATE"):
            entries = component.get(name, [])
            if not isinstance(entries, list):
                entries = [entries]
            for entry in entries:
                for item in entry.dts:
                    value = item.dt
                    if isinstance(value, tuple):
                        starts, stop = value
                        until = (
                            as_utc(starts, timezone) + stop
                            if isinstance(stop, timedelta)
                            else as_utc(stop, timezone)
                        )
                    else:
                        starts = value
                        duration = component_end(component, timezone) - as_utc(
                            component.decoded("DTSTART"), timezone
                        )
                        until = (
                            as_utc(starts + timedelta(days=1), timezone)
                            if not isinstance(starts, datetime)
                            else as_utc(starts, timezone) + duration
                        )
                    until = max(until, instant_until(as_utc(starts, timezone)))
                    components[f"{key}:{name}:{as_utc(starts, timezone).isoformat()}"] = {
                        "fingerprint": stable_key(item.to_ical().decode()),
                        "series": False,
                        "until": until.isoformat(),
                    }
    return {"series_until": series_until.isoformat(), "components": components}


def parse_snapshot(
    resources: list[str],
    timezone_name: str,
    start: datetime,
    end: datetime,
) -> CalendarSnapshot:
    """Diff complete series; expand only the bounded reminder window locally.

    Parsing is atomic: one invalid resource rejects the entire calendar snapshot,
    otherwise a partial result could falsely report deletions.
    """
    timezone = ZoneInfo(timezone_name)
    events: dict[str, RemoteEvent] = {}
    occurrences: dict[str, RemoteOccurrence] = {}
    for raw in resources:
        calendar = Calendar.from_ical(raw)
        components = calendar.walk("VEVENT")
        if not components:
            raise ValueError("Event resource contains no VEVENT")
        by_uid: dict[str, list] = {}
        for component in components:
            uid = str(component.get("UID", ""))
            if not uid or "DTSTART" not in component:
                raise ValueError("Event has no UID or DTSTART")
            by_uid.setdefault(uid, []).append(component)
        for uid, series in by_uid.items():
            master = next((c for c in series if "RECURRENCE-ID" not in c), series[0])
            starts = master.decoded("DTSTART")
            key = stable_key(uid)
            if key in events:
                raise ValueError("Duplicate event UID in calendar")
            events[key] = RemoteEvent(
                key=key,
                uid=uid,
                fingerprint=semantic_fingerprint(series),
                summary=str(master.get("SUMMARY", "Без названия")),
                starts_at=as_utc(starts, timezone),
                all_day=not isinstance(starts, datetime),
                cancelled=str(master.get("STATUS", "")).upper() == "CANCELLED",
                location=meeting_location(master),
                meeting_url=meeting_link(master),
            )
        # Floating dates and date-only recurrences follow the user's chosen timezone.
        expanded = recurring_ical_events.of(calendar).between(
            start.astimezone(timezone), end.astimezone(timezone)
        )
        for uid, series in by_uid.items():
            key = stable_key(uid)
            events[key] = replace(
                events[key],
                change_state=change_state(
                    calendar,
                    series,
                    timezone,
                    end,
                    [component for component in expanded if str(component["UID"]) == uid],
                ),
            )
        for component in expanded:
            if str(component.get("STATUS", "")).upper() == "CANCELLED":
                continue
            uid = str(component["UID"])
            if events[stable_key(uid)].cancelled:
                continue
            starts = component.decoded("DTSTART")
            all_day = not isinstance(starts, datetime)
            starts_at = as_utc(starts, timezone)
            ends_at = component_end(component, timezone)
            recurrence = component.get("RECURRENCE-ID")
            recurrence_id = as_utc(recurrence.dt, timezone).isoformat() if recurrence else "single"
            key = stable_key(uid, recurrence_id)
            occurrences[key] = RemoteOccurrence(
                key=key,
                summary=str(component.get("SUMMARY", "Без названия")),
                location=meeting_location(component),
                starts_at=starts_at,
                ends_at=ends_at,
                all_day=all_day,
                meeting_url=meeting_link(component),
            )
    return CalendarSnapshot(list(events.values()), list(occurrences.values()))


class CalendarClient:
    """Short-lived synchronous CalDAV sessions run off the asyncio event loop."""

    def __init__(self, timeout: int = 20):
        self.timeout = timeout

    def _client(self, login: str, password: str):
        return caldav.DAVClient(
            url=YANDEX_CALDAV_URL,
            username=login,
            password=password,
            auth_type="basic",
            timeout=self.timeout,
        )

    async def calendars(self, login: str, password: str) -> list[RemoteCalendar]:
        return await asyncio.to_thread(self._calendars, login, password)

    def _calendars(self, login: str, password: str) -> list[RemoteCalendar]:
        try:
            with self._client(login, password) as client:
                return [
                    RemoteCalendar(str(item.url), str(item.name or "Календарь"))
                    for item in client.principal().calendars()
                ]
        except Exception as exc:
            raise connection_error(exc, operation="discover_calendars") from None

    async def snapshot(
        self,
        login: str,
        password: str,
        url: str,
        timezone: str,
        start: datetime,
        end: datetime,
    ) -> CalendarSnapshot:
        return await asyncio.to_thread(self._snapshot, login, password, url, timezone, start, end)

    def _snapshot(self, login, password, url, timezone, start, end) -> CalendarSnapshot:
        operation = "fetch_events"
        try:
            parsed = urlparse(url)
            if parsed.scheme != "https" or parsed.hostname != "caldav.yandex.ru":
                raise ValueError("Unexpected CalDAV origin")
            with self._client(login, password) as client:
                calendar = client.calendar(url=url)
                # Full collection, not a moving date range: moving/old events are not deletions.
                resources = [item.data for item in calendar.events()]
            operation = "parse_events"
            return parse_snapshot(resources, timezone, start, end)
        except Exception as exc:
            raise connection_error(exc, operation=operation) from None
