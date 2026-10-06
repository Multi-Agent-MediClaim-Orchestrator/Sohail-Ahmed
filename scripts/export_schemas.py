"""Write contract/openapi/claims-v1.yaml and contract/openapi/schemas/*.json from the models.
`--check` exits non-zero if the checked-in files are stale (used by tests and CI)."""

import json
import pathlib
import sys

import yaml
from claim_contract import openapi

OUT = pathlib.Path("contract/openapi")


def render() -> dict[str, str]:
    files = {"claims-v1.yaml": yaml.safe_dump(openapi.build(), sort_keys=False, width=100)}
    for name, schema in openapi.schemas().items():
        files[f"schemas/{name}.json"] = json.dumps(schema, indent=2, sort_keys=True) + "\n"
    return files


if __name__ == "__main__":
    files = render()
    if "--check" in sys.argv:
        stale = [
            n for n, c in files.items() if not (OUT / n).exists() or (OUT / n).read_text() != c
        ]
        print("stale:", stale) if stale else print("schemas up to date")
        sys.exit(1 if stale else 0)
    (OUT / "schemas").mkdir(parents=True, exist_ok=True)
    for n, c in files.items():
        (OUT / n).write_text(c)
    print(f"wrote {len(files)} files to {OUT}")
