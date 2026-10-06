"""Transactional-outbox sender (doc 06 §5-6): signed HTTPS delivery to the insurer, ordered per claim, with
exponential backoff, dead-lettering and crash recovery. Exactly-once effect comes from the stable idempotency key."""

from __future__ import annotations

import asyncio
import json
import logging
import random
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from claim_contract import models as cm
from claim_contract import signing
from claim_contract.outbox import canonical_bytes
from sqlalchemy import text

from app.core.uow import UoW
from app.services import audit, transitions

log = logging.getLogger("app.outbox")
DELAYS = [0, 1, 4, 16, 60, 300, 300, 300]  # seconds before attempt n (index n-1); jitter +-20%
MAX_ATTEMPTS = 8
STUCK_AFTER_S = 60
SuccessHandler = Callable[[UoW, Any, Any, Any], Awaitable[None]]
SUCCESS_HANDLERS: dict[str, SuccessHandler] = {}


def on_success(kind: str) -> Callable[[SuccessHandler], SuccessHandler]:
    """Register a post-delivery handler for an outbox kind (docs 06/07)."""

    def deco(fn: SuccessHandler) -> SuccessHandler:
        SUCCESS_HANDLERS[kind] = fn
        return fn

    return deco


CLAIM_DUE = text("""
WITH heads AS (
  SELECT DISTINCT ON (o.case_id) o.id FROM outbox o WHERE o.status IN ('pending','sending','failed','dead')
    AND NOT (o.status = 'failed' AND EXISTS (  -- a rejected submission that was reopened and resubmitted is history
      SELECT 1 FROM outbox n WHERE n.case_id = o.case_id AND n.kind = o.kind AND n.sequence > o.sequence))
  ORDER BY o.case_id, o.sequence)
SELECT o.* FROM outbox o JOIN heads h ON h.id = o.id
WHERE o.status = 'pending' AND o.next_attempt_at <= now() AND (CAST(:case AS uuid) IS NULL OR o.case_id = CAST(:case AS uuid))
ORDER BY o.next_attempt_at LIMIT :n FOR UPDATE OF o SKIP LOCKED""")


def backoff(attempts_done: int) -> float:
    """Delay before the next attempt after `attempts_done` attempts."""
    base = DELAYS[min(attempts_done, len(DELAYS) - 1)]
    return base * random.uniform(0.8, 1.2)  # noqa: S311


