"""Compare component fingerprints while ignoring edits that only affect the past."""

from datetime import datetime, timedelta


def changed_until(before: dict | None, after: dict | None) -> datetime | None:
    """Latest affected time; None also means an old snapshot needs a silent baseline."""
    if before is None or after is None:
        return None
    previous = before["components"]
    incoming = after["components"]
    affected = []
    for key in previous.keys() | incoming.keys():
        old = previous.get(key)
        new = incoming.get(key)
        if old and new and old["fingerprint"] == new["fingerprint"]:
            continue
        parts = [part for part in (old, new) if part]
        if any(part["series"] for part in parts):
            affected.extend((before["series_until"], after["series_until"]))
        else:
            affected.extend(part["until"] for part in parts)
            if old is None or new is None:
                affected.extend(
                    part["original_until"] for part in parts if "original_until" in part
                )
    return max((datetime.fromisoformat(value) for value in affected), default=None)


def instant_until(value: datetime) -> datetime:
    """A zero-duration event is still relevant exactly at its start."""
    return value + timedelta(microseconds=1)
