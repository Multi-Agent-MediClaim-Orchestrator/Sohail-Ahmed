"""Injectable clock so business timestamps are testable (DB ``now()`` is used only for created_at defaults)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta


class Clock:
    def now(self) -> datetime:
        return datetime.now(UTC)


class FakeClock(Clock):
    def __init__(self, start: datetime | None = None) -> None:
        self._now = start or datetime(2026, 10, 6, 10, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self._now

    def advance(self, **kw: float) -> datetime:
        self._now += timedelta(**kw)
        return self._now


clock: Clock = Clock()


def now() -> datetime:
    return clock.now()


def set_clock(c: Clock) -> None:
    global clock
    clock = c
