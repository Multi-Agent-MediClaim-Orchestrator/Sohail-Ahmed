"""Built-in scenarios (Drop 1). More are loaded from ``scenarios/*.yaml`` (Drop 2)."""

from __future__ import annotations

from .dsl import Scenario, parse_scenario

HAPPY = """
id: happy_path
name: Happy path
description: Acknowledge, verify, approve in full, settle.
steps:
  - {id: verifying, kind: status, status: verifying, after: 2s}
  - {id: ready, kind: status, status: ready_for_decision, after: 5s}
  - {id: approve, kind: decision, outcome: approve, after: 5s}
  - {id: paid, kind: settlement, settle: paid, after: 10s}
"""

QUERY_TWICE = """
id: query_twice_then_approve
name: Two queries then approval
description: Raises two queries (waiting for each answer) and then approves.
steps:
  - {id: verifying, kind: status, status: verifying, after: 2s}
  - {id: q1, kind: query, category: missing_document, requested_doc_types: [discharge_summary], text: "Please upload the signed discharge summary for this admission.", after: 3s}
  - {id: w1, kind: wait_response}
  - {id: q2, kind: query, category: clarification, text: "Please clarify the reason for the extended length of stay.", after: 3s}
  - {id: w2, kind: wait_response}
  - {id: ready, kind: status, status: ready_for_decision, after: 3s}
  - {id: approve, kind: decision, outcome: approve, after: 5s}
  - {id: paid, kind: settlement, settle: paid, after: 10s}
"""


def builtin_scenarios() -> list[Scenario]:
    return [parse_scenario(HAPPY), parse_scenario(QUERY_TWICE)]
