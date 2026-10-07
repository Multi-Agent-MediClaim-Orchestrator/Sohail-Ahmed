"""Concurrent writers on one case must not deadlock (found by the browser tests: the UI uploads three files at once and
three requests died with 'deadlock detected': audit append and status transition took the case row and the audit head
in opposite orders)."""

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from tests.helpers import files, new_case, pdf_bytes

pytestmark = pytest.mark.integration
PDF = "application/pdf"


async def test_parallel_uploads_to_one_case_all_succeed(
    client: httpx.AsyncClient, tok: Any
) -> None:
    for round_ in range(3):  # deadlocks are timing dependent: several rounds on fresh cases
        case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])

        async def up(i: int, cid: str = case["id"]) -> httpx.Response:
            return await client.post(
                f"/v1/cases/{cid}/documents",
                headers={**tok("desk1"), "Idempotency-Key": str(uuid.uuid4())},
                files=files((f"d{i}.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
            )

        rs = await asyncio.gather(*(up(i) for i in range(6)))
        assert [r.status_code for r in rs] == [202] * 6, (
            round_,
            [(r.status_code, r.text[:120]) for r in rs if r.status_code != 202],
        )
        docs = (await client.get(f"/v1/cases/{case['id']}/documents", headers=tok("desk1"))).json()[
            "documents"
        ]
        assert len(docs) == 6


async def test_audit_chain_stays_valid_after_concurrent_writes(
    client: httpx.AsyncClient, tok: Any, settings: Any
) -> None:
    from tests.test_documents import sql

    case = await new_case(client, tok("officer1"), "UH-" + uuid.uuid4().hex[:8])
    await asyncio.gather(
        *(
            client.post(
                f"/v1/cases/{cid}/documents",
                headers={**tok("desk1"), "Idempotency-Key": str(uuid.uuid4())},
                files=files((f"x{i}.pdf", pdf_bytes(1, marker=uuid.uuid4().hex), PDF)),
            )
            for i in range(4)
        )
    )
    seqs = [
        r[0]
        for r in sql(
            settings, "SELECT seq FROM audit_event WHERE case_id=:c ORDER BY seq", c=case["id"]
        )
    ]
    assert seqs == list(range(1, len(seqs) + 1)) and len(seqs) >= 9  # gapless, in order


def test_no_plain_for_update_on_the_case_row() -> None:
    """FOR UPDATE on claim_case conflicts with the key-share locks child inserts take through foreign keys, and mixing it
    with FOR NO KEY UPDATE deadlocks. Every case-row lock must be FOR NO KEY UPDATE (no key column is ever changed)."""
    import re
    from pathlib import Path

    bad = []
    for f in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        for ln_no, ln in enumerate(f.read_text().splitlines(), 1):
            if re.search(r"claim_case", ln) and re.search(r"FOR UPDATE", ln):
                bad.append(f"{f.name}:{ln_no}")
    for f in (Path(__file__).resolve().parents[1] / "app").rglob("*.py"):
        t = f.read_text()
        for m in re.finditer(r'" FOR UPDATE(?: OF c)?"', t):
            if "claim_case" in t[max(0, m.start() - 600) : m.start()]:
                bad.append(f"{f.name}@{m.start()}")
    assert bad == [], bad
