CAPS: dict[str, set[str]] = {
    "desk": {"case.create", "case.view_own", "doc.upload", "doc.request", "audit.view_own"},
    "officer": {
        "case.create",
        "case.view_all",
        "doc.upload",
        "doc.request",
        "doc.waive",
        "draft.edit",
        "claim.submit",
        "query.approve",
        "case.assign",
        "audit.view_all",
    },
    "admin": {
        "case.view_all",
        "config.edit",
        "config.publish",
        "user.manage",
        "case.assign",
        "audit.view_all",
    },
}
HUMAN_ROLES = frozenset(CAPS)
SERVICE_ROLES = frozenset({"svc-n8n", "svc-crew", "svc-internal"})
PRECEDENCE = ("admin", "officer", "desk")


def capabilities(roles: set[str] | frozenset[str]) -> list[str]:
    return sorted(set().union(*(CAPS[r] for r in roles if r in CAPS)))


def primary_role(roles: set[str] | frozenset[str]) -> str:
    return next(r for r in PRECEDENCE if r in roles)
