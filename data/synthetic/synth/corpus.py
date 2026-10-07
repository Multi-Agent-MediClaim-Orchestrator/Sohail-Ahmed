from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path

from synth import lint
from synth.archetypes import RECIPES
from synth.case import GENERATOR_VERSION, build_case, pick_recipe


def write_case(out: Path, built: dict) -> None:  # type: ignore[type-arg]
    d = out / built["case_id"]
    for rel, data in built["files"].items():
        p = d / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)


def build_corpus(out: Path, n: int, seed: int, only: str | None = None) -> dict:  # type: ignore[type-arg]
    out.mkdir(parents=True, exist_ok=True)
    counts: Counter[str] = Counter()
    index = []
    ids_by_recipe: dict[str, list[str]] = {}
    for i in range(n):
        r = RECIPES[only] if only else pick_recipe(seed, i)
        b = build_case(seed, i, r)
        write_case(out, b)
        counts[r.id] += 1
        ids_by_recipe.setdefault(r.id, []).append(b["case_id"])
        index.append(
            {
                "case_id": b["case_id"],
                "archetype": r.id,
                "split": "dev" if i % 10 < 6 else "test" if i % 10 < 9 else "golden_reserved",
            }
        )
    bad = lint.scan_dir(out)
    if bad:
        raise SystemExit(f"lint failed: real-looking identifiers in {bad[:3]}")
    manifest = {
        "generator_version": GENERATOR_VERSION,
        "seed": seed,
        "n": n,
        "archetype_counts": dict(counts),
        "cases": index,
    }
    (out / "corpus_manifest.json").write_text(json.dumps(manifest, indent=1))
    return manifest


def freeze_golden(out: Path, golden: Path) -> dict[str, str]:
    """One case per archetype, frozen by sha256: changing the generator must not change these files."""
    m = json.loads((out / "corpus_manifest.json").read_text())
    seen: set[str] = set()
    hashes: dict[str, str] = {}
    for c in m["cases"]:
        if c["archetype"] in seen:
            continue
        seen.add(c["archetype"])
        for f in sorted((out / c["case_id"]).rglob("*")):
            if f.is_file():
                hashes[f"{c['case_id']}/{f.relative_to(out / c['case_id'])}"] = hashlib.sha256(
                    f.read_bytes()
                ).hexdigest()
    golden.mkdir(parents=True, exist_ok=True)
    (golden / "manifest.json").write_text(json.dumps(hashes, indent=1, sort_keys=True))
    return hashes


def main() -> None:
    ap = argparse.ArgumentParser(prog="synth")
    ap.add_argument("--out", default="data/synthetic/out")
    ap.add_argument("-n", type=int, default=50)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--archetype")
    ap.add_argument("--golden", action="store_true")
    a = ap.parse_args()
    m = build_corpus(Path(a.out), a.n, a.seed, a.archetype)
    print(f"{m['n']} cases: {m['archetype_counts']}")
    if a.golden:
        h = freeze_golden(Path(a.out), Path("data/synthetic/golden"))
        print(f"golden manifest: {len(h)} files")


if __name__ == "__main__":
    main()
