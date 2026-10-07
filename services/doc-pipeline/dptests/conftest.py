import sys
from pathlib import Path

import pytest
from dp_helpers import BILL, make_pdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


@pytest.fixture
def bill_pdf() -> bytes:
    return make_pdf(BILL)
