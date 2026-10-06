from datetime import UTC, datetime

import pytest
from claim_contract import outbox
from claim_contract.errors import ContractError
from claim_contract.idempotency import MemoryStore, StoredResponse, request_hash, run_idempotent
from claim_contract.inbox import SeqResult, apply_sequence


async def test_idempotency_replay_conflict_and_5xx() -> None:
    s, calls = MemoryStore(), []

    async def ok() -> StoredResponse:
        calls.append(1)
        return StoredResponse("", 202, b"{}")

    h = request_hash("POST", "/x", b"a")
    r1, rep1 = await run_idempotent(s, "k", "i1", h, ok)
    r2, rep2 = await run_idempotent(s, "k", "i1", h, ok)
    assert (rep1, rep2, len(calls), r2.status) == (False, True, 1, 202)
    with pytest.raises(ContractError) as ei:
        await run_idempotent(s, "k", "i1", request_hash("POST", "/x", b"b"), ok)
    assert ei.value.code == "idempotency_conflict"

    state = {"n": 0}

    async def flaky() -> StoredResponse:
        state["n"] += 1
        return StoredResponse("", 500 if state["n"] == 1 else 202, b"")

    h2 = request_hash("POST", "/y", b"")
    await run_idempotent(s, "k", "i2", h2, flaky)
    r, replayed = await run_idempotent(s, "k", "i2", h2, flaky)
    assert (r.status, replayed) == (202, False)  # 5xx not stored


async def test_in_progress() -> None:
    s = MemoryStore()
    await s.acquire_lock("k:i")

    async def never() -> StoredResponse:
        raise AssertionError

    with pytest.raises(ContractError) as ei:
        await run_idempotent(s, "k", "i", "h", never)
    assert ei.value.code == "idempotency_in_progress"


def test_sequence() -> None:
    assert apply_sequence(3, 4).result is SeqResult.APPLY
    assert apply_sequence(5, 4).result is SeqResult.IGNORE
    assert apply_sequence(5, 5).result is SeqResult.IGNORE
    d = apply_sequence(3, 6)
    assert (d.result, d.new_last) == (SeqResult.APPLY_GAP, 6)


class MemOutbox:
    def __init__(self, msgs: list[outbox.OutboxMessage]) -> None:
        self.msgs = msgs

    async def fetch_due(self, limit: int) -> list[outbox.OutboxMessage]:
        return [m for m in self.msgs if m.status == "pending"][:limit]

    async def save(self, m: outbox.OutboxMessage) -> None:
        pass


def _msg() -> outbox.OutboxMessage:
    return outbox.OutboxMessage("1", "HC-2026-000001", "/v1/x", {"a": 1}, "idem-1")


async def test_outbox_delivers_signed_and_retries_to_dead() -> None:
    seen: list[dict[str, str]] = []

    async def good(m: str, p: str, b: bytes, h: dict[str, str]) -> outbox.HttpResult:
        seen.append(h)
        return outbox.HttpResult(202)

    m = _msg()
    st = await outbox.run_once(MemOutbox([m]), good, b"s", "hosp-001")
    assert st["delivered"] == 1 and m.status == "delivered"
    assert seen[0]["X-Idempotency-Key"] == "idem-1" and seen[0]["X-Signature"]

    async def boom(m: str, p: str, b: bytes, h: dict[str, str]) -> outbox.HttpResult:
        return outbox.HttpResult(500)

    m = _msg()
    store = MemOutbox([m])
    for _ in range(outbox.MAX_ATTEMPTS):
        await outbox.run_once(store, boom, b"s", "hosp-001", now=datetime.now(UTC))
    assert m.status == "dead" and m.attempts == 8


async def test_outbox_permanent_4xx_is_dead_and_stale_not_penalised() -> None:
    async def bad(m: str, p: str, b: bytes, h: dict[str, str]) -> outbox.HttpResult:
        return outbox.HttpResult(422, "validation_error")

    m = _msg()
    await outbox.run_once(MemOutbox([m]), bad, b"s", "k")
    assert m.status == "dead"

    async def stale(m: str, p: str, b: bytes, h: dict[str, str]) -> outbox.HttpResult:
        return outbox.HttpResult(401, "stale_request")

    m = _msg()
    await outbox.run_once(MemOutbox([m]), stale, b"s", "k")
    assert m.attempts == 0 and m.status == "pending"
