"""Acceptance check (01-hospital-db §10): every `table.column` referenced in the hospital docs must
exist in the SQLAlchemy models. Prints missing ones; exit 1 if any (use --warn to always exit 0)."""

import pathlib
import re
import sys

sys.path.insert(0, "hospital/api")
import app.models  # noqa: E402,F401
from app.db.base import Base  # noqa: E402

tables = {t.name: {c.name for c in t.columns} for t in Base.metadata.tables.values()}
# only flag references to tables we own; plain prose like `claim.submit` is ignored
pat = re.compile(
    r"\b(" + "|".join(sorted(tables, key=len, reverse=True)) + r")\.([a-z_][a-z0-9_]*)\b"
)
# Tokens that look like table.column but are not: file names, audit/SSE event names, function names, JSON payload keys.
# Listed as (table, name) pairs so a real missing column with the same name elsewhere is still reported.
NOT_COLUMNS = (
    {("hospital", x) for x in ("dev", "local", "json", "md", "py", "yml", "yaml")}
    | {("document", x) for x in ("uploaded", "scanned", "parsed", "classified")}
    | {("doc_request", x) for x in ("opened", "closed")}
    | {
        ("outbox", "enqueue"),
        ("outbox", "next_sequence"),
        ("settlement", "received"),
        ("claim_draft", "totals"),
        ("patient", "name"),
        (
            "patient",
            "member_id",
        ),  # JSON alias path / python attribute (member id lives on insurance_policy_ref)
        ("signoff", "four_eyes"),
        ("signoff", "invalidate"),
        ("signoff", "invalidated"),
    }
)
missing: dict[tuple[str, str], set[str]] = {}
for doc in sorted(pathlib.Path("docs/implementation/02-dev-A-hospital").glob("*.md")):
    for m in pat.finditer(doc.read_text()):
        t, c = m.groups()
        if c not in tables[t] and (t, c) not in NOT_COLUMNS:
            missing.setdefault((t, c), set()).add(doc.name)
for (t, c), docs in sorted(missing.items()):
    print(f"MISSING {t}.{c}  <- {', '.join(sorted(docs))}")
print(f"{len(missing)} missing column reference(s)")
sys.exit(0 if (not missing or "--warn" in sys.argv) else 1)
