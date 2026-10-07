from __future__ import annotations

from uuid import UUID

from uuid_utils import uuid7 as _uuid7


def uuid7() -> UUID:
    return UUID(str(_uuid7()))


def new_id() -> UUID:
    return uuid7()
