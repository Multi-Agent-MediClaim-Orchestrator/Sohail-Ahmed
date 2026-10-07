"""Print a development bearer token for insurer-api (dev auth mode only).

    python scripts/make_dev_token.py reviewer approver      # roles as arguments
"""

from __future__ import annotations

import sys

sys.path.insert(0, "insurer/api")
from app.security.auth import make_dev_token  # noqa: E402

if __name__ == "__main__":
    roles = sys.argv[1:] or ["reviewer"]
    print(make_dev_token(f"dev-{roles[0]}", roles))
