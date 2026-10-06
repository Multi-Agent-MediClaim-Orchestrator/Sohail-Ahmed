"""documents"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE document (
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  original_filename text NOT NULL,              -- sanitised display name; stored key is generated
  mime_type text NOT NULL,
  size_bytes bigint NOT NULL,
  sha256 char(64) NOT NULL,
  storage_key text NOT NULL,                    -- hospital-docs/<case>/<doc>/original.<ext>
  pages int,
  scan_status text NOT NULL DEFAULT 'pending',
  lifecycle text NOT NULL DEFAULT 'active',     -- active|superseded|deleted|quarantined
  doc_type doc_type,
  doc_type_source text,
  classification_confidence real,
  parse_status text NOT NULL DEFAULT 'pending',
  parse_attempts smallint NOT NULL DEFAULT 0,   -- sweeper (doc 03 §6) stops at 5
  last_trigger_at timestamptz,                  -- sweeper re-trigger bookkeeping
  parse_confidence real,
  quality_score real,
  quality_flags text[] NOT NULL DEFAULT '{}',   -- blurry, skewed, low_light, cropped, illegible_candidate
  has_required_stamp boolean,
  supersedes_id uuid REFERENCES document(id),   -- newer copy replaces older
  parent_id uuid REFERENCES document(id),       -- child produced by splitting a mixed PDF (doc 03 §8)
  page_range int4range,                         -- pages of the parent covered by this child
  quarantine_reason text,
  deleted_at timestamptz,
  deleted_by uuid REFERENCES app_user(id),
  purge_after timestamptz,                      -- tombstone retention (30 d)
  uploaded_by uuid REFERENCES app_user(id),
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_document_scan_status CHECK (scan_status IN ('pending','clean','infected','error')),
  CONSTRAINT ck_document_lifecycle CHECK (lifecycle IN ('active','superseded','deleted','quarantined')),
  CONSTRAINT ck_document_type_source CHECK (doc_type_source IS NULL OR doc_type_source IN ('auto','manual')),
  CONSTRAINT ck_document_parse_status CHECK (parse_status IN ('pending','processing','parsed','failed','needs_review')),
  CONSTRAINT ck_document_size CHECK (size_bytes > 0 AND size_bytes <= 26214400),
  CONSTRAINT ck_document_conf CHECK (
        (classification_confidence IS NULL OR classification_confidence BETWEEN 0 AND 1)
    AND (parse_confidence IS NULL OR parse_confidence BETWEEN 0 AND 1)
    AND (quality_score IS NULL OR quality_score BETWEEN 0 AND 1)),
  CONSTRAINT ck_document_no_self_ref CHECK (supersedes_id IS NULL OR supersedes_id <> id),
  CONSTRAINT ck_document_infected_lifecycle CHECK (scan_status <> 'infected' OR lifecycle = 'quarantined'),
  CONSTRAINT uq_document_case_sha UNIQUE (case_id, sha256)             -- duplicate upload guard
);
CREATE INDEX ix_doc_case ON document(case_id) WHERE lifecycle = 'active';
CREATE INDEX ix_doc_case_all ON document(case_id);
CREATE INDEX ix_doc_pending_parse ON document(last_trigger_at) WHERE parse_status = 'pending' AND scan_status = 'clean';
CREATE INDEX ix_doc_purge ON document(purge_after) WHERE purge_after IS NOT NULL;
CREATE INDEX ix_doc_parent ON document(parent_id) WHERE parent_id IS NOT NULL;

CREATE TABLE document_parse (          -- one row per parse attempt (two-pass agreement)
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL REFERENCES document(id),
  pass_no smallint NOT NULL,
  engine text NOT NULL,                -- 'mineru-pipeline' | 'crew-llm' | 'vision-ocr'
  engine_version text,
  raw_markdown_key text,               -- MinIO key; raw text may contain PII, never sent out
  masked_text_key text,                -- after Presidio masking
  entities jsonb,
  typed_json jsonb,
  confidence real,
  agreement_score real,                -- filled on pass 2
  duration_ms int,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_document_parse_pass UNIQUE (document_id, pass_no),
  CONSTRAINT ck_document_parse_pass CHECK (pass_no BETWEEN 1 AND 3),
  CONSTRAINT ck_document_parse_size CHECK (typed_json IS NULL OR octet_length(typed_json::text) <= 1048576)
);

CREATE TABLE doc_request (            -- needs-info requests raised to patient/desk
  id uuid PRIMARY KEY,
  case_id uuid NOT NULL REFERENCES claim_case(id),
  doc_type doc_type NOT NULL,
  rule_id text,                       -- completeness rule that raised it, e.g. R-IMP-02
  reason text NOT NULL,
  status text NOT NULL DEFAULT 'open',
  due_by timestamptz,
  reminders_sent int NOT NULL DEFAULT 0,
  fulfilled_by_doc uuid REFERENCES document(id),
  waived_by uuid REFERENCES app_user(id),
  waive_reason text,
  created_at timestamptz NOT NULL DEFAULT now(),
  updated_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT ck_doc_request_status CHECK (status IN ('open','fulfilled','waived','expired')),
  CONSTRAINT ck_doc_request_waive CHECK (status <> 'waived' OR (waived_by IS NOT NULL AND waive_reason IS NOT NULL))
);
CREATE UNIQUE INDEX uq_doc_request_open ON doc_request(case_id, doc_type) WHERE status = 'open';
CREATE INDEX ix_doc_request_due ON doc_request(due_by) WHERE status = 'open';

CREATE TRIGGER trg_document_updated BEFORE UPDATE ON document
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();
CREATE TRIGGER trg_doc_request_updated BEFORE UPDATE ON doc_request
  FOR EACH ROW EXECUTE FUNCTION set_updated_at();

GRANT DELETE ON doc_request TO hosp_app;
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS doc_request CASCADE;
DROP TABLE IF EXISTS document_parse CASCADE;
DROP TABLE IF EXISTS document CASCADE;
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
