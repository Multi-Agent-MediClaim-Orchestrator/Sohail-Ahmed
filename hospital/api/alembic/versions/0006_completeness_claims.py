"""completeness_claims"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE completeness_check (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  config_version int NOT NULL,
  run_no int NOT NULL,
  complete boolean NOT NULL,
  result jsonb NOT NULL,        -- {items:[{rule_id, doc_type, requirement, status, severity, document_ids, message}]}
  trigger text,                 -- upload|parse|manual|waive|config
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_completeness_run UNIQUE (case_id, run_no)
);

CREATE TABLE claim_draft (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  version int NOT NULL,
  payload jsonb NOT NULL,                  -- ClaimSubmission minus signed URLs
  validation jsonb,                        -- {errors[], warnings[], reconciliation{lines_sum, bill_total, diff}}
  source text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_claim_draft_version UNIQUE (case_id, version),
  CONSTRAINT ck_claim_draft_source CHECK (source IN ('agent','human_edit'))
);

CREATE TABLE bill_line (
  id uuid PRIMARY KEY,
  draft_id uuid NOT NULL REFERENCES claim_draft(id) ON DELETE CASCADE,
  line_no int NOT NULL,
  code text,
  description text NOT NULL,
  category text NOT NULL,
  qty numeric(10,3) NOT NULL,
  unit_price numeric(14,2) NOT NULL,
  amount numeric(14,2) NOT NULL,
  source_document_id uuid REFERENCES document(id),
  source_page int,
  CONSTRAINT uq_bill_line_no UNIQUE (draft_id, line_no),
  CONSTRAINT ck_bill_line_category CHECK (category IN ('room','icu','surgery','anaesthesia','medicine','consumable','implant','investigation','consultation','other')),
  CONSTRAINT ck_bill_line_nonneg CHECK (qty >= 0 AND unit_price >= 0 AND amount >= 0)
);

CREATE TABLE signoff (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  draft_id uuid NOT NULL REFERENCES claim_draft(id),
  officer_id uuid NOT NULL REFERENCES app_user(id),
  decision text NOT NULL,
  comment text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_signoff_decision CHECK (decision IN ('approved','returned')),
  CONSTRAINT uq_signoff_officer_draft UNIQUE (draft_id, officer_id)    -- four-eyes: one vote per officer
);
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS signoff CASCADE;
DROP TABLE IF EXISTS bill_line CASCADE;
DROP TABLE IF EXISTS claim_draft CASCADE;
DROP TABLE IF EXISTS completeness_check CASCADE;
"""


def _run(sql: str) -> None:
    # raw cursor, no params: '%' in function bodies must not be read as a placeholder
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP_SQL)


def downgrade() -> None:
    _run(DOWN_SQL)
