import hashlib
from dataclasses import dataclass, field
from datetime import datetime


def stable_key(*values: str) -> str:
    return hashlib.sha256("\0".join(values).encode()).hexdigest()


@dataclass(frozen=True)
class RemoteCalendar:
    url: str
    name: str

    @property
    def key(self) -> str:
        return stable_key(self.url)


@dataclass(frozen=True)
class RemoteEvent:
    key: str
    uid: str
    fingerprint: str
    summary: str
    starts_at: datetime
    all_day: bool
    cancelled: bool = False
    change_state: dict | None = None
    location: str = ""
    meeting_url: str = ""


@dataclass(frozen=True)
class RemoteOccurrence:
    key: str
    summary: str
    location: str
    starts_at: datetime
    ends_at: datetime
    all_day: bool
    meeting_url: str = ""


@dataclass(frozen=True)
class CalendarSnapshot:
    events: list[RemoteEvent] = field(default_factory=list)
    occurrences: list[RemoteOccurrence] = field(default_factory=list)
