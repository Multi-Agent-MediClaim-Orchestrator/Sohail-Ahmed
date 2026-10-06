import pathlib

import yaml
from openapi_spec_validator import validate

ROOT = pathlib.Path(__file__).resolve().parents[2]
SPEC = ROOT / "contract/openapi/claims-v1.yaml"
EXPECTED_OPS = {
    "submitClaim",
    "getClaimStatus",
    "supplementDocuments",
    "listQueries",
    "respondToQuery",
    "withdrawClaim",
    "callbackStatus",
    "callbackQuery",
    "callbackDecision",
    "callbackSettlement",
    "refreshDocumentUrl",
    "health",
    "contractInfo",
}


def test_openapi_valid_and_complete() -> None:
    doc = yaml.safe_load(SPEC.read_text())
    validate(doc)
    ops = {op["operationId"] for p in doc["paths"].values() for op in p.values()}
    assert ops == EXPECTED_OPS
    assert {"ClaimSubmission", "Decision", "ProblemDetail"} <= set(doc["components"]["schemas"])


def test_checked_in_schemas_are_not_stale() -> None:
    import sys

    sys.path.insert(0, str(ROOT / "scripts"))
    import export_schemas  # type: ignore[import-not-found]

    stale = [
        n
        for n, c in export_schemas.render().items()
        if not (ROOT / "contract/openapi" / n).exists()
        or (ROOT / "contract/openapi" / n).read_text() != c
    ]
    assert not stale, f"run `make schemas`: {stale}"
