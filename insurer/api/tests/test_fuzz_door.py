"""SEC-T5 (insurer door): hostile/malformed submissions never cause a 5xx, a hang, or partial state (always a clean 4xx problem+json)."""

from __future__ import annotations

import copy
import json
import random
import uuid

import pytest
from claim_contract.samples import make_submission
from ins_helpers import unique_member
from sqlalchemy import text

pytestmark = pytest.mark.integration

SEED = 1337


def mutate(base: dict, rng: random.Random) -> bytes:
    d = copy.deepcopy(base)
    kind = rng.randrange(14)
    if kind == 0:
        d.pop(rng.choice(list(d)))
    elif kind == 1:
        d["bill_lines"] = []
    elif kind == 2:
        d["bill_lines"][0]["amount"]["amount"] = rng.choice(["-1.00", "NaN", "1e400", "abc", "", "1.234", "9" * 40])
    elif kind == 3:
        d["patient"]["dob"] = rng.choice(["2999-01-01", "not-a-date", "1900-13-45", ""])
    elif kind == 4:
        d["claim_ref"] = rng.choice(["", "HC-1", "x" * 5000, "HC-2026-000001; DROP TABLE core.claim_case;--", "../../etc/passwd"])
    elif kind == 5:
        d["documents"][0]["download_url"] = rng.choice(["file:///etc/passwd", "http://169.254.169.254/latest", "gopher://x", "javascript:alert(1)", "http://[::1]:5432/"])
    elif kind == 6:
        d["documents"] = d["documents"] * 200
    elif kind == 7:
        d["bill_lines"] = d["bill_lines"] * 3000
    elif kind == 8:
        d["hospital_notes"] = "A" * rng.choice([2001, 100000])
    elif kind == 9:
        d["extra_field"] = {"nested": [1, 2, {"deep": "x"}]}
    elif kind == 10:
        d["admission"]["diagnosis_codes"] = rng.choice([[], ["'; --"], ["K80.2"] * 500, [None]])
    elif kind == 11:
        d["totals"]["claimed"] = {"amount": "1.00", "currency": rng.choice(["USD", "", "inr"])}
    elif kind == 12:
        return rng.choice([b"", b"null", b"[]", b"{", b"\xff\xfe\x00", b'{"a":' * 5000, json.dumps(d).encode()[: rng.randrange(1, 400)]])
    else:
        d["patient"]["full_name"] = rng.choice(["", "\x00\x00", "A" * 10000, "Robert'); DROP TABLE students;--", "😀" * 200, "<script>alert(1)</script>"])
    return json.dumps(d).encode()


async def test_two_hundred_hostile_submissions_get_clean_4xx_and_leave_no_state(env):
    rng = random.Random(SEED)
    base = make_submission(claim_ref=f"HC-2026-{uuid.uuid4().int % 900000 + 100000}", doc_base=(uuid.uuid4().int % 0xFFFFFFFFF) << 8, **unique_member())
    async with env.sm() as s:
        before = (await s.execute(text("SELECT count(*) FROM core.claim_case"))).scalar_one()
    codes: dict[int, int] = {}
    for _ in range(200):
        raw = mutate(base, rng)
        r = await env.sim.request("POST", "/v1/hospital-api/claims", raw=raw)
        codes[r.status_code] = codes.get(r.status_code, 0) + 1
        assert r.status_code < 500, (r.status_code, r.text[:200], raw[:120])
        if r.status_code >= 400:
            assert r.headers["content-type"].startswith("application/problem+json"), (r.status_code, r.headers["content-type"])
            assert r.json()["code"]
    async with env.sm() as s:
        after = (await s.execute(text("SELECT count(*) FROM core.claim_case"))).scalar_one()
    assert after - before <= 20  # a few mutations (e.g. extra field, unknown notes) may be legitimately accepted; the rest were refused
    assert set(codes) <= {202, 400, 401, 403, 409, 413, 415, 422, 429}, codes


async def test_oversized_body_is_refused_before_parsing(env):
    r = await env.sim.request("POST", "/v1/hospital-api/claims", raw=b"{" + b" " * (3 * 1024 * 1024) + b"}")
    assert r.status_code == 413 and r.json()["code"] == "payload_too_large"
