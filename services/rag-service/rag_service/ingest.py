"""Idempotent, versioned ingestion (04-05 §6.4, §6.7, §6.8) and loaders."""

from __future__ import annotations

import csv
import io
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from html.parser import HTMLParser
from typing import Any

from .chunking import Chunk, chunk_markdown, default_caption
from .db import Meta, now_iso
from .store import Filter, Point, Store
from .text import pii_hits, sparse_vector

NS = uuid.UUID("a1b2c3d4-0000-4000-8000-5a6b7c8d9e0f")
MAX_CHUNKS_PER_DOC = 20_000


class IngestError(Exception):
    def __init__(self, status: int, code: str, message: str = "") -> None:
        super().__init__(f"{code}: {message}")
        self.status, self.code, self.message = status, code, message


@dataclass
class IngestReport:
    doc_id: str
    version: int
    chunks_added: int = 0
    chunks_skipped: int = 0
    chunks_deleted: int = 0
    tables_found: int = 0
    warnings: list[str] = field(default_factory=list)


def point_id(collection: str, doc_id: str, version: int, chunk_index: int) -> str:
    return str(uuid.uuid5(NS, f"{collection}|{doc_id}|{version}|{chunk_index}"))


def doc_id_for(collection: str, slug: str) -> str:
    return str(uuid.uuid5(NS, f"doc|{collection}|{slug}"))


def _d(v: Any) -> date | None:
    return None if v in (None, "") else (v if isinstance(v, date) else date.fromisoformat(str(v)[:10]))


def check_embed_model(meta: Meta, collection: str, embedder: Any, chunk_tokens: int, overlap: int, *, create: bool = True) -> None:
    rec = meta.get_collection(collection)
    if rec is None:
        if create:
            meta.set_collection(collection, embedder.model_id, embedder.dim, chunk_tokens, overlap)
        return
    if rec["embed_model"] != embedder.model_id:
        raise IngestError(409, "embed_model_mismatch", f"collection {collection} was built with {rec['embed_model']}, gateway reports {embedder.model_id}; run reindex")


def assign_citation_ids(chunks: list[Chunk], base_payload: dict[str, Any]) -> dict[int, str]:
    """Stable, unique ids: ``{prefix}#p{page}#s{section}`` (or ``#c{chunk_index}``); the 2nd, 3rd... chunk with the same base gets ``.2``, ``.3``."""
    from .retrieval import citation_id

    seen: dict[str, int] = {}
    out: dict[int, str] = {}
    for c in chunks:
        base = citation_id({**base_payload, "page": c.page, "section": c.section, "chunk_index": c.chunk_index})
        seen[base] = seen.get(base, 0) + 1
        out[c.chunk_index] = base if seen[base] == 1 else f"{base}.{seen[base]}"
    return out


