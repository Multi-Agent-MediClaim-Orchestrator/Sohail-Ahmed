"""Clock abstraction (04-06 §9.4): wall clock in production, ``FakeClock`` in tests so whole scenarios run in milliseconds."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC).replace(microsecond=0)


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float = 0, **kw: float) -> datetime:
        self._now += timedelta(seconds=seconds, **kw)
        return self._now