class OutboxWorker:
    def __init__(self, app: Any) -> None:
        self.app = app
        self.wakeup = asyncio.Event()
        self.task: asyncio.Task[Any] | None = None
        self._stop = False

    # ---- lifecycle -------------------------------------------------------------------------------
    def start(self) -> None:
        self._stop = False
        self.task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop = True
        self.wakeup.set()
        if self.task:
            await asyncio.gather(self.task, return_exceptions=True)

    def wake(self) -> None:
        self.wakeup.set()

    async def _loop(self) -> None:
        poll = self.app.state.settings.outbox_poll_ms / 1000
        while not self._stop:
            try:
                stats = await self.run_once()
            except Exception:  # noqa: BLE001
                log.exception("outbox loop error")
                stats = {}
            if stats.get("claimed"):
                continue
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=poll)
            except TimeoutError:
                pass
            self.wakeup.clear()

    # ---- one pass ----------------------------------------------------------------------------------
    async def run_once(self, limit: int = 10, case_id: str | None = None) -> dict[str, int]:
        st = self.app.state
        async with (
            st.sessionmaker() as s
        ):  # janitor: rows stuck in 'sending' (worker crashed mid-send) go back to pending
            await s.execute(
                text(
                    "UPDATE outbox SET status='pending', attempts=GREATEST(attempts-1, 0), next_attempt_at=now() "
                    "WHERE status='sending' AND created_at < now() - make_interval(secs => :s) AND "
                    "COALESCE(sent_at, created_at) < now() - make_interval(secs => :s) AND next_attempt_at < now() - make_interval(secs => :s)"
                ),
                {"s": STUCK_AFTER_S},
            )
            await s.commit()
        async with st.sessionmaker() as s:
            rows = (await s.execute(CLAIM_DUE, {"n": limit, "case": case_id})).mappings().all()
            for r in rows:
                await s.execute(
                    text(
                        "UPDATE outbox SET status='sending', attempts=attempts+1, next_attempt_at=now() WHERE id=:i"
                    ),
                    {"i": r["id"]},
                )
            await s.commit()
        rows = [
            {**dict(r), "attempts": r["attempts"] + 1} for r in rows
        ]  # the DB value was incremented above
        results = await asyncio.gather(
            *(self.deliver(dict(r)) for r in rows), return_exceptions=True
        )
        for r, res in zip(rows, results, strict=True):
            if isinstance(res, Exception):
                log.error("delivery %s crashed: %r", r["id"], res)
        return {"claimed": len(rows), "errors": sum(isinstance(x, Exception) for x in results)}

    async def deliver(self, row: dict[str, Any]) -> None:
        st = self.app.state
        s = st.settings
        body = canonical_bytes(row["body"])
        ts = signing.now_ts()
        idem = str(row["idempotency_key"])
        headers = {
            "X-Contract-Version": "1.1",
            "X-Key-Id": s.hospital_key_id,
            "X-Timestamp": ts,
            "X-Idempotency-Key": idem,
            "X-Signature": signing.sign(
                s.hospital_to_insurer_secret.encode(), row["method"], row["path"], ts, idem, body
            ),
            "Content-Type": "application/json; charset=utf-8",
            "X-Request-Id": f"ob-{row['id']}"[:64],
        }
        jid = (row["body"] or {}).get("journey_id") if isinstance(row["body"], dict) else None
        if jid:
            headers["X-Journey-Id"] = str(jid)
        try:
            r = await st.insurer_client.request(
                row["method"], row["path"], content=body, headers=headers, timeout=20
            )
        except (httpx.TimeoutException, httpx.TransportError) as e:
            await self._retry(row, f"{type(e).__name__}", None)
            return
        code = None
        retry_after = None
        try:
            problem = (
                r.json() if r.content and "json" in r.headers.get("content-type", "") else None
            )
            code = problem.get("code") if isinstance(problem, dict) else None
        except ValueError:
            problem = None
        if r.headers.get("retry-after", "").replace(".", "").isdigit():
            retry_after = float(r.headers["retry-after"])
        if 200 <= r.status_code < 300:
            await self._success(row, r.status_code, problem)
        elif r.status_code == 401 and code == "stale_request":
            await self._requeue_now(row, "stale_request (clock skew?)")
        elif (
            r.status_code in (429, 503) or r.status_code >= 500 or code == "idempotency_in_progress"
        ):
            await self._retry(row, f"{r.status_code} {code or ''}".strip(), retry_after)
        else:
            await self._fail(row, r.status_code, problem)

    # ---- outcomes ---------------------------------------------------------------------------------------
    async def _success(self, row: dict[str, Any], status: int, body: Any) -> None:
        st = self.app.state
        async with st.sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE outbox SET status='sent', sent_at=now(), response_status=:r, response_body=CAST(:b AS jsonb), "
                    "last_error=NULL WHERE id=:i"
                ),
                {"r": status, "b": json.dumps(body) if body is not None else None, "i": row["id"]},
            )
            uow = UoW(s)
            handler = SUCCESS_HANDLERS.get(row["kind"])
            if handler is not None:
                await handler(uow, row, body, self.app)
            await audit.append(
                s,
                row["case_id"],
                "submission.sent",
                {"kind": row["kind"], "outbox_id": str(row["id"]), "attempts": row["attempts"]},
                actor_id="outbox-worker",
            )
            await uow.commit()
        await st.hub.publish("submission.sent", str(row["case_id"]), {"kind": row["kind"]})

    async def _retry(self, row: dict[str, Any], err: str, retry_after: float | None) -> None:
        attempts = row["attempts"]
        if attempts >= MAX_ATTEMPTS:
            await self._dead(row, err)
            return
        delay = retry_after if retry_after is not None else backoff(attempts)
        async with self.app.state.sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE outbox SET status='pending', next_attempt_at=:t, last_error=:e WHERE id=:i"
                ),
                {
                    "t": datetime.now(UTC) + timedelta(seconds=delay),
                    "e": json.dumps({"message": err, "attempt": attempts}),
                    "i": row["id"],
                },
            )
            await s.commit()

    async def _requeue_now(self, row: dict[str, Any], err: str) -> None:
        log.warning("outbox %s: %s", row["id"], err)
        async with self.app.state.sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE outbox SET status='pending', attempts=GREATEST(attempts-1,0), next_attempt_at=now(), last_error=:e WHERE id=:i"
                ),
                {"e": json.dumps({"message": err}), "i": row["id"]},
            )
            await s.commit()

    async def _fail(self, row: dict[str, Any], status: int, problem: Any) -> None:
        st = self.app.state
        async with st.sessionmaker() as s:
            await s.execute(
                text(
                    "UPDATE outbox SET status='failed', response_status=:r, response_body=CAST(:b AS jsonb), last_error=:e WHERE id=:i"
                ),
                {
                    "r": status,
                    "b": json.dumps(problem) if problem is not None else None,
                    "e": json.dumps(
                        {"status": status, "problem": problem, "at": datetime.now(UTC).isoformat()}
                    ),
                    "i": row["id"],
                },
            )
            await audit.append(
                s,
                row["case_id"],
                "submission.failed",
                {
                    "kind": row["kind"],
                    "status": status,
                    "code": (problem or {}).get("code") if isinstance(problem, dict) else None,
                },
                actor_id="outbox-worker",
            )
            await s.commit()
        await st.hub.publish(
            "submission.failed", str(row["case_id"]), {"kind": row["kind"], "status": status}
        )

    async def _dead(self, row: dict[str, Any], err: str) -> None:
        st = self.app.state
        async with st.sessionmaker() as s:
            await s.execute(
                text("UPDATE outbox SET status='dead', last_error=:e WHERE id=:i"),
                {"e": json.dumps({"message": err, "attempts": row["attempts"]}), "i": row["id"]},
            )
            await audit.append(
                s,
                row["case_id"],
                "submission.dead",
                {"kind": row["kind"], "attempts": row["attempts"]},
                actor_id="outbox-worker",
            )
            await s.commit()
        await st.hub.publish("submission.dead", str(row["case_id"]), {"kind": row["kind"]})


