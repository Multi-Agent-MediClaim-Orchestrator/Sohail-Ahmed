"""Small SQLite metadata store (04-05 §3.4): collection_meta, ingest_job, retrieval_log. Independent of insurer-db by design."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DDL = [
    "CREATE TABLE IF NOT EXISTS collection_meta (collection TEXT PRIMARY KEY, embed_model TEXT NOT NULL, embed_dim INTEGER NOT NULL, created_at TEXT, last_reindex_at TEXT, chunk_tokens INTEGER, chunk_overlap INTEGER)",
    "CREATE TABLE IF NOT EXISTS ingest_job (id TEXT PRIMARY KEY, collection TEXT, bucket TEXT, key TEXT, status TEXT, chunks_added INTEGER DEFAULT 0, chunks_skipped INTEGER DEFAULT 0, tables_found INTEGER DEFAULT 0, warnings TEXT, error TEXT, created_at TEXT, finished_at TEXT)",
    "CREATE TABLE IF NOT EXISTS retrieval_log (retrieval_id TEXT PRIMARY KEY, caller TEXT, collection TEXT, query_hash TEXT, masked_query TEXT, top_ids TEXT, scores TEXT, created_at TEXT)",
    "CREATE TABLE IF NOT EXISTS doc_lock (doc_id TEXT PRIMARY KEY, job_id TEXT, locked_at TEXT)",
]


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class Meta:
    def __init__(self, path: str = ":memory:") -> None:
        if path != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        with self.lock:
            for s in DDL:
                self.conn.execute(s)
            self.conn.commit()

    def _x(self, sql: str, args: tuple[Any, ...] = ()) -> sqlite3.Cursor:
        with self.lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    # collection_meta
    def get_collection(self, name: str) -> dict[str, Any] | None:
        r = self._x("SELECT * FROM collection_meta WHERE collection=?", (name,)).fetchone()
        return dict(r) if r else None

    def set_collection(self, name: str, embed_model: str, dim: int, chunk_tokens: int, overlap: int, reindexed: bool = False) -> None:
        old = self.get_collection(name)
        self._x("INSERT INTO collection_meta VALUES (?,?,?,?,?,?,?) ON CONFLICT(collection) DO UPDATE SET embed_model=?, embed_dim=?, last_reindex_at=?",
                (name, embed_model, dim, old["created_at"] if old else now_iso(), now_iso() if reindexed else None, chunk_tokens, overlap, embed_model, dim, now_iso() if reindexed else (old or {}).get("last_reindex_at")))

    # jobs
    def new_job(self, collection: str, bucket: str | None, key: str | None) -> str:
        jid = "job_" + uuid.uuid4().hex[:20]
        self._x("INSERT INTO ingest_job (id, collection, bucket, key, status, created_at, warnings) VALUES (?,?,?,?,?,?,?)", (jid, collection, bucket, key, "queued", now_iso(), "[]"))
        return jid

    def update_job(self, jid: str, **cols: Any) -> None:
        if "warnings" in cols:
            cols["warnings"] = json.dumps(cols["warnings"])
        sets = ", ".join(f"{k}=?" for k in cols)
        self._x(f"UPDATE ingest_job SET {sets} WHERE id=?", (*cols.values(), jid))

    def get_job(self, jid: str) -> dict[str, Any] | None:
        r = self._x("SELECT * FROM ingest_job WHERE id=?", (jid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["warnings"] = json.loads(d["warnings"] or "[]")
        return d

    # per-doc advisory lock (04-05 §8 #17)
    def try_lock(self, doc_id: str, job_id: str) -> bool:
        with self.lock:
            cur = self.conn.execute("INSERT OR IGNORE INTO doc_lock VALUES (?,?,?)", (doc_id, job_id, now_iso()))
            self.conn.commit()
            return cur.rowcount == 1

    def unlock(self, doc_id: str) -> None:
        self._x("DELETE FROM doc_lock WHERE doc_id=?", (doc_id,))

    # retrieval log: hash + masked query only
    def log_retrieval(self, caller: str, collection: str, query: str, masked: str, top_ids: list[str], scores: list[float]) -> str:
        rid = "rtr_" + uuid.uuid4().hex[:24]
        self._x("INSERT INTO retrieval_log VALUES (?,?,?,?,?,?,?,?)", (rid, caller, collection, hashlib.sha256(query.encode()).hexdigest(), masked, json.dumps(top_ids), json.dumps(scores), now_iso()))
        return rid

    def get_retrieval(self, rid: str) -> dict[str, Any] | None:
        r = self._x("SELECT * FROM retrieval_log WHERE retrieval_id=?", (rid,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["top_ids"], d["scores"] = json.loads(d["top_ids"]), json.loads(d["scores"])
        return d
