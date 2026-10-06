from typing import Any

import pytest
from sample_claim import valid_submission


@pytest.fixture
def submission() -> dict[str, Any]:
    return valid_submission()
