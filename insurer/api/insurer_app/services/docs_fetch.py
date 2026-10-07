"""Document ingest (03-02 §6.5): download presigned URL (SSRF-guarded, no redirects, size cap), verify sha256+size,
ClamAV scan, store in ``insurer-docs``; expired URL -> refresh-url callback (<= 3 attempts)."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

import httpx
from claim_contract import signing
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from .. import clock
from ..db import sessionmaker, transaction
from ..models.core import ClaimCase, ClaimDocument, NetworkHospital
from ..security.ssrf import UrlNotAllowed, assert_allowed_host, parse_allow_list
from ..security.urlcrypt import decrypt_url, encrypt_url
from ..settings import Settings, get_settings
from . import audit, events, jobs

EICAR = b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR-STANDARD-ANTIVIRUS-TEST-FILE!$H+H*"
MAX_REFRESH = 3


class ObjectStore(Protocol):
    async def put(self, bucket: str, key: str, data: bytes) -> None: ...
    async def get(self, bucket: str, key: str) -> bytes: ...
    async def presign(self, bucket: str, key: str, ttl: int) -> str: ...


class MemoryStore:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], bytes] = {}

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        self.objects[(bucket, key)] = data

    async def get(self, bucket: str, key: str) -> bytes:
        return self.objects[(bucket, key)]

    async def presign(self, bucket: str, key: str, ttl: int) -> str:
        return f"http://memory-store/{bucket}/{key}?ttl={ttl}"


class MinioStore:  # pragma: no cover - needs a live MinIO
    def __init__(self, s: Settings) -> None:
        from minio import Minio

        self.c = Minio(s.minio_endpoint, access_key=s.minio_access_key, secret_key=s.minio_secret_key, secure=False)
        self.bucket = s.minio_bucket

    async def put(self, bucket: str, key: str, data: bytes) -> None:
        import io

        if not await asyncio.to_thread(self.c.bucket_exists, bucket):
            await asyncio.to_thread(self.c.make_bucket, bucket)
        await asyncio.to_thread(self.c.put_object, bucket, key, io.BytesIO(data), len(data))

    async def get(self, bucket: str, key: str) -> bytes:
        r = await asyncio.to_thread(self.c.get_object, bucket, key)
        try:
            return r.read()  # type: ignore[no-any-return]
        finally:
            r.close()

    async def presign(self, bucket: str, key: str, ttl: int) -> str:
        from datetime import timedelta

        return await asyncio.to_thread(self.c.presigned_get_object, bucket, key, timedelta(seconds=ttl))


class Scanner(Protocol):
    async def scan(self, data: bytes) -> str: ...  # "clean" | "infected" | "skipped"


class NoScanner:
    async def scan(self, data: bytes) -> str:
        return "skipped"


class EicarScanner:
    """Test scanner: flags the EICAR signature like ClamAV would."""

    async def scan(self, data: bytes) -> str:
        return "infected" if EICAR in data else "clean"


class ClamdScanner:  # pragma: no cover - needs clamd
    def __init__(self, host: str, port: int = 3310) -> None:
        self.host, self.port = host, port

    async def scan(self, data: bytes) -> str:
        r, w = await asyncio.open_connection(self.host, self.port)
        w.write(b"zINSTREAM\0")
        for i in range(0, len(data), 8192):
            chunk = data[i : i + 8192]
            w.write(len(chunk).to_bytes(4, "big") + chunk)
        w.write(b"\0\0\0\0")
        await w.drain()
        resp = (await r.read(1024)).decode(errors="ignore")
        w.close()
        return "infected" if "FOUND" in resp else ("clean" if "OK" in resp else "skipped")


@dataclass
class Deps:
    store: ObjectStore
    scanner: Scanner
    http_factory: Any  # () -> httpx.AsyncClient (no redirects)


_deps: Deps | None = None


def set_deps(d: Deps) -> None:
    global _deps
    _deps = d


def get_deps() -> Deps:
    global _deps
    if _deps is None:
        s = get_settings()
        _deps = Deps(MemoryStore(), ClamdScanner(s.clamav_host, s.clamav_port) if s.clamav_host else NoScanner(),
                     lambda: httpx.AsyncClient(timeout=30, follow_redirects=False))
    return _deps


FAILURE_CODES = {"url_expired": "doc.url_expired", "hash_mismatch": "doc.hash_mismatch", "virus": "doc.virus_found", "fetch_failed": "doc.fetch_failed"}


async def _mark(session: AsyncSession, doc_id: UUID, status: str, code: str | None = None, **extra: Any) -> None:
    sets = ["fetch_status = :s", "fetch_error = :e", "fetch_attempts = fetch_attempts + 1"] + [f"{k} = :{k}" for k in extra]
    await session.execute(text(f"UPDATE core.claim_document SET {', '.join(sets)} WHERE id = :i"), {"s": status, "e": code, "i": doc_id, **extra})


async def fetch_document(doc_id: UUID, settings: Settings | None = None) -> str:
    """Returns final fetch_status. Safe to re-run (idempotent)."""
    s = settings or get_settings()
    deps = get_deps()
    sm = sessionmaker()
    async with sm() as session:
        doc = await session.get(ClaimDocument, doc_id)
        if doc is None:
            return "missing"
        if doc.fetch_status == "fetched":
            return "fetched"
        case_id, sha, size, url_enc = doc.case_id, doc.sha256, doc.size_bytes, doc.source_url_enc
    if not url_enc:
        async with transaction() as tx:
            await _mark(tx.session, doc_id, "failed", "doc.fetch_failed")
        return "failed"
    url = decrypt_url(url_enc)
    try:
        assert_allowed_host(url, parse_allow_list(s.allowed_doc_hosts))
    except UrlNotAllowed:
        async with transaction() as tx:
            await _mark(tx.session, doc_id, "failed", "doc.fetch_failed")
        return "failed"
    data = b""
    code: str | None = None
    try:
        async with deps.http_factory() as client:
            async with client.stream("GET", url) as r:
                if r.status_code in (403, 404):
                    code = "doc.url_expired"
                elif r.status_code >= 500:
                    raise httpx.HTTPStatusError("upstream", request=r.request, response=r)
                else:
                    r.raise_for_status()
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in r.aiter_bytes(65536):
                        total += len(chunk)
                        if total > s.max_doc_bytes:
                            code = "doc.fetch_failed"
                            break
                        chunks.append(chunk)
                    data = b"".join(chunks)
    except (httpx.TransportError, httpx.HTTPStatusError):
        code = "doc.fetch_failed"
    if code == "doc.url_expired":
        refreshed = await refresh_url(doc_id, case_id, s)
        if refreshed:
            return await fetch_document(doc_id, s)
    if code is None and (hashlib.sha256(data).hexdigest() != sha or len(data) != size):
        code = "doc.hash_mismatch"
    scan = "skipped"
    if code is None:
        scan = await deps.scanner.scan(data)
        if scan == "infected":
            code = "doc.virus_found"
    async with transaction() as tx:
        if code is None:
            key = f"{case_id}/{doc_id}"
            await deps.store.put(s.minio_bucket, key, data)
            await _mark(tx.session, doc_id, "fetched", None, object_key=key, scan_result=scan)
            await audit.append(tx.session, case_id, "doc.fetched", payload={"doc_id": str(doc_id), "sha256": sha, "scan": scan})
        else:
            status = "hash_mismatch" if code == "doc.hash_mismatch" else "failed"
            await _mark(tx.session, doc_id, status, code, scan_result=scan)
            await audit.append(tx.session, case_id, "doc.fetch_failed", payload={"doc_id": str(doc_id), "reason": code})
    return "fetched" if code is None else code


async def refresh_url(doc_id: UUID, case_id: UUID, s: Settings) -> bool:
    """Ask the hospital for a fresh presigned URL (contract 6.2.5). <= 3 attempts per document."""
    deps = get_deps()
    async with sessionmaker()() as session:
        doc = await session.get(ClaimDocument, doc_id)
        case = await session.get(ClaimCase, case_id)
        hosp = await session.get(NetworkHospital, case.hospital_id) if case else None
        if doc is None or case is None or hosp is None or doc.refresh_attempts >= MAX_REFRESH:
            return False
        claim_ref = case.hospital_claim_ref
        base = hosp.callback_base_url or s.hospital_callback_base
    async with transaction() as tx:
        await tx.session.execute(text("UPDATE core.claim_document SET refresh_attempts = refresh_attempts + 1 WHERE id = :i"), {"i": doc_id})
    path = f"/v1/insurer-callbacks/documents/{doc_id}/refresh-url"
    body = json.dumps({"claim_ref": claim_ref, "reason": "url_expired"}).encode()
    import uuid

    headers = signing.build_headers(s.ins_to_hosp_hmac_secret.encode(), s.ins_key_id, "POST", path, body, str(uuid.uuid4()), now=clock.now())
    try:
        async with deps.http_factory() as client:
            r = await client.post(base.rstrip("/") + path, content=body, headers=headers)
        if r.status_code != 200:
            return False
        new_url = r.json()["download_url"]
        assert_allowed_host(new_url, parse_allow_list(s.allowed_doc_hosts))
    except (httpx.HTTPError, KeyError, ValueError, UrlNotAllowed):
        return False
    async with transaction() as tx:
        await tx.session.execute(text("UPDATE core.claim_document SET source_url_enc = :u WHERE id = :i"), {"u": encrypt_url(new_url), "i": doc_id})
    return True


@jobs.register("fetch_documents")
async def fetch_documents(case_id: str) -> dict[str, int]:
    async with sessionmaker()() as session:
        ids = [r for r in (await session.execute(select(ClaimDocument.id).where(ClaimDocument.case_id == UUID(case_id), ClaimDocument.fetch_status == "pending"))).scalars()]
    results = await asyncio.gather(*(fetch_document(i) for i in ids))
    summary = {"fetched": sum(r == "fetched" for r in results), "failed": sum(r != "fetched" for r in results)}
    await events.publish("verification.documents_ready", case_id, summary, ["reviewer", "senior_reviewer", "admin"])
    return summary


async def docs_status(session: AsyncSession, case_id: UUID) -> dict[str, Any]:
    rows = (await session.execute(select(ClaimDocument.id, ClaimDocument.fetch_status, ClaimDocument.fetch_error).where(
        ClaimDocument.case_id == case_id, ClaimDocument.superseded_by.is_(None)))).all()
    pending = sum(1 for r in rows if r.fetch_status == "pending")
    failed = [{"doc_id": str(r.id), "status": r.fetch_status, "code": r.fetch_error} for r in rows if r.fetch_status in ("failed", "hash_mismatch")]
    return {"all_fetched": pending == 0 and not failed and len(rows) > 0, "terminal": pending == 0, "pending": pending, "failed": failed, "total": len(rows)}
