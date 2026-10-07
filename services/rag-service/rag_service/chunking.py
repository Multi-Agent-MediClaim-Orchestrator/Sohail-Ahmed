"""Structure-aware chunking (04-05 §6.1).

Input: normalised markdown with page markers ``<!-- page:12 -->``, headings ``#..####`` and markdown tables.
Rules: tables are atomic (oversized ones split by row groups with the header repeated), definitions are single chunks,
numbered clauses stay whole, chunks target ``chunk_tokens`` with ``overlap`` tokens carried between neighbours of one section,
short sections merge with the next sibling under the same parent."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Callable
from dataclasses import dataclass, field

_TOKEN = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_PAGE = re.compile(r"^\s*<!--\s*page:(\d+)\s*-->\s*$")
_HEADING = re.compile(r"^(#{1,4})\s+(.*\S)\s*$")
_SECTION_NO = re.compile(r"^(\d+(?:\.\d+)*)[.)]?\s+(.*)$")
_CLAUSE = re.compile(r"^\s*\d+(?:\.\d+)+[.)]?\s")
_DEFINITION = re.compile(r'^"?([A-Z][\w\s\-/]{1,60})"?\s+(means|shall mean)\b')
_SENT_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_SEPARATOR = re.compile(r"^\s*\|?\s*:?-{2,}")


def count_tokens(text: str) -> int:
    return len(_TOKEN.findall(text))


def text_hash(text: str) -> str:
    norm = " ".join(text.split())
    return "sha256:" + hashlib.sha256(norm.encode()).hexdigest()


@dataclass
class Chunk:
    text: str
    chunk_type: str  # text | table | list | definition
    page: int
    page_end: int
    section: str | None
    section_title: str | None
    heading_path: str
    caption: str | None = None
    table_id: str | None = None
    chunk_index: int = 0
    token_count: int = 0
    text_hash: str = ""

    @property
    def embed_text(self) -> str:
        """Text that is embedded: heading path (and table caption) prefixed; stripped on display."""
        pre = f"Heading path: {self.heading_path}\n" if self.heading_path else ""
        if self.caption:
            pre += f"Caption: {self.caption}\n"
        return pre + self.text


@dataclass
class _Block:
    kind: str  # paragraph | list | table | definition
    text: str
    page: int
    page_end: int


@dataclass
class _Section:
    path: list[str]
    number: str | None
    title: str | None
    blocks: list[_Block] = field(default_factory=list)


def default_caption(table_md: str, heading: str) -> str:
    """Offline caption: heading + header-row labels, <= 25 words. Production swaps in a ``reason-local`` call (cached)."""
    header = next((ln for ln in table_md.splitlines() if _TABLE_ROW.match(ln)), "")
    labels = [c.strip() for c in header.strip().strip("|").split("|") if c.strip()]
    words = f"Table of {', '.join(labels)}" + (f" under {heading}" if heading else "")
    return " ".join(words.split()[:25])


def _parse(md: str) -> list[_Section]:
    sections: list[_Section] = [_Section(path=[], number=None, title=None)]
    stack: list[tuple[int, str]] = []
    page = 1
    para: list[str] = []
    para_page = 1
    table: list[str] = []

    def flush_para() -> None:
        nonlocal para
        if para:
            text = "\n".join(para).strip()
            if text:
                kind = "definition" if _DEFINITION.match(text) else ("list" if all(re.match(r"^\s*([-*+]|\d+[.)])\s", ln) for ln in para) else "paragraph")
                sections[-1].blocks.append(_Block(kind, text, para_page, page))
            para = []

    def flush_table() -> None:
        nonlocal table
        if table:
            sections[-1].blocks.append(_Block("table", "\n".join(table), para_page, page))
            table = []

    for line in md.splitlines():
        m = _PAGE.match(line)
        if m:
            flush_para()
            flush_table()
            page = int(m.group(1))
            continue
        h = _HEADING.match(line)
        if h:
            flush_para()
            flush_table()
            level, title = len(h.group(1)), h.group(2)
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            num = _SECTION_NO.match(title)
            sections.append(_Section(path=[t for _, t in stack], number=num.group(1) if num else None, title=num.group(2) if num else title))
            continue
        if _TABLE_ROW.match(line) or (table and _SEPARATOR.match(line)):
            flush_para()
            if not table:
                para_page = page
            table.append(line.rstrip())
            continue
        flush_table()
        if not line.strip():
            flush_para()
            continue
        if not para:
            para_page = page
        # a numbered clause or a definition starts a new block even without a blank line
        if para and (_CLAUSE.match(line) or _DEFINITION.match(line.strip())):
            flush_para()
            para_page = page
        para.append(line.rstrip())
    flush_para()
    flush_table()
    return [s for s in sections if s.blocks]


def _split_sentences(text: str) -> list[str]:
    parts = [p for p in _SENT_SPLIT.split(text) if p.strip()]
    return parts or [text]


def _tail(text: str, overlap: int) -> str:
    """Last whole sentences totalling <= overlap tokens."""
    out: list[str] = []
    total = 0
    for s in reversed(_split_sentences(text)):
        n = count_tokens(s)
        if total + n > overlap:
            break
        out.insert(0, s)
        total += n
    return " ".join(out)


def _table_chunks(md: str, max_tokens: int) -> list[str]:
    if count_tokens(md) <= max_tokens:
        return [md]
    lines = [ln for ln in md.splitlines() if ln.strip()]
    header = lines[:2] if len(lines) > 1 and _SEPARATOR.match(lines[1]) else lines[:1]
    rows = lines[len(header) :]
    out: list[str] = []
    cur: list[str] = []
    for r in rows:
        if cur and count_tokens("\n".join(header + cur + [r])) > max_tokens:
            out.append("\n".join(header + cur))
            cur = []
        cur.append(r)
    if cur:
        out.append("\n".join(header + cur))
    return out


def chunk_markdown(md: str, *, chunk_tokens: int = 600, overlap: int = 80, min_tokens: int = 120, max_tokens: int = 750, table_max_tokens: int = 1500,
                   caption: Callable[[str, str], str] = default_caption) -> list[Chunk]:
    sections = _parse(md)
    chunks: list[Chunk] = []
    carry: list[_Block] = []  # short-section remainder to merge into the next sibling
    carry_meta: _Section | None = None

    def emit(text: str, kind: str, sec: _Section, page: int, page_end: int, **kw: object) -> None:
        text = text.strip()
        if text:
            chunks.append(Chunk(text=text, chunk_type=kind, page=page, page_end=page_end, section=sec.number, section_title=sec.title,
                                heading_path=" > ".join(sec.path), token_count=count_tokens(text), text_hash=text_hash(text), **kw))  # type: ignore[arg-type]

    for i, sec in enumerate(sections):
        blocks = carry + sec.blocks
        meta = carry_meta if carry_meta is not None else sec
        carry, carry_meta = [], None
        buf = ""  # accumulated text in this section
        buf_page = blocks[0].page if blocks else 1
        buf_end = buf_page
        pending_overlap = ""

        def flush(final: bool = False) -> None:
            nonlocal buf, pending_overlap, buf_page, buf_end
            if not buf.strip():
                return
            emit(buf, "text", meta, buf_page, buf_end)  # noqa: B023 - called within the iteration that defines it
            pending_overlap = _tail(buf, overlap) if not final else ""
            buf = pending_overlap
            buf_page = buf_end  # noqa: B023

        text_tokens = sum(count_tokens(b.text) for b in blocks if b.kind != "table")
        nxt = sections[i + 1] if i + 1 < len(sections) else None
        mergeable = (text_tokens < min_tokens and nxt is not None and nxt.path[:-1] == sec.path[:-1] and not any(b.kind == "table" for b in blocks)
                     and not any(b.kind == "definition" for b in blocks))
        if mergeable:
            carry, carry_meta = blocks, meta
            continue

        for b in blocks:
            if b.kind == "table":
                flush(final=True)
                tid = text_hash(b.text)[7:19]
                cap = caption(b.text, sec.title or "")
                for part in _table_chunks(b.text, table_max_tokens):
                    emit(part, "table", meta, b.page, b.page_end, caption=cap, table_id=tid)
                continue
            if b.kind == "definition":
                flush(final=True)
                emit(b.text, "definition", meta, b.page, b.page_end)
                continue
            n = count_tokens(b.text)
            if n > max_tokens:  # one huge block: cut at sentence boundaries
                flush(final=True)
                cur = ""
                for s in _split_sentences(b.text):
                    if cur and count_tokens(cur + " " + s) > chunk_tokens:
                        emit(cur, "text", meta, b.page, b.page_end)
                        cur = _tail(cur, overlap) + " " + s
                    else:
                        cur = (cur + " " + s).strip()
                buf, buf_page, buf_end = cur, b.page, b.page_end
                continue
            if buf and count_tokens(buf + "\n\n" + b.text) > chunk_tokens:
                flush()
                # overlap is only a prefix of the next chunk; drop it if the next block alone fills the chunk
                if pending_overlap and count_tokens(pending_overlap + "\n\n" + b.text) > max_tokens:
                    buf = ""
            if not buf:
                buf_page = b.page
            buf = (buf + "\n\n" + b.text).strip()
            buf_end = b.page_end
        if buf.strip() and buf.strip() != pending_overlap.strip():
            emit(buf, "text", meta, buf_page, buf_end)

    for idx, c in enumerate(chunks):
        c.chunk_index = idx
    return chunks
