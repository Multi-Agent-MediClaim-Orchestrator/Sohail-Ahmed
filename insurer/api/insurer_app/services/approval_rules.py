"""Approval voting rules (03-04 §6.3): distinctness, roles, segregation of duties, senior requirement. Pure."""

from __future__ import annotations

from dataclasses import dataclass


class ApprovalError(Exception):
    def __init__(self, code: str, detail: str, status: int = 403) -> None:
        super().__init__(detail)
        self.code, self.detail, self.status = code, detail, status


@dataclass(frozen=True)
class Vote:
    approver: str
    verdict: str
    roles: frozenset[str]
    valid: bool = True


def check_vote(
    *, user: str, user_roles: frozenset[str], allowed_roles: frozenset[str], verdict: str, comment: str | None, prior_votes: list[Vote],
    submitter: str, assignee: str | None, sod: bool,
) -> None:
    """Raises ``ApprovalError`` with the documented codes (sod_violation, role_not_allowed, already_voted, comment_required)."""
    if any(v.approver == user for v in prior_votes):
        raise ApprovalError("already_voted", "you already voted on this task", 409)
    if sod and user in {submitter, assignee}:
        raise ApprovalError("sod_violation", "you prepared or are assigned to this case", 403)
    if not (user_roles & allowed_roles):
        raise ApprovalError("role_not_allowed", f"requires one of {sorted(allowed_roles)}", 403)
    if verdict in ("return", "reject") and not (comment or "").strip():
        raise ApprovalError("comment_required", "a comment is required to return or reject", 422)
    if verdict not in ("approve", "return", "reject"):
        raise ApprovalError("validation_error", f"unknown verdict {verdict!r}", 422)


def can_finalize(valid_approve_votes: list[Vote], required: int, min_senior: int) -> tuple[bool, int, int]:
    """(finalizes?, remaining, senior_needed). Votes must come from distinct people; seniors count for min_senior."""
    distinct = {v.approver: v for v in valid_approve_votes}
    votes = list(distinct.values())
    seniors = [v for v in votes if "senior_reviewer" in v.roles]
    ok = len(votes) >= required and len(seniors) >= min_senior
    return ok, max(0, required - len(votes)), max(0, min_senior - len(seniors))