# ---------------------------------------------------------------------------------- success handlers
@on_success("claim.submit")
async def _submit_ok(uow: UoW, row: Any, body: Any, app: Any) -> None:
    ack = cm.Acknowledgement.model_validate(body)
    await uow.session.execute(
        text(
            "UPDATE claim_case SET insurer_claim_no=COALESCE(insurer_claim_no, :n), acknowledged_at=COALESCE(acknowledged_at, now()), "
            "insurer_status=COALESCE(insurer_status, :s) WHERE id=:i"
        ),
        {"n": ack.insurer_claim_no, "s": ack.status.value, "i": row["case_id"]},
    )
    status = (
        await uow.session.execute(
            text("SELECT status::text FROM claim_case WHERE id=:i"), {"i": row["case_id"]}
        )
    ).scalar()
    if status == "submitted":  # a fast callback may already have moved the case further
        await transitions.transition(
            uow,
            row["case_id"],
            "acknowledged",
            None,
            reason="insurer acknowledged",
            hub=app.state.hub,
        )
    await audit.append(
        uow.session,
        row["case_id"],
        "ack.received",
        {"insurer_claim_no": ack.insurer_claim_no, "sequence": ack.sequence},
        actor_type="external",
        actor_id="insurer",
    )


@on_success("claim.withdraw")
async def _withdraw_ok(uow: UoW, row: Any, body: Any, app: Any) -> None:
    status = (
        await uow.session.execute(
            text("SELECT status::text FROM claim_case WHERE id=:i"), {"i": row["case_id"]}
        )
    ).scalar()
    if status in ("submitted", "acknowledged", "under_query"):
        await transitions.transition(
            uow, row["case_id"], "closed", None, reason="withdrawn", hub=app.state.hub
        )
