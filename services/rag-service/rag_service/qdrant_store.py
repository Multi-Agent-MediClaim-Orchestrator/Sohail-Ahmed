"""Qdrant over its REST API (no qdrant-client dependency). Each logical collection is an *alias* over a physical
``<name>__v<n>`` collection so reindex can build a new one and swap atomically (04-05 §6.7)."""

from __future__ import annotations

import time
import uuid
from typing import Any

import httpx

from .store import Filter, Hit, Point
from .text import SparseVec

PAYLOAD_INDEXES = {
    "system": "keyword", "policy_product": "keyword", "policy_version": "keyword", "effective_from": "datetime", "effective_to": "datetime",
    "case_id": "keyword", "doc_id": "keyword", "chunk_type": "keyword", "doc_type": "keyword", "version": "integer",
}
COLLECTION_CONFIG = {
    "hnsw_config": {"m": 16, "ef_construct": 100},
    "optimizers_config": {"default_segment_number": 2},
    "quantization_config": {"scalar": {"type": "int8", "quantile": 0.99, "always_ram": True}},
    "on_disk_payload": True,
}


class QdrantError(RuntimeError):
    pass


def to_qdrant_filter(flt: Filter) -> dict[str, Any] | None:
    must: list[dict[str, Any]] = []
    for k, vals in flt.match.items():
        must.append({"key": k, "match": {"any": list(vals)}} if len(vals) != 1 else {"key": k, "match": {"value": vals[0]}})
    if flt.as_of is not None:
        d = flt.as_of.isoformat() + "T00:00:00Z"
        must.append({"should": [{"is_null": {"key": "effective_from"}}, {"key": "effective_from", "range": {"lte": d}}]})
        must.append({"should": [{"is_null": {"key": "effective_to"}}, {"key": "effective_to", "range": {"gt": d}}]})
    must_not = [{"key": k, "match": {"any": list(v)}} for k, v in flt.exclude.items()]
    out: dict[str, Any] = {}
    if must:
        out["must"] = must
    if must_not:
        out["must_not"] = must_not
    return out or None


