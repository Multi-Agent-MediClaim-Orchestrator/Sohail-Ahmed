"""reporting views and extra indexes (01-insurer-db §3.7-3.8); gate stats view (03-04)

Revision ID: 0012
Revises: 0011
"""

from alembic import op

revision = "0012"
down_revision = "0011"


def upgrade() -> None:
    op.execute(
        """CREATE VIEW core.v_case_list AS
SELECT c.id, c.insurer_claim_no, c.hospital_claim_ref, h.name AS hospital_name, c.claim_type, c.admission_type,
       c.status, c.priority, c.claimed_amount, c.recommended_amount, c.approved_amount, c.assigned_reviewer,
       c.sla_due_at, c.sla_breached, c.received_at, c.updated_at, c.etag, m.full_name AS member_name, p.policy_number
FROM core.claim_case c
JOIN core.network_hospital h ON h.id = c.hospital_id
LEFT JOIN core.policy_member m ON m.id = c.member_id
LEFT JOIN core.policy p ON p.id = c.policy_id"""
    )
    op.execute(
        """CREATE VIEW core.v_reviewer_load AS
SELECT assigned_reviewer, count(*) FILTER (WHERE status IN ('verifying','needs_info','ready_for_decision')) AS open_cases,
       count(*) FILTER (WHERE sla_breached) AS breached
FROM core.claim_case WHERE assigned_reviewer IS NOT NULL GROUP BY assigned_reviewer"""
    )
    op.execute(
        """CREATE VIEW core.v_query_aging AS
SELECT q.case_id, q.id AS query_id, q.round, q.status, q.due_by, GREATEST(now() - q.due_by, interval '0') AS overdue_by
FROM core.query q WHERE q.status IN ('open','draft_ready')"""
    )
    op.execute(
        """CREATE VIEW core.v_gate_stats AS
SELECT t.tier, t.status, count(*) AS tasks,
       avg(extract(epoch FROM (coalesce(t.closed_at, now()) - t.opened_at))) AS avg_seconds
FROM core.decision_task t GROUP BY t.tier, t.status"""
    )
    op.execute("GRANT SELECT ON core.v_case_list, core.v_reviewer_load, core.v_query_aging, core.v_gate_stats TO ins_app, ins_readonly")
    op.execute("CREATE INDEX ix_case_member_dates ON core.claim_case(member_id, received_at)")
    op.execute("CREATE INDEX ix_case_sla ON core.claim_case(sla_due_at) WHERE status NOT IN ('settled','closed','rejected')")
    op.execute("CREATE INDEX ix_doc_sha ON core.claim_document(sha256)")
    op.execute("CREATE INDEX ix_bill_case ON core.bill_line(case_id)")


def downgrade() -> None:
    for i in ("ix_bill_case", "ix_doc_sha", "ix_case_sla", "ix_case_member_dates"):
        op.execute(f"DROP INDEX IF EXISTS core.{i}")
    for v in ("v_gate_stats", "v_query_aging", "v_reviewer_load", "v_case_list"):
        op.execute(f"DROP VIEW IF EXISTS core.{v}")
