import json

from evalh.run import ratio, score
from synth.corpus import build_corpus


def doc(**kw):
    base = {"doc_type": "final_bill", "doc_type_pred": "final_bill", "quality_class": "good", "legible_pred": True, "legible_expected": True,
            "stamp_expected": True, "review_reasons": []}  # fmt: skip
    return {**base, **kw}


def test_score_math_and_targets():
    rows = [{"docs": [doc(lines_expected=3, lines_pred=3, line_amounts_match=True, total_match=True, stamp_pred=True),
                      doc(doc_type_pred="other", lines_expected=3, lines_pred=2, line_amounts_match=False, total_match=False, stamp_pred=False),
                      doc(quality_class="poor", legible_expected=False, legible_pred=False, review_reasons=["low_parser_conf"], lines_expected=3, lines_pred=0, line_amounts_match=False)]}]  # fmt: skip
    m = score(rows)
    assert (
        m["classify_acc"] == ratio(2, 3) and m["line_count_exact"] == 0.5
    )  # the poor document is not scored for extraction
    assert (
        m["stamp_precision"] == 1.0
        and m["stamp_recall"] == 0.5
        and m["poor_flagged"] == 1.0
        and m["pass"]["classify_acc"] is False
    )


def test_harness_runs_end_to_end_on_a_small_corpus(tmp_path):
    import argparse
    import asyncio

    from evalh.run import main_async

    build_corpus(tmp_path / "c", 6, 7)
    rep = asyncio.run(
        main_async(
            argparse.Namespace(
                corpus=str(tmp_path / "c"), out=str(tmp_path / "r.json"), llm="none", limit=0
            )
        )
    )
    assert json.loads((tmp_path / "r.json").read_text())["metrics"]["cases"] == 6
    assert rep["metrics"]["classify_acc"] >= 0.9 and rep["metrics"]["line_amount_exact"] == 1.0