def ingest_markdown(store: Store, meta: Meta, embedder: Any, collection: str, markdown: str, doc: dict[str, Any], *, supersede: bool = False,
                    chunk_tokens: int = 600, overlap: int = 80, min_tokens: int = 120, max_tokens: int = 750, embed_batch: int = 16,
                    caption: Callable[[str, str], str] = default_caption) -> IngestReport:
    slug = doc.get("doc_slug") or "doc"
    doc_id = doc.get("doc_id") or doc_id_for(collection, slug)
    version = int(doc.get("version", 1))
    rep = IngestReport(doc_id=doc_id, version=version)
    store.ensure_collection(collection, embedder.dim)
    check_embed_model(meta, collection, embedder, chunk_tokens, overlap)

    new_from, new_to = _d(doc.get("effective_from")), _d(doc.get("effective_to"))
    # -- version overlap (same doc lineage, other version) --
    siblings = store.scroll(collection, Filter(match={"doc_id": [doc_id]}))
    other_versions: dict[int, list[tuple[str, dict[str, Any]]]] = {}
    for pid, pl in siblings:
        if int(pl.get("version", 1)) != version:
            other_versions.setdefault(int(pl["version"]), []).append((pid, pl))
    for v, pts in sorted(other_versions.items()):
        f, t = _d(pts[0][1].get("effective_from")), _d(pts[0][1].get("effective_to"))
        if new_from is None or f is None:
            continue
        overlaps = (t is None or t > new_from) and (new_to is None or new_to > f) and f <= (new_to or date.max)
        if overlaps and v < version and f < new_from:
            if not supersede:
                raise IngestError(409, "version_overlap", f"version {v} is still effective on {new_from}; pass supersede=true to end it")
            store.set_payload(collection, [i for i, _ in pts], {"effective_to": new_from.isoformat()})
        elif overlaps:
            raise IngestError(409, "version_overlap", f"version {v} overlaps the effective window of version {version}")

    chunks: list[Chunk] = chunk_markdown(markdown, chunk_tokens=chunk_tokens, overlap=overlap, min_tokens=min_tokens, max_tokens=max_tokens, caption=caption)
    if len(chunks) > MAX_CHUNKS_PER_DOC:
        rep.warnings.append(f"chunk cap {MAX_CHUNKS_PER_DOC} reached; {len(chunks) - MAX_CHUNKS_PER_DOC} chunks dropped")
        chunks = chunks[:MAX_CHUNKS_PER_DOC]
    rep.tables_found = sum(1 for c in chunks if c.chunk_type == "table")
    if doc.get("low_ocr_confidence"):
        rep.warnings.append("low OCR confidence: chunks flagged and ranked lower")

    cids = assign_citation_ids(chunks, base_payload={"doc_slug": slug, "citation_prefix": doc.get("citation_prefix"), "system": doc.get("system"), "case_id": doc.get("case_id"), "doc_type": doc.get("doc_type")})
    existing = {pid: pl for pid, pl in siblings if int(pl.get("version", 1)) == version}
    new_ids = {point_id(collection, doc_id, version, c.chunk_index): c for c in chunks}
    stale = [pid for pid in existing if pid not in new_ids]
    to_embed = [(pid, c) for pid, c in new_ids.items() if existing.get(pid, {}).get("text_hash") != c.text_hash]
    rep.chunks_skipped = len(new_ids) - len(to_embed)

    base = {k: doc[k] for k in ("doc_slug", "source", "policy_product", "policy_version", "system", "language", "case_id", "doc_type", "citation_prefix", "pii_masked") if doc.get(k) is not None}
    base.update({"doc_id": doc_id, "version": version, "collection": collection, "embed_model": embedder.model_id,
                 "effective_from": new_from.isoformat() if new_from else None, "effective_to": new_to.isoformat() if new_to else None,
                 "low_ocr_confidence": bool(doc.get("low_ocr_confidence", False)), "created_at": now_iso()})
    base.setdefault("language", "en")
    for i in range(0, len(to_embed), embed_batch):
        batch = to_embed[i : i + embed_batch]
        vecs = embedder.embed([f"search_document: {c.embed_text}" for _, c in batch], kind="document")
        new_pts: list[Point] = []
        for (pid, c), v in zip(batch, vecs, strict=True):
            payload = {**base, "text": c.text, "page": c.page, "page_end": c.page_end, "section": c.section, "section_title": c.section_title, "heading_path": c.heading_path,
                       "chunk_index": c.chunk_index, "citation_id": cids[c.chunk_index], "chunk_type": c.chunk_type, "text_hash": c.text_hash, "token_count": c.token_count,
                       **({"caption": c.caption, "table_id": c.table_id} if c.chunk_type == "table" else {})}
            new_pts.append(Point(pid, v, sparse_vector(f"{c.heading_path} {c.caption or ''} {c.text}"), payload))
        store.upsert(collection, new_pts)
    # chunks that kept their text but moved metadata (e.g. new effective_to) are refreshed without re-embedding
    unchanged = [pid for pid in new_ids if pid in existing and existing[pid].get("text_hash") == new_ids[pid].text_hash]
    if unchanged and any(existing[p].get("effective_to") != base["effective_to"] or existing[p].get("effective_from") != base["effective_from"] for p in unchanged):
        store.set_payload(collection, unchanged, {"effective_from": base["effective_from"], "effective_to": base["effective_to"]})
    if stale:
        store.delete_ids(collection, stale)
    rep.chunks_added, rep.chunks_deleted = len(to_embed), len(stale)
    return rep


def ingest_case_text(store: Store, meta: Meta, embedder: Any, case_id: str, doc_id: str, doc_type: str, pages: list[dict[str, Any]], source: str = "", **kw: Any) -> IngestReport:
    """Hospital case context: masked text only; re-posting a ``doc_id`` replaces all of its chunks."""
    for p in pages:
        hits = pii_hits(p.get("text", ""))
        if hits:
            raise IngestError(422, "pii_detected", f"patterns: {sorted(hits)} (page {p.get('page')})")
    md = "".join(f"<!-- page:{p.get('page', i + 1)} -->\n{p.get('text', '')}\n\n" for i, p in enumerate(pages))
    doc = {"doc_id": doc_id, "doc_slug": f"case-{case_id[:8]}-{doc_type}", "source": source or doc_type, "version": 1, "system": "hospital", "case_id": case_id, "doc_type": doc_type,
           "citation_prefix": f"case-{case_id[:8]}", "pii_masked": True}
    return ingest_markdown(store, meta, embedder, "hosp_case_context", md, doc, **kw)


