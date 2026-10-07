import json

from evalh.stamps_eval import iou, score
from synth.stamps_hard import build_set
from vision import stamp_model, stamps


def test_hard_stamp_labels_sit_on_ink():
    import numpy as np

    for page, labels, diff in build_set(12, 3):
        a = np.asarray(page.convert("L")).astype(float)
        for lab in labels:
            x0, y0, x1, y1 = lab["bbox"]
            assert 0 <= x0 < x1 <= page.width and 0 <= y0 < y1 <= page.height
            inside = a[y0:y1, x0:x1]
            assert (inside < 200).mean() > 0.01, (diff, lab)  # something is drawn there


def test_iou():
    assert (
        iou([0, 0, 10, 10], [0, 0, 10, 10]) == 1.0 and iou([0, 0, 10, 10], [20, 20, 30, 30]) == 0.0
    )


def test_learned_detector_regression_guard_on_held_out_pages():
    """Held-out pages (seed unseen in training). Numbers are for SYNTHETIC pages: real scans are untested."""
    fo = stamp_model.load()
    data = build_set(36, 4242)
    learned = score(lambda im, pg: stamps.detect_learned(im, pg, fo), data, fo.threshold)["all"]
    classical = score(stamps.detect_classical, data)["all"]
    assert learned["page_recall"] >= 0.9 and learned["page_precision"] >= 0.9, learned
    assert learned["box_recall"] > classical["box_recall"] + 0.2, (learned, classical)
    json.dumps(learned)
