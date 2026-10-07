"""claim_case, claim_document, bill_line

Revision ID: 0003
Revises: 0002
"""

from alembic import op
from app.migration_helpers import add_updated_at_trigger

revision = "0003"
down_revision = "0002"


def upgrade() -> None:
    op.execute(
        """CREATE TYPE core.insurer_case_status AS ENUM
 ('received','verifying','needs_info','ready_for_decision','awaiting_approval','approved','partially_approved',
  'rejected','escalated','settled','closed')"""
    )
    op.execute(
        """CREATE TABLE core.claim_case (
  id UUID PRIMARY KEY, insurer_claim_no TEXT UNIQUE NOT NULL, hospital_claim_ref TEXT NOT NULL,
  hospital_id UUID NOT NULL REFERENCES core.network_hospital(id),
  claim_type TEXT NOT NULL CHECK (claim_type IN ('cashless','reimbursement')),
  admission_type TEXT NOT NULL CHECK (admission_type IN ('planned','emergency')),
  status core.insurer_case_status NOT NULL DEFAULT 'received',
  policy_id UUID REFERENCES core.policy(id), member_id UUID REFERENCES core.policy_member(id),
  claimed_amount NUMERIC(14,2) NOT NULL, recommended_amount NUMERIC(14,2), approved_amount NUMERIC(14,2),
  contract_version TEXT NOT NULL, submission JSONB NOT NULL CHECK (jsonb_typeof(submission) = 'object'),
  submission_hash CHAR(64) NOT NULL, received_at timestamptz NOT NULL, assigned_reviewer TEXT,
  priority SMALLINT NOT NULL DEFAULT 3, sla_due_at timestamptz, closure_reason TEXT,
  last_callback_seq BIGINT NOT NULL DEFAULT 0,
  created_at timestamptz NOT NULL DEFAULT now(), updated_at timestamptz NOT NULL DEFAULT now(),
  UNIQUE (hospital_id, hospital_claim_ref))"""
    )
    op.execute("CREATE INDEX ix_case_status ON core.claim_case(status, priority, sla_due_at)")
    op.execute(
        "CREATE INDEX ix_case_reviewer ON core.claim_case(assigned_reviewer) "
        "WHERE status IN ('verifying','needs_info','ready_for_decision')"
    )
    op.execute(
        """CREATE TABLE core.claim_document (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), doc_type TEXT NOT NULL,
  filename TEXT NOT NULL, sha256 CHAR(64) NOT NULL, size_bytes BIGINT NOT NULL, pages INT NOT NULL,
  object_key TEXT, fetch_status TEXT NOT NULL DEFAULT 'pending'
    CHECK (fetch_status IN ('pending','fetched','failed','hash_mismatch')),
  fetch_attempts INT NOT NULL DEFAULT 0, parse_confidence NUMERIC(4,3),
  superseded_by UUID REFERENCES core.claim_document(id), added_via_query UUID,
  created_at timestamptz NOT NULL DEFAULT now())"""
    )
    op.execute("CREATE INDEX ix_doc_case ON core.claim_document(case_id)")
    op.execute(
        """CREATE TABLE core.bill_line (
  id UUID PRIMARY KEY, case_id UUID NOT NULL REFERENCES core.claim_case(id), line_no INT NOT NULL, code TEXT,
  description TEXT NOT NULL, category TEXT NOT NULL, qty NUMERIC(10,3) NOT NULL, unit_price NUMERIC(14,2) NOT NULL,
  amount NUMERIC(14,2) NOT NULL, service_date DATE, source_doc_id UUID REFERENCES core.claim_document(id),
  source_page INT, UNIQUE (case_id, line_no))"""
    )
    add_updated_at_trigger(op, "core", "claim_case")


def downgrade() -> None:
    for t in ("bill_line", "claim_document", "claim_case"):
        op.execute(f"DROP TABLE IF EXISTS core.{t} CASCADE")
    op.execute("DROP TYPE IF EXISTS core.insurer_case_status")
