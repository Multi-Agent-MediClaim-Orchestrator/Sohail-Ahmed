"""SQLite persistence for the simulator (04-06 §3). The simulator never touches insurer-db."""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

DDL = [
    """CREATE TABLE IF NOT EXISTS sim_claim (claim_ref TEXT PRIMARY KEY, insurer_claim_no TEXT UNIQUE NOT NULL, scenario_id TEXT NOT NULL, state TEXT NOT NULL, round INTEGER NOT NULL DEFAULT 0,
       sequence INTEGER NOT NULL DEFAULT 0, step_cursor INTEGER NOT NULL DEFAULT 0, received_at TEXT NOT NULL, idem_key TEXT NOT NULL, payload_hash TEXT NOT NULL, payload TEXT NOT NULL,
       override_json TEXT, chaos_json TEXT, withdrawn INTEGER NOT NULL DEFAULT 0, closed INTEGER NOT NULL DEFAULT 0, paused INTEGER NOT NULL DEFAULT 0, profile TEXT, last_decision TEXT)""",
    """CREATE TABLE IF NOT EXISTS sim_event (id INTEGER PRIMARY KEY AUTOINCREMENT, claim_ref TEXT NOT NULL, step_id TEXT, kind TEXT NOT NULL, due_at TEXT NOT NULL, fired_at TEXT,
       attempts INTEGER NOT NULL DEFAULT 0, result TEXT, payload TEXT NOT NULL, seq_assigned INTEGER, depends_on_response INTEGER NOT NULL DEFAULT 0, cancelled INTEGER NOT NULL DEFAULT 0)""",
    "CREATE INDEX IF NOT EXISTS ix_sim_event_due ON sim_event (due_at) WHERE fired_at IS NULL",
    """CREATE TABLE IF NOT EXISTS sim_query (query_id TEXT PRIMARY KEY, claim_ref TEXT NOT NULL, round INTEGER NOT NULL, category TEXT NOT NULL, text TEXT NOT NULL, requested_doc_types TEXT,
       due_by TEXT, status TEXT NOT NULL, response_json TEXT, answered_at TEXT)""",
    "CREATE TABLE IF NOT EXISTS sim_idempotency (key_id TEXT, idem_key TEXT, request_hash TEXT, status INTEGER, body TEXT, headers TEXT, created_at TEXT, PRIMARY KEY (key_id, idem_key))",
    """CREATE TABLE IF NOT EXISTS sim_log (id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, direction TEXT, claim_ref TEXT, method TEXT, path TEXT, status INTEGER, sequence INTEGER,
       signature_ok INTEGER, idem_replay INTEGER, chaos TEXT, request_body TEXT, response_body TEXT, error TEXT)""",
    "CREATE TABLE IF NOT EXISTS scenario (id TEXT PRIMARY KEY, name TEXT, description TEXT, steps TEXT, match TEXT, source TEXT)",
    "CREATE TABLE IF NOT EXISTS sim_config (key TEXT PRIMARY KEY, value TEXT)",
    "CREATE TABLE IF NOT EXISTS sim_counter (name TEXT PRIMARY KEY, value INTEGER NOT NULL)",
]


def _j(v: Any) -> str | None:
    return None if v is None else json.dumps(v, separators=(",", ":"))


def _l(v: str | None) -> Any:
    return None if v is None else json.loads(v)


