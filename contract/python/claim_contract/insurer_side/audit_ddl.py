"""DDL for the audit tables (01-04 §3), parameterised by schema so both sides (and tests) share one definition."""

from __future__ import annotations


def audit_ddl_pg(schema: str | None = "audit", app_role: str | None = None) -> list[str]:
    """Statements creating audit tables, append-only triggers and grants. ``schema=None`` → default schema."""
    p = f"{schema}." if schema else ""
    stmts = [
        "DO $$ BEGIN CREATE TYPE audit_actor_type AS ENUM ('agent','human','system','external'); "
        "EXCEPTION WHEN duplicate_object THEN NULL; END $$",
        f"""CREATE TABLE {p}audit_event (
  id UUID PRIMARY KEY, case_id UUID NOT NULL, seq BIGINT NOT NULL, ts TIMESTAMPTZ NOT NULL,
  actor_type audit_actor_type NOT NULL, actor_id TEXT NOT NULL, event_type TEXT NOT NULL,
  payload JSONB NOT NULL, config_versions JSONB NOT NULL DEFAULT '{{}}', model_info JSONB, journey_id UUID,
  prev_hash CHAR(64) NOT NULL, hash CHAR(64) NOT NULL, UNIQUE (case_id, seq))""",
        f"CREATE INDEX audit_event_case ON {p}audit_event (case_id, seq)",
        f"CREATE INDEX audit_event_type_ts ON {p}audit_event (event_type, ts)",
        f"""CREATE TABLE {p}case_audit_head (
  case_id UUID PRIMARY KEY, last_seq BIGINT NOT NULL, last_hash CHAR(64) NOT NULL,
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        f"""CREATE TABLE {p}audit_anchor (
  anchor_date DATE PRIMARY KEY, merkle_root CHAR(64) NOT NULL, case_count INT NOT NULL, object_key TEXT,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        f"""CREATE OR REPLACE FUNCTION {p}audit_event_block() RETURNS trigger AS $$
BEGIN RAISE EXCEPTION 'audit_event is append-only'; END $$ LANGUAGE plpgsql""",
        f"""CREATE TRIGGER audit_event_no_update BEFORE UPDATE OR DELETE ON {p}audit_event
  FOR EACH ROW EXECUTE FUNCTION {p}audit_event_block()""",
        f"""CREATE TRIGGER audit_event_no_truncate BEFORE TRUNCATE ON {p}audit_event
  FOR EACH STATEMENT EXECUTE FUNCTION {p}audit_event_block()""",
    ]
    if app_role:
        stmts += [
            f"REVOKE UPDATE, DELETE, TRUNCATE ON {p}audit_event FROM {app_role}",
            f"GRANT INSERT, SELECT ON {p}audit_event TO {app_role}",
            f"GRANT SELECT, INSERT, UPDATE ON {p}case_audit_head TO {app_role}",
            f"GRANT SELECT, INSERT ON {p}audit_anchor TO {app_role}",
        ]
    return stmts


def audit_ddl_sqlite() -> list[str]:
    return [
        """CREATE TABLE audit_event (id TEXT PRIMARY KEY, case_id TEXT NOT NULL, seq INTEGER NOT NULL, ts TEXT NOT NULL,
  actor_type TEXT NOT NULL, actor_id TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL,
  config_versions TEXT NOT NULL DEFAULT '{}', model_info TEXT, journey_id TEXT, prev_hash TEXT NOT NULL,
  hash TEXT NOT NULL, UNIQUE (case_id, seq))""",
        "CREATE TABLE case_audit_head (case_id TEXT PRIMARY KEY, last_seq INTEGER NOT NULL, last_hash TEXT NOT NULL, updated_at TEXT)",
        "CREATE TABLE audit_anchor (anchor_date TEXT PRIMARY KEY, merkle_root TEXT NOT NULL, case_count INTEGER NOT NULL, object_key TEXT)",
    ]
