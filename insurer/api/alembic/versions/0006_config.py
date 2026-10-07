"""versioned config (01-03 DDL): sets, versions, immutability, no-overlap, dry runs

Revision ID: 0006
Revises: 0005
"""

from alembic import op

revision = "0006"
down_revision = "0005"


def upgrade() -> None:
    op.execute("CREATE TYPE config.config_status AS ENUM ('draft','published','retired')")
    op.execute(
        """CREATE TABLE config.config_set (
  id UUID PRIMARY KEY, domain TEXT NOT NULL, name TEXT NOT NULL, description TEXT, created_by TEXT NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(), UNIQUE (domain, name))"""
    )
    op.execute(
        """CREATE TABLE config.config_version (
  id UUID PRIMARY KEY, config_set_id UUID NOT NULL REFERENCES config.config_set(id), version INT NOT NULL,
  status config.config_status NOT NULL DEFAULT 'draft', payload JSONB NOT NULL, payload_schema TEXT NOT NULL,
  checksum CHAR(64) NOT NULL, effective_from timestamptz, effective_to timestamptz, change_note TEXT NOT NULL,
  created_by TEXT NOT NULL, created_at timestamptz NOT NULL DEFAULT now(), published_by TEXT, second_approver TEXT,
  published_at timestamptz, UNIQUE (config_set_id, version),
  CHECK (status <> 'published' OR (effective_from IS NOT NULL AND published_by IS NOT NULL)),
  CHECK (effective_to IS NULL OR effective_to > effective_from))"""
    )
    op.execute("CREATE INDEX config_version_resolve ON config.config_version (config_set_id, status, effective_from)")
    op.execute(
        """ALTER TABLE config.config_version ADD CONSTRAINT no_overlap
  EXCLUDE USING gist (config_set_id WITH =, tstzrange(effective_from, effective_to) WITH &&) WHERE (status = 'published')"""
    )
    op.execute(
        """CREATE FUNCTION config.config_version_guard() RETURNS trigger AS $$
BEGIN
  IF TG_OP = 'DELETE' THEN
    IF OLD.status <> 'draft' THEN RAISE EXCEPTION 'published config_version cannot be deleted'; END IF;
    RETURN OLD;
  END IF;
  IF OLD.status = 'published' AND (NEW.payload <> OLD.payload OR NEW.version <> OLD.version
     OR NEW.checksum <> OLD.checksum OR NEW.effective_from IS DISTINCT FROM OLD.effective_from) THEN
     RAISE EXCEPTION 'published config_version is immutable';
  END IF;
  IF OLD.status = 'retired' AND NEW.status <> 'retired' THEN
     RAISE EXCEPTION 'retired config_version cannot be reopened';
  END IF;
  RETURN NEW;
END $$ LANGUAGE plpgsql"""
    )
    op.execute(
        "CREATE TRIGGER config_version_immutable BEFORE UPDATE ON config.config_version "
        "FOR EACH ROW EXECUTE FUNCTION config.config_version_guard()"
    )
    op.execute(
        "CREATE TRIGGER config_version_nodelete BEFORE DELETE ON config.config_version "
        "FOR EACH ROW EXECUTE FUNCTION config.config_version_guard()"
    )
    op.execute(
        """CREATE TABLE config.config_dry_run (
  id UUID PRIMARY KEY, version_id UUID NOT NULL REFERENCES config.config_version(id), summary JSONB NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now())"""
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS config.config_dry_run")
    op.execute("DROP TABLE IF EXISTS config.config_version CASCADE")
    op.execute("DROP TABLE IF EXISTS config.config_set CASCADE")
    op.execute("DROP FUNCTION IF EXISTS config.config_version_guard() CASCADE")
    op.execute("DROP TYPE IF EXISTS config.config_status")
