from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal


@dataclass(frozen=True)
class Principal:
    kind: Literal["human", "service"]
    sub: str
    roles: frozenset[str]
    id: uuid.UUID | None = None  # app_user id (humans only)
    email: str | None = None
    name: str | None = None
    client_id: str | None = None  # service accounts
    role: str | None = None  # primary human role

    @property
    def actor_id(self) -> str:
        return str(self.id) if self.id else (self.client_id or self.sub)

    @property
    def actor_type(self) -> str:
        return "human" if self.kind == "human" else "system"
