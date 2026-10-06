"""config"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

UP_SQL = r"""
CREATE TYPE config_status AS ENUM ('draft','published','retired');   -- shape per 01-03 §3

CREATE TABLE config_set (
  id uuid PRIMARY KEY,
  domain text NOT NULL,              -- doc_requirements|deadlines|router_rules|confidence_gates|signoff|query_policy
  name text NOT NULL,                -- scope key, 'default' or e.g. 'cashless/planned/cardiac'
  description text,
  created_by text NOT NULL,          -- user id or 'seed'
  created_at timestamptz NOT NULL DEFAULT now(),
  CONSTRAINT uq_config_set UNIQUE (domain, name)
);
CREATE TABLE config_version (
  id uuid PRIMARY KEY,
  config_set_id uuid NOT NULL REFERENCES config_set(id),
  version int NOT NULL,
  status config_status NOT NULL DEFAULT 'draft',
  payload jsonb NOT NULL,
  payload_schema text NOT NULL,                   -- e.g. doc_requirements@1 (01-03)
  checksum char(64) NOT NULL,
  effective_from timestamptz,
  effective_to timestamptz,
  change_note text NOT NULL,
  created_by text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  published_by text,
  second_approver text,                           -- two-person rule (01-03 §5)
  published_at timestamptz,
  CONSTRAINT uq_config_version UNIQUE (config_set_id, version),
  CONSTRAINT ck_config_version_published CHECK (status <> 'published' OR (effective_from IS NOT NULL AND published_by IS NOT NULL)),
  CONSTRAINT ck_config_version_window CHECK (effective_to IS NULL OR effective_to > effective_from),
  CONSTRAINT ck_config_publish_distinct CHECK (second_approver IS NULL OR second_approver <> published_by),
  CONSTRAINT ex_config_no_overlap EXCLUDE USING gist (
      config_set_id WITH =,
      tstzrange(effective_from, effective_to, '[)') WITH &&) WHERE (status = 'published')
);

CREATE FUNCTION guard_config_version() RETURNS trigger AS $$
BEGIN
  IF OLD.status = 'published' AND (
       NEW.payload IS DISTINCT FROM OLD.payload OR NEW.version <> OLD.version OR
       NEW.checksum <> OLD.checksum OR NEW.effective_from IS DISTINCT FROM OLD.effective_from OR
       NEW.status NOT IN ('published','retired'))
  THEN RAISE EXCEPTION 'published config is immutable' USING ERRCODE = '42501'; END IF;
  IF OLD.status = 'retired' THEN RAISE EXCEPTION 'retired config is immutable' USING ERRCODE = '42501'; END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql;
CREATE TRIGGER trg_config_guard BEFORE UPDATE ON config_version
  FOR EACH ROW EXECUTE FUNCTION guard_config_version();
"""

DOWN_SQL = r"""
DROP TABLE IF EXISTS config_version CASCADE;
DROP TABLE IF EXISTS config_set CASCADE;
DROP FUNCTION IF EXISTS guard_config_version();
DROP TYPE IF EXISTS config_status;
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
