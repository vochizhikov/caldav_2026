from datetime import UTC, datetime, timedelta

import pytest

from CalendarClient.client import parse_snapshot


def ics(body):
    return f"BEGIN:VCALENDAR\nVERSION:2.0\nPRODID:-//Tests//RU\n{body}\nEND:VCALENDAR"


def event(extra="", start="DTSTART:20260918T090000Z", summary="Встреча"):
    return (
        f"BEGIN:VEVENT\nUID:meeting-1\nDTSTAMP:20260901T000000Z\n{start}\n"
        f"SUMMARY:{summary}\n{extra}\nEND:VEVENT"
    )


def test_series_exdate_and_moved_occurrence(now):
    raw = ics(
        event("RRULE:FREQ=DAILY;COUNT=3\nEXDATE:20260919T090000Z")
        + "\n"
        + event(
            "RECURRENCE-ID:20260920T090000Z", start="DTSTART:20260920T120000Z", summary="Перенос"
        )
    )
    snapshot = parse_snapshot(
        [raw], "Europe/Moscow", now - timedelta(days=1), now + timedelta(days=4)
    )
    assert len(snapshot.events) == 1
    assert [item.starts_at.hour for item in snapshot.occurrences] == [9, 12]
    assert len({item.key for item in snapshot.occurrences}) == 2
    moved = parse_snapshot(
        [raw.replace("DTSTART:20260920T120000Z", "DTSTART:20260920T130000Z")],
        "Europe/Moscow",
        now - timedelta(days=1),
        now + timedelta(days=4),
    )
    assert {item.key for item in moved.occurrences} == {item.key for item in snapshot.occurrences}


@pytest.mark.parametrize(
    ("start", "expected", "all_day"),
    [
        ("DTSTART;VALUE=DATE:20260918", datetime(2026, 9, 17, 21, tzinfo=UTC), True),
        ("DTSTART:20260918T120000", datetime(2026, 9, 18, 9, tzinfo=UTC), False),
        ("DTSTART;TZID=Europe/Berlin:20260918T110000", datetime(2026, 9, 18, 9, tzinfo=UTC), False),
    ],
)
def test_timezone_and_all_day(now, start, expected, all_day):
    snapshot = parse_snapshot(
        [ics(event(start=start))], "Europe/Moscow", now - timedelta(days=1), now + timedelta(days=1)
    )
    assert snapshot.occurrences[0].starts_at == expected
    assert snapshot.occurrences[0].all_day == all_day


def test_cancelled_series_has_no_reminders(now):
    snapshot = parse_snapshot(
        [ics(event("RRULE:FREQ=DAILY;COUNT=3\nSTATUS:CANCELLED"))],
        "Europe/Moscow",
        now - timedelta(days=1),
        now + timedelta(days=4),
    )
    assert snapshot.events[0].cancelled
    assert snapshot.occurrences == []


def test_metadata_updates_are_not_event_changes(now):
    raw = ics(event())

    def parse(value):
        return parse_snapshot([value], "Europe/Moscow", now, now + timedelta(days=1)).events[0]

    assert parse(raw).fingerprint == parse(raw.replace("20260901", "20260902")).fingerprint
    assert parse(raw).fingerprint != parse(raw.replace("Встреча", "Другая")).fingerprint


def test_malformed_snapshot_rejected_atomically(now):
    with pytest.raises(ValueError):
        parse_snapshot(
            [ics(event()), ics(event().replace("UID:meeting-1\n", ""))],
            "Europe/Moscow",
            now,
            now + timedelta(days=1),
        )


def test_attendee_order_is_not_a_change_and_input_is_not_mutated():
    from icalendar import Calendar

    from CalendarClient.client import semantic_fingerprint

    first = "ATTENDEE;CN=Alice;PARTSTAT=ACCEPTED:mailto:alice@example.test"
    second = "ATTENDEE;CN=Bob;PARTSTAT=TENTATIVE:mailto:bob@example.test"
    left = Calendar.from_ical(ics(event(f"{first}\n{second}"))).walk("VEVENT")
    right = Calendar.from_ical(ics(event(f"{second}\n{first}"))).walk("VEVENT")
    original = [c.to_ical() for c in left]
    assert semantic_fingerprint(left) == semantic_fingerprint(right)
    assert [c.to_ical() for c in left] == original


@pytest.mark.parametrize(
    "replacement",
    [
        "ATTENDEE;CN=Alice;PARTSTAT=DECLINED:mailto:alice@example.test",
        "ATTENDEE;CN=Alice;PARTSTAT=ACCEPTED:mailto:another@example.test",
        "ATTENDEE;CN=Alice;ROLE=CHAIR;PARTSTAT=ACCEPTED:mailto:alice@example.test",
        "",
    ],
)
def test_real_attendee_changes_are_still_detected(now, replacement):
    original = "ATTENDEE;CN=Alice;PARTSTAT=ACCEPTED:mailto:alice@example.test"
    before = parse_snapshot([ics(event(original))], "Europe/Moscow", now, now + timedelta(days=1))
    after = parse_snapshot([ics(event(replacement))], "Europe/Moscow", now, now + timedelta(days=1))
    assert before.events[0].fingerprint != after.events[0].fingerprint


def test_duplicate_attendee_addresses_are_sorted_by_parameters(now):
    one = "ATTENDEE;PARTSTAT=ACCEPTED:mailto:alice@example.test"
    two = "ATTENDEE;PARTSTAT=TENTATIVE:mailto:alice@example.test"
    before = parse_snapshot(
        [ics(event(f"{one}\n{two}"))], "Europe/Moscow", now, now + timedelta(days=1)
    )
    after = parse_snapshot(
        [ics(event(f"{two}\n{one}"))], "Europe/Moscow", now, now + timedelta(days=1)
    )
    assert before.events[0].fingerprint == after.events[0].fingerprint
