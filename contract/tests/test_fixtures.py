import json
import pathlib

import pytest
from claim_contract.errors import from_validation_error
from claim_contract.models import ClaimSubmission
from pydantic import ValidationError

FIX = pathlib.Path(__file__).parent / "fixtures"
VALID = sorted((FIX / "valid").glob("*.json"))
EXPECTED = json.loads((FIX / "invalid" / "_expected.json").read_text())


@pytest.mark.parametrize("path", VALID, ids=lambda p: p.stem)
def test_valid_fixtures(path: pathlib.Path) -> None:
    c = ClaimSubmission.model_validate_json(path.read_text())
    assert ClaimSubmission.model_validate_json(c.model_dump_json()) == c  # round trip


@pytest.mark.parametrize(("name", "code"), sorted(EXPECTED.items()))
def test_invalid_fixtures(name: str, code: str) -> None:
    with pytest.raises(ValidationError) as ei:
        ClaimSubmission.model_validate_json((FIX / "invalid" / f"{name}.json").read_text())
    assert from_validation_error(ei.value).code == code


def test_fixture_counts_match_spec() -> None:
    assert len(VALID) == 5 and len(EXPECTED) == 15
