"""Vector-store abstraction. ``MemoryStore`` implements the same semantics as Qdrant (filters, temporal window, hybrid vectors,
aliases) so unit tests and small local demos run without a server; ``QdrantStore`` (qdrant_store.py) speaks the REST API."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

from .text import SparseVec


@dataclass
class Filter:
    """``match``: payload field -> allowed values (any-of). ``as_of``: temporal window on effective_from/effective_to."""

    match: dict[str, list[Any]] = field(default_factory=dict)
    as_of: date | None = None
    exclude: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class Point:
    id: str
    dense: list[float] | None
    sparse: SparseVec | None
    payload: dict[str, Any]


@dataclass
class Hit:
    id: str
    score: float
    payload: dict[str, Any]


class Store(Protocol):
    def ensure_collection(self, name: str, dim: int) -> str: ...
    def collection_exists(self, name: str) -> bool: ...
    def upsert(self, collection: str, points: list[Point]) -> None: ...
    def delete_ids(self, collection: str, ids: list[str]) -> None: ...
    def delete_where(self, collection: str, flt: Filter) -> int: ...
    def scroll(self, collection: str, flt: Filter | None = None, limit: int = 100000) -> list[tuple[str, dict[str, Any]]]: ...
    def query_dense(self, collection: str, vector: list[float], flt: Filter, limit: int) -> list[Hit]: ...
    def query_sparse(self, collection: str, vector: SparseVec, flt: Filter, limit: int) -> list[Hit]: ...
    def set_payload(self, collection: str, ids: list[str], payload: dict[str, Any]) -> None: ...
    def count(self, collection: str, flt: Filter | None = None) -> int: ...
    def create_physical(self, name: str, dim: int) -> str: ...
    def swap_alias(self, alias: str, physical: str) -> str | None: ...
    def drop_physical(self, physical: str) -> None: ...
    def stats(self) -> dict[str, dict[str, Any]]: ...


def _d(v: Any) -> date | None:
    if v in (None, ""):
        return None
    return v if isinstance(v, date) else date.fromisoformat(str(v)[:10])


def matches(payload: dict[str, Any], flt: Filter) -> bool:
    for k, allowed in flt.match.items():
        val = payload.get(k)
        vals = val if isinstance(val, list) else [val]
        if not any(v in allowed for v in vals):
            return False
    for k, banned in flt.exclude.items():
        val = payload.get(k)
        if val in banned:
            return False
    if flt.as_of is not None:
        start, end = _d(payload.get("effective_from")), _d(payload.get("effective_to"))
        if start is not None and start > flt.as_of:
            return False
        if end is not None and end <= flt.as_of:
            return False
    return True


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=False))  # vectors are L2-normalised


class MemoryStore:
    def __init__(self) -> None:
        self._data: dict[str, dict[str, Point]] = {}
        self._dims: dict[str, int] = {}
        self._alias: dict[str, str] = {}
        self._idf_n = 0

    # -- names -------------------------------------------------------------------------------
    def _phys(self, name: str) -> str:
        return self._alias.get(name, name)

    def collection_exists(self, name: str) -> bool:
        return self._phys(name) in self._data

    def ensure_collection(self, name: str, dim: int) -> str:
        if self.collection_exists(name):
            return self._phys(name)
        phys = f"{name}__v1"
        self.create_physical(phys, dim)
        self._alias[name] = phys
        return phys

    def create_physical(self, name: str, dim: int) -> str:
        self._data.setdefault(name, {})
        self._dims[name] = dim
        return name

    def swap_alias(self, alias: str, physical: str) -> str | None:
        old = self._alias.get(alias)
        self._alias[alias] = physical
        return old

    def drop_physical(self, physical: str) -> None:
        self._data.pop(physical, None)
        self._dims.pop(physical, None)

    # -- writes ------------------------------------------------------------------------------
    def upsert(self, collection: str, points: list[Point]) -> None:
        col = self._data[self._phys(collection)]
        dim = self._dims[self._phys(collection)]
        for p in points:
            if p.dense is not None and len(p.dense) != dim:
                raise ValueError(f"dimension mismatch: got {len(p.dense)}, collection is {dim}")
            col[p.id] = p

    def delete_ids(self, collection: str, ids: list[str]) -> None:
        col = self._data[self._phys(collection)]
        for i in ids:
            col.pop(i, None)

    def delete_where(self, collection: str, flt: Filter) -> int:
        col = self._data[self._phys(collection)]
        gone = [i for i, p in col.items() if matches(p.payload, flt)]
        for i in gone:
            del col[i]
        return len(gone)

    def set_payload(self, collection: str, ids: list[str], payload: dict[str, Any]) -> None:
        col = self._data[self._phys(collection)]
        for i in ids:
            if i in col:
                col[i].payload.update(payload)

    # -- reads -------------------------------------------------------------------------------
    def scroll(self, collection: str, flt: Filter | None = None, limit: int = 100000) -> list[tuple[str, dict[str, Any]]]:
        col = self._data[self._phys(collection)]
        out = [(i, p.payload) for i, p in col.items() if flt is None or matches(p.payload, flt)]
        return out[:limit]

    def count(self, collection: str, flt: Filter | None = None) -> int:
        return len(self.scroll(collection, flt))

    def query_dense(self, collection: str, vector: list[float], flt: Filter, limit: int) -> list[Hit]:
        col = self._data[self._phys(collection)]
        hits = [Hit(i, cosine(vector, p.dense), p.payload) for i, p in col.items() if p.dense is not None and matches(p.payload, flt)]
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:limit]

    def query_sparse(self, collection: str, vector: SparseVec, flt: Filter, limit: int) -> list[Hit]:
        col = self._data[self._phys(collection)]
        cands = [(i, p) for i, p in col.items() if p.sparse is not None and matches(p.payload, flt)]
        n = max(len(col), 1)
        df: dict[int, int] = {}
        for _, p in [(i, p) for i, p in col.items() if p.sparse is not None]:
            assert p.sparse is not None
            for idx in p.sparse.indices:
                df[idx] = df.get(idx, 0) + 1
        qset = dict(zip(vector.indices, vector.values, strict=False))
        hits = []
        for i, p in cands:
            s = 0.0
            assert p.sparse is not None
            for idx, val in zip(p.sparse.indices, p.sparse.values, strict=False):
                if idx in qset:
                    idf = math.log(1 + (n - df.get(idx, 0) + 0.5) / (df.get(idx, 0) + 0.5))  # Qdrant's idf modifier shape
                    s += qset[idx] * val * idf
            if s > 0:
                hits.append(Hit(i, s, p.payload))
        hits.sort(key=lambda h: (-h.score, h.id))
        return hits[:limit]

    def stats(self) -> dict[str, dict[str, Any]]:
        out = {}
        for alias, phys in self._alias.items():
            out[alias] = {"physical": phys, "points": len(self._data.get(phys, {})), "dim": self._dims.get(phys)}
        return out
