from __future__ import annotations

import pytest
from insurer_crew.runtime import Settings


@pytest.fixture
def settings() -> Settings:
    return Settings(crew_max_concurrency=2, crew_queue_depth=2, crew_request_timeout=5)