class Store:
    def __init__(self, url: str) -> None:
        self.url = url
        if url.startswith("sqlite+aiosqlite:///") and ":memory:" not in url:
            from pathlib import Path

            Path(url.split("///", 1)[1]).parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(url)

    async def init(self) -> None:
        async with self.engine.begin() as c:
            for stmt in DDL:
                await c.execute(text(stmt))

    async def close(self) -> None:
        await self.engine.dispose()

    async def reset(self, keep_scenarios: bool = True) -> None:
        async with self.engine.begin() as c:
            for t in ("sim_claim", "sim_event", "sim_query", "sim_idempotency", "sim_log", "sim_counter"):
                await c.execute(text(f"DELETE FROM {t}"))
            if not keep_scenarios:
                await c.execute(text("DELETE FROM scenario"))

    # ------------------------------------------------------------------ counters / config
    async def next_counter(self, name: str) -> int:
        async with self.engine.begin() as c:
            await c.execute(text("INSERT INTO sim_counter (name, value) VALUES (:n, 0) ON CONFLICT (name) DO NOTHING"), {"n": name})
            await c.execute(text("UPDATE sim_counter SET value = value + 1 WHERE name = :n"), {"n": name})
            return int((await c.execute(text("SELECT value FROM sim_counter WHERE name = :n"), {"n": name})).scalar_one())

    async def set_config(self, key: str, value: Any) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("INSERT INTO sim_config (key, value) VALUES (:k, :v) ON CONFLICT (key) DO UPDATE SET value = :v"), {"k": key, "v": json.dumps(value)})

    async def get_config(self, key: str, default: Any = None) -> Any:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT value FROM sim_config WHERE key = :k"), {"k": key})).scalar_one_or_none()
        return default if r is None else json.loads(r)

    # ------------------------------------------------------------------ claims
    async def add_claim(self, row: dict[str, Any]) -> None:
        row = {**row, "payload": _j(row["payload"]), "override_json": _j(row.get("override_json")), "chaos_json": _j(row.get("chaos_json")), "last_decision": _j(row.get("last_decision"))}
        cols = ["claim_ref", "insurer_claim_no", "scenario_id", "state", "round", "sequence", "step_cursor", "received_at", "idem_key", "payload_hash", "payload",
                "override_json", "chaos_json", "profile", "last_decision"]
        async with self.engine.begin() as c:
            await c.execute(text(f"INSERT INTO sim_claim ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})"), {k: row.get(k) for k in cols})

    async def get_claim(self, ref: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT * FROM sim_claim WHERE claim_ref = :r"), {"r": ref})).mappings().one_or_none()
        return self._claim(r) if r else None

    @staticmethod
    def _claim(r: Any) -> dict[str, Any]:
        d = dict(r)
        for k in ("payload", "override_json", "chaos_json", "last_decision"):
            d[k] = _l(d.get(k))
        for k in ("withdrawn", "closed", "paused"):
            d[k] = bool(d[k])
        return d

    async def list_claims(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT * FROM sim_claim ORDER BY insurer_claim_no"))).mappings().all()
        return [self._claim(r) for r in rows]

    async def update_claim(self, ref: str, **cols: Any) -> None:
        for k in ("payload", "override_json", "chaos_json", "last_decision"):
            if k in cols:
                cols[k] = _j(cols[k])
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        async with self.engine.begin() as c:
            await c.execute(text(f"UPDATE sim_claim SET {sets} WHERE claim_ref = :_r"), {**cols, "_r": ref})

    async def next_sequence(self, ref: str) -> int:
        async with self.engine.begin() as c:
            await c.execute(text("UPDATE sim_claim SET sequence = sequence + 1 WHERE claim_ref = :r"), {"r": ref})
            return int((await c.execute(text("SELECT sequence FROM sim_claim WHERE claim_ref = :r"), {"r": ref})).scalar_one())

    # ------------------------------------------------------------------ events
    async def add_event(self, ref: str, kind: str, due_at: datetime, payload: dict[str, Any], step_id: str | None = None) -> int:
        async with self.engine.begin() as c:
            r = await c.execute(text("INSERT INTO sim_event (claim_ref, step_id, kind, due_at, payload) VALUES (:r, :s, :k, :d, :p)"),
                                {"r": ref, "s": step_id, "k": kind, "d": due_at.isoformat(), "p": _j(payload)})
            return int(r.lastrowid or 0)

    @staticmethod
    def _event(r: Any) -> dict[str, Any]:
        d = dict(r)
        d["payload"] = _l(d["payload"])
        return d

    async def due_events(self, now: datetime) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text(
                "SELECT e.* FROM sim_event e JOIN sim_claim c ON c.claim_ref = e.claim_ref WHERE e.fired_at IS NULL AND e.cancelled = 0 AND c.paused = 0 AND e.due_at <= :n ORDER BY e.due_at, e.id"),
                {"n": now.isoformat()})).mappings().all()
        return [self._event(r) for r in rows]

    async def claim_event(self, event_id: int, now: datetime, result: str) -> bool:
        """Atomically mark an event fired; returns False if someone else already did (control-plane fire vs scheduler)."""
        async with self.engine.begin() as c:
            r = await c.execute(text("UPDATE sim_event SET fired_at = :n, result = :r WHERE id = :i AND fired_at IS NULL AND cancelled = 0"), {"n": now.isoformat(), "r": result, "i": event_id})
            return r.rowcount == 1

    async def update_event(self, event_id: int, **cols: Any) -> None:
        if "payload" in cols:
            cols["payload"] = _j(cols["payload"])
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        async with self.engine.begin() as c:
            await c.execute(text(f"UPDATE sim_event SET {sets} WHERE id = :_i"), {**cols, "_i": event_id})

    async def events_for(self, ref: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT * FROM sim_event WHERE claim_ref = :r ORDER BY id"), {"r": ref})).mappings().all()
        return [self._event(r) for r in rows]

    async def cancel_pending(self, ref: str, kinds: list[str] | None = None) -> int:
        async with self.engine.begin() as c:
            r = await c.execute(text("UPDATE sim_event SET cancelled = 1 WHERE claim_ref = :r AND fired_at IS NULL" + (" AND kind IN :k" if False else "")), {"r": ref})
            return r.rowcount

    # ------------------------------------------------------------------ queries
    async def add_query(self, row: dict[str, Any]) -> None:
        row = {**row, "requested_doc_types": _j(row.get("requested_doc_types", []))}
        async with self.engine.begin() as c:
            await c.execute(text("INSERT INTO sim_query (query_id, claim_ref, round, category, text, requested_doc_types, due_by, status) VALUES (:query_id, :claim_ref, :round, :category, :text, :requested_doc_types, :due_by, :status)"), row)

    async def get_query(self, qid: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT * FROM sim_query WHERE query_id = :q"), {"q": qid})).mappings().one_or_none()
        if r is None:
            return None
        d = dict(r)
        d["requested_doc_types"] = _l(d["requested_doc_types"]) or []
        d["response_json"] = _l(d["response_json"])
        return d

    async def queries_for(self, ref: str) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            ids = (await c.execute(text("SELECT query_id FROM sim_query WHERE claim_ref = :r ORDER BY round"), {"r": ref})).scalars().all()
        return [q for i in ids if (q := await self.get_query(i))]

    async def update_query(self, qid: str, **cols: Any) -> None:
        if "response_json" in cols:
            cols["response_json"] = _j(cols["response_json"])
        sets = ", ".join(f"{k} = :{k}" for k in cols)
        async with self.engine.begin() as c:
            await c.execute(text(f"UPDATE sim_query SET {sets} WHERE query_id = :_q"), {**cols, "_q": qid})

    # ------------------------------------------------------------------ log (ring buffer)
    async def log(self, cap: int = 20_000, **row: Any) -> None:
        cols = ["ts", "direction", "claim_ref", "method", "path", "status", "sequence", "signature_ok", "idem_replay", "chaos", "request_body", "response_body", "error"]
        row = {k: row.get(k) for k in cols} | {"request_body": _j(row.get("request_body")), "response_body": _j(row.get("response_body"))}
        async with self.engine.begin() as c:
            await c.execute(text(f"INSERT INTO sim_log ({', '.join(cols)}) VALUES ({', '.join(':' + k for k in cols)})"), row)
            await c.execute(text("DELETE FROM sim_log WHERE id <= (SELECT max(id) FROM sim_log) - :cap"), {"cap": cap})

    async def read_log(self, claim_ref: str | None = None, direction: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        where: list[str] = ["1=1"]
        p: dict[str, Any] = {"l": limit}
        if claim_ref:
            where.append("claim_ref = :r")
            p["r"] = claim_ref
        if direction:
            where.append("direction = :d")
            p["d"] = direction
        async with self.engine.connect() as c:
            rows = (await c.execute(text(f"SELECT * FROM sim_log WHERE {' AND '.join(where)} ORDER BY id DESC LIMIT :l"), p)).mappings().all()
        out = []
        for r in rows:
            d = dict(r)
            d["request_body"], d["response_body"] = _l(d["request_body"]), _l(d["response_body"])
            out.append(d)
        return out

    # ------------------------------------------------------------------ scenarios
    async def upsert_scenario(self, sid: str, name: str, description: str, steps: Any, match: Any, source: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("INSERT INTO scenario (id, name, description, steps, match, source) VALUES (:i,:n,:d,:s,:m,:src) ON CONFLICT (id) DO UPDATE SET name=:n, description=:d, steps=:s, match=:m, source=:src"),
                            {"i": sid, "n": name, "d": description, "s": _j(steps), "m": _j(match), "src": source})

    async def delete_scenario(self, sid: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("DELETE FROM scenario WHERE id = :i AND source = 'custom'"), {"i": sid})

    async def scenarios(self) -> list[dict[str, Any]]:
        async with self.engine.connect() as c:
            rows = (await c.execute(text("SELECT * FROM scenario ORDER BY id"))).mappings().all()
        return [{**dict(r), "steps": _l(r["steps"]), "match": _l(r["match"])} for r in rows]

    # ------------------------------------------------------------------ idempotency (sim_idempotency)
    async def idem_get(self, key_id: str, key: str) -> dict[str, Any] | None:
        async with self.engine.connect() as c:
            r = (await c.execute(text("SELECT * FROM sim_idempotency WHERE key_id = :k AND idem_key = :i"), {"k": key_id, "i": key})).mappings().one_or_none()
        return dict(r) if r else None

    async def idem_put(self, key_id: str, key: str, req_hash: str, status: int, body: str, headers: str) -> None:
        async with self.engine.begin() as c:
            await c.execute(text("INSERT OR IGNORE INTO sim_idempotency VALUES (:k, :i, :h, :s, :b, :hd, :t)"),
                            {"k": key_id, "i": key, "h": req_hash, "s": status, "b": body, "hd": headers, "t": datetime.now().isoformat()})
