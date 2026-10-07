from evalh.tune_t_auto import recommend, sweep


def test_known_rate():
    rows = [{"payable": 1000 * i, "gates_pass": True, "correct": i != 30} for i in range(1, 101)]
    rows.append({"payable": 1, "gates_pass": False, "correct": False})  # failed gate: never auto
    t = {r["t_auto"]: r for r in sweep(rows)}
    assert t[20_000]["false_approve_rate"] == 0.0 and t[20_000]["auto_count"] == 20
    assert abs(t[50_000]["false_approve_rate"] - 1 / 50) < 1e-9
    assert recommend(sweep(rows)) == 20_000
    assert recommend(sweep([{"payable": 5, "gates_pass": True, "correct": False}])) is None
