"""In-process Prometheus-style metrics (no extra dependency): request counts and latency histogram per route template,
plus outbox and queue gauges read on scrape. Labels never contain ids, users or query strings."""

from __future__ import annotations

import threading
from collections import defaultdict
from typing import Any

BUCKETS = (0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10)
_lock = threading.Lock()
_count: dict[tuple[str, str, int], int] = defaultdict(int)
_sum: dict[tuple[str, str], float] = defaultdict(float)
_hist: dict[tuple[str, str], list[int]] = defaultdict(lambda: [0] * (len(BUCKETS) + 1))


def route_label(scope: dict[str, Any]) -> str:
    r = scope.get("route")
    return (
        getattr(r, "path", None) or "unmatched"
    )  # template like /v1/cases/{case_id}, never the raw path


def observe(method: str, route: str, status: int, seconds: float) -> None:
    with _lock:
        _count[(method, route, status)] += 1
        _sum[(method, route)] += seconds
        h = _hist[(method, route)]
        for i, b in enumerate(BUCKETS):
            if seconds <= b:
                h[i] += 1
        h[-1] += 1


def reset() -> None:
    with _lock:
        _count.clear()
        _sum.clear()
        _hist.clear()


def render(extra: dict[str, float] | None = None) -> str:
    out = ["# TYPE http_requests_total counter"]
    with _lock:
        for (m, r, s), n in sorted(_count.items()):
            out.append(f'http_requests_total{{method="{m}",route="{r}",status="{s}"}} {n}')
        out.append("# TYPE http_request_duration_seconds histogram")
        for (m, r), h in sorted(_hist.items()):
            for b, v in zip(BUCKETS, h, strict=False):
                out.append(
                    f'http_request_duration_seconds_bucket{{method="{m}",route="{r}",le="{b}"}} {v}'
                )
            out.append(
                f'http_request_duration_seconds_bucket{{method="{m}",route="{r}",le="+Inf"}} {h[-1]}'
            )
            out.append(
                f'http_request_duration_seconds_sum{{method="{m}",route="{r}"}} {_sum[(m, r)]:.6f}'
            )
            out.append(f'http_request_duration_seconds_count{{method="{m}",route="{r}"}} {h[-1]}')
    for name, v in sorted((extra or {}).items()):
        out += [f"# TYPE {name} gauge", f"{name} {v}"]
    return "\n".join(out) + "\n"