class QdrantStore:
    def __init__(self, url: str, api_key: str = "", transport: httpx.BaseTransport | None = None, timeout: float = 30) -> None:
        headers = {"api-key": api_key} if api_key else {}
        self.http = httpx.Client(base_url=url.rstrip("/"), headers=headers, transport=transport, timeout=timeout)

    def _call(self, method: str, path: str, body: dict[str, Any] | None = None, *, ok: tuple[int, ...] = (200,)) -> dict[str, Any]:
        r = self.http.request(method, path, json=body)
        if r.status_code not in ok:
            raise QdrantError(f"{method} {path} -> {r.status_code}: {r.text[:300]}")
        return r.json() if r.content else {}

    # -- collections -------------------------------------------------------------------------
    def collection_exists(self, name: str) -> bool:
        return self.http.get(f"/collections/{name}").status_code == 200

    def create_physical(self, name: str, dim: int) -> str:
        body = {"vectors": {"dense": {"size": dim, "distance": "Cosine", "on_disk": False}}, "sparse_vectors": {"bm25": {"index": {"on_disk": False}, "modifier": "idf"}},
                **COLLECTION_CONFIG}
        self._call("PUT", f"/collections/{name}", body)
        for field, schema in PAYLOAD_INDEXES.items():
            self._call("PUT", f"/collections/{name}/index?wait=true", {"field_name": field, "field_schema": schema})
        return name

    def ensure_collection(self, name: str, dim: int) -> str:
        if self.collection_exists(name):
            return name
        phys = f"{name}__v1"
        self.create_physical(phys, dim)
        self.swap_alias(name, phys)
        return phys

    def swap_alias(self, alias: str, physical: str) -> str | None:
        old = self._alias_target(alias)
        actions: list[dict[str, Any]] = []
        if old:
            actions.append({"delete_alias": {"alias_name": alias}})
        actions.append({"create_alias": {"collection_name": physical, "alias_name": alias}})
        self._call("POST", "/collections/aliases", {"actions": actions})
        return old

    def _alias_target(self, alias: str) -> str | None:
        for a in self._call("GET", "/aliases").get("result", {}).get("aliases", []):
            if a.get("alias_name") == alias:
                return a.get("collection_name")
        return None

    def drop_physical(self, physical: str) -> None:
        self._call("DELETE", f"/collections/{physical}", ok=(200, 404))

    # -- points ------------------------------------------------------------------------------
    @staticmethod
    def _pid(i: str) -> str:
        return str(uuid.UUID(i)) if len(i) == 36 else i

    def upsert(self, collection: str, points: list[Point]) -> None:
        for i in range(0, len(points), 64):
            batch = []
            for p in points[i : i + 64]:
                vec: dict[str, Any] = {}
                if p.dense is not None:
                    vec["dense"] = p.dense
                if p.sparse is not None:
                    vec["bm25"] = {"indices": p.sparse.indices, "values": p.sparse.values}
                batch.append({"id": self._pid(p.id), "vector": vec, "payload": p.payload})
            self._call("PUT", f"/collections/{collection}/points?wait=true", {"points": batch})

    def delete_ids(self, collection: str, ids: list[str]) -> None:
        if ids:
            self._call("POST", f"/collections/{collection}/points/delete?wait=true", {"points": [self._pid(i) for i in ids]})

    def delete_where(self, collection: str, flt: Filter) -> int:
        f = to_qdrant_filter(flt)
        if f is None:
            raise QdrantError("refusing to delete with an empty filter")
        n = self.count(collection, flt)
        self._call("POST", f"/collections/{collection}/points/delete?wait=true", {"filter": f})
        return n

    def set_payload(self, collection: str, ids: list[str], payload: dict[str, Any]) -> None:
        if ids:
            self._call("POST", f"/collections/{collection}/points/payload?wait=true", {"payload": payload, "points": [self._pid(i) for i in ids]})

    def scroll(self, collection: str, flt: Filter | None = None, limit: int = 100000) -> list[tuple[str, dict[str, Any]]]:
        out: list[tuple[str, dict[str, Any]]] = []
        offset: Any = None
        while len(out) < limit:
            body: dict[str, Any] = {"limit": min(256, limit - len(out)), "with_payload": True, "with_vector": False}
            if flt is not None and (f := to_qdrant_filter(flt)):
                body["filter"] = f
            if offset is not None:
                body["offset"] = offset
            res = self._call("POST", f"/collections/{collection}/points/scroll", body)["result"]
            out += [(str(p["id"]), p.get("payload") or {}) for p in res["points"]]
            offset = res.get("next_page_offset")
            if offset is None:
                break
        return out

    def count(self, collection: str, flt: Filter | None = None) -> int:
        body: dict[str, Any] = {"exact": True}
        if flt is not None and (f := to_qdrant_filter(flt)):
            body["filter"] = f
        return int(self._call("POST", f"/collections/{collection}/points/count", body)["result"]["count"])

    def _query(self, collection: str, query: Any, using: str, flt: Filter, limit: int) -> list[Hit]:
        body: dict[str, Any] = {"query": query, "using": using, "limit": limit, "with_payload": True}
        if f := to_qdrant_filter(flt):
            body["filter"] = f
        res = self._call("POST", f"/collections/{collection}/points/query", body)["result"]
        return [Hit(str(p["id"]), float(p["score"]), p.get("payload") or {}) for p in res["points"]]

    def query_dense(self, collection: str, vector: list[float], flt: Filter, limit: int) -> list[Hit]:
        return self._query(collection, vector, "dense", flt, limit)

    def query_sparse(self, collection: str, vector: SparseVec, flt: Filter, limit: int) -> list[Hit]:
        if not vector.indices:
            return []
        return self._query(collection, {"indices": vector.indices, "values": vector.values}, "bm25", flt, limit)

    def stats(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for a in self._call("GET", "/aliases").get("result", {}).get("aliases", []):
            info = self._call("GET", f"/collections/{a['collection_name']}")["result"]
            out[a["alias_name"]] = {"physical": a["collection_name"], "points": info.get("points_count"), "status": info.get("status")}
        return out

    def wait_ready(self, seconds: float = 30) -> bool:
        end = time.time() + seconds
        while time.time() < end:
            try:
                if self.http.get("/readyz").status_code == 200:
                    return True
            except httpx.HTTPError:
                pass
            time.sleep(0.5)
        return False