def purge_case(store: Store, case_id: str) -> int:
    return store.delete_where("hosp_case_context", Filter(match={"case_id": [case_id]}))


def mark_case_closed(store: Store, case_id: str, when: date | None = None) -> int:
    ids = [i for i, _ in store.scroll("hosp_case_context", Filter(match={"case_id": [case_id]}))]
    store.set_payload("hosp_case_context", ids, {"case_closed_at": (when or date.today()).isoformat()})
    return len(ids)


def janitor(store: Store, ttl_days: int, today: date | None = None) -> int:
    """Delete case context whose case closed more than ``ttl_days`` ago (also covers failed purges)."""
    cutoff = (today or date.today()) - timedelta(days=ttl_days)
    gone = 0
    for pid, pl in store.scroll("hosp_case_context"):
        closed = pl.get("case_closed_at")
        if closed and date.fromisoformat(closed) < cutoff:
            store.delete_ids("hosp_case_context", [pid])
            gone += 1
    return gone


def reindex(store: Store, meta: Meta, embedder: Any, collection: str, *, embed_batch: int = 16, ts: str | None = None, chunk_tokens: int = 600, overlap: int = 80) -> dict[str, Any]:
    """Re-embed every point into ``<name>__reindex_<ts>`` with the embedder's dimension, verify counts, swap the alias."""
    rows = store.scroll(collection)
    ts = ts or now_iso().replace(":", "").replace("-", "")[:15]
    new = f"{collection}__reindex_{ts}"
    store.create_physical(new, embedder.dim)
    for i in range(0, len(rows), embed_batch):
        batch = rows[i : i + embed_batch]
        vecs = embedder.embed([f"search_document: {(pl.get('heading_path') and 'Heading path: ' + pl['heading_path'] + chr(10)) or ''}{pl.get('text', '')}" for _, pl in batch], kind="document")
        store.upsert(new, [Point(pid, v, sparse_vector(f"{pl.get('heading_path', '')} {pl.get('caption') or ''} {pl.get('text', '')}"), {**pl, "embed_model": embedder.model_id}) for (pid, pl), v in zip(batch, vecs, strict=True)])
    if store.count(new) != len(rows):
        store.drop_physical(new)
        raise IngestError(500, "reindex_count_mismatch", "new collection does not contain every point; old collection kept")
    old = store.swap_alias(collection, new)
    meta.set_collection(collection, embedder.model_id, embedder.dim, chunk_tokens, overlap, reindexed=True)
    if old:
        store.drop_physical(old)
    return {"collection": collection, "points": len(rows), "physical": new, "dropped": old}


# -------------------------------------------------------------------------------------------
# loaders
# -------------------------------------------------------------------------------------------
class _Html(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.out: list[str] = []
        self._skip = False
        self._row: list[str] | None = None
        self._cell: list[str] = []
        self._tag = ""

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self._tag = tag
        if tag in ("script", "style"):
            self._skip = True
        elif re.fullmatch(r"h[1-4]", tag):
            self.out.append("\n" + "#" * int(tag[1]) + " ")
        elif tag == "tr":
            self._row = []
        elif tag in ("td", "th"):
            self._cell = []
        elif tag in ("p", "li", "br", "div"):
            self.out.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in ("script", "style"):
            self._skip = False
        elif tag in ("td", "th") and self._row is not None:
            self._row.append(" ".join("".join(self._cell).split()))
        elif tag == "tr" and self._row is not None:
            self.out.append("| " + " | ".join(self._row) + " |\n")
            self._row = None
        elif re.fullmatch(r"h[1-4]|p|li", tag):
            self.out.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip:
            return
        if self._row is not None:
            self._cell.append(data)
        else:
            self.out.append(data)


def csv_to_markdown(text: str) -> str:
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return ""
    head = "| " + " | ".join(rows[0]) + " |"
    sep = "|" + "---|" * len(rows[0])
    return "\n".join([head, sep] + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def load_markdown(filename: str, data: bytes, docpipe: Callable[[str, bytes], str] | None = None) -> str:
    ext = filename.lower().rsplit(".", 1)[-1] if "." in filename else "txt"
    if ext in ("md", "markdown", "txt"):
        return data.decode("utf-8", "replace")
    if ext in ("html", "htm"):
        p = _Html()
        p.feed(data.decode("utf-8", "replace"))
        return re.sub(r"\n{3,}", "\n\n", "".join(p.out)).strip()
    if ext == "csv":
        return csv_to_markdown(data.decode("utf-8", "replace"))
    if ext in ("pdf", "docx"):
        if docpipe is None:
            raise IngestError(501, "parser_unavailable", "PDF/DOCX need doc-pipeline /v1/parse")
        return docpipe(filename, data)
    raise IngestError(415, "unsupported_type", f".{ext} is not supported")
