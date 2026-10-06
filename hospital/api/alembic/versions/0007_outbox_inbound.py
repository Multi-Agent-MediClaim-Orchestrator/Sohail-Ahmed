"""outbox_inbound"""

from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TABLE outbox (                 -- reliable delivery to insurer (see 01-01 §8)
  id uuid PRIMARY KEY,
  case_id uuid REFERENCES claim_case(id),
  kind text NOT NULL,                 -- claim.submit | documents.supplement | query.response | claim.withdraw
  method text NOT NULL,
  path text NOT NULL,
  body jsonb NOT NULL,
  idempotency_key uuid NOT NULL,
  sequence bigint,                    -- per-claim monotonic (01-01 §8)
  status text NOT NULL DEFAULT 'pending',
  attempts int NOT NULL DEFAULT 0,
  next_attempt_at timestamptz NOT NULL DEFAULT now(),
  last_error text,
  response_status int,
  response_body jsonb,
  created_at timestamptz NOT NULL DEFAULT now(),
  sent_at timestamptz,
  CONSTRAINT uq_outbox_idem UNIQUE (idempotency_key),
  CONSTRAINT ck_outbox_status CHECK (status IN ('pending','sent','failed','dead')),
  CONSTRAINT ck_outbox_method CHECK (method IN ('POST','PUT','GET'))
);
CREATE INDEX ix_outbox_due ON outbox(next_attempt_at) WHERE status IN ('pending','failed');
CREATE INDEX ix_outbox_case ON outbox(case_id, sequence);
CREATE INDEX ix_outbox_dead ON outbox(created_at) WHERE status = 'dead';

CREATE TABLE inbound_callback (       -- dedup/ordering for insurer callbacks
  id uuid PRIMARY KEY,
  claim_ref text NOT NULL,
  kind text NOT NULL,                 -- status|queries|decisions|settlements
  sequence bigint NOT NULL,
  idempotency_key uuid NOT NULL,
  body jsonb NOT NULL,
  processed boolean NOT NULL DEFAULT false,
  process_error text,
  received_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_inbound_seq UNIQUE (claim_ref, kind, sequence),
  CONSTRAINT uq_inbound_idem UNIQUE (idempotency_key),
  CONSTRAINT ck_inbound_kind CHECK (kind IN ('status','queries','decisions','settlements'))
);
CREATE INDEX ix_inbound_unprocessed ON inbound_callback(received_at) WHERE processed = false;

GRANT DELETE ON outbox TO hosp_app;
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS inbound_callback CASCADE;
DROP TABLE IF EXISTS outbox CASCADE;
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
