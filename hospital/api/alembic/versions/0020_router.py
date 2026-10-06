"""router schema (doc 05 §3.1, reconciled with doc 01): discharged_at, admission_source, conversion links,
route_history, network_insurer audit columns, wider simulated_preauth status set"""

from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

UP = r"""
ALTER TABLE claim_case
  ADD COLUMN discharged_at timestamptz,
  ADD COLUMN admission_source text,
  ADD COLUMN converted_from uuid REFERENCES claim_case(id),
  ADD COLUMN converted_to uuid REFERENCES claim_case(id),
  ADD CONSTRAINT ck_case_admission_source CHECK (admission_source IS NULL OR admission_source IN ('ER','OPD','referral'));
CREATE INDEX ix_case_converted_from ON claim_case(converted_from) WHERE converted_from IS NOT NULL;

ALTER TABLE network_insurer
  ADD COLUMN updated_by uuid REFERENCES app_user(id),
  ADD COLUMN updated_at timestamptz NOT NULL DEFAULT now();

ALTER TABLE simulated_preauth DROP CONSTRAINT ck_simulated_preauth_status;
ALTER TABLE simulated_preauth ADD CONSTRAINT ck_simulated_preauth_status
  CHECK (status IN ('approved','enhanced','cancelled','expired','revoked','pending'));

CREATE TABLE route_history (
  id            uuid PRIMARY KEY DEFAULT uuid_generate_v7(),
  case_id       uuid NOT NULL REFERENCES claim_case(id),
  seq           int  NOT NULL,
  decision      jsonb NOT NULL,
  decision_hash char(64) NOT NULL,
  trigger       text NOT NULL,
  actor         text NOT NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_route_history_seq UNIQUE (case_id, seq),
  CONSTRAINT ck_route_history_trigger CHECK (trigger IN
    ('create','patch','doc_classified','draft_totals','config_republish','manual','override','ack','post_submission'))
);
CREATE INDEX ix_route_history_case ON route_history (case_id, seq DESC);
"""
DOWN = r"""
DROP TABLE route_history;
ALTER TABLE simulated_preauth DROP CONSTRAINT ck_simulated_preauth_status;
ALTER TABLE simulated_preauth ADD CONSTRAINT ck_simulated_preauth_status CHECK (status IN ('approved','enhanced','cancelled','expired'));
ALTER TABLE network_insurer DROP COLUMN updated_by, DROP COLUMN updated_at;
DROP INDEX ix_case_converted_from;
ALTER TABLE claim_case DROP CONSTRAINT ck_case_admission_source, DROP COLUMN discharged_at, DROP COLUMN admission_source,
  DROP COLUMN converted_from, DROP COLUMN converted_to;
"""


def _run(sql: str) -> None:
    cur = op.get_bind().connection.cursor()
    cur.execute(sql)
    cur.close()


def upgrade() -> None:
    _run(UP)


def downgrade() -> None:
    _run(DOWN)
