"""Single-line JSON logs; tokens and Authorization headers are scrubbed (doc 02 §6)."""

from __future__ import annotations

import json
import logging
import re
import time
import uuid
from typing import Any

from claim_contract.middleware import request_id_var

_SCRUB = re.compile(
    r"(Bearer\s+)[A-Za-z0-9._\-]+|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]*"
)


def scrub(text: str) -> str:
    return _SCRUB.sub(lambda m: (m.group(1) or "") + "[REDACTED]", text)


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        data: dict[str, Any] = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "msg": scrub(record.getMessage()),
            "trace_id": request_id_var.get(),
        }
        for k in ("method", "path", "status", "duration_ms", "user_id"):
            if hasattr(record, k):
                data[k] = getattr(record, k)
        if record.exc_info:
            data["exc"] = scrub(self.formatException(record.exc_info))
        return json.dumps(data, default=str)


def setup_logging(level: str = "INFO") -> None:
    h = logging.StreamHandler()
    h.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [h]
    root.setLevel(level)


class RequestIdMiddleware:
    """Outermost: sets X-Request-ID / trace id, logs one access line (never bodies or tokens)."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.log = logging.getLogger("app.access")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        hdr = {k.decode().lower(): v.decode() for k, v in scope["headers"]}
        rid = hdr.get("x-request-id") or str(uuid.uuid4())
        if not re.match(r"^[A-Za-z0-9._-]{1,64}$", rid):
            rid = str(uuid.uuid4())
        request_id_var.set(rid)
        t0, status = time.perf_counter(), 500
        if len(hdr.get("authorization", "")) > 8192:
            await send(
                {
                    "type": "http.response.start",
                    "status": 431,
                    "headers": [(b"content-type", b"application/problem+json")],
                }
            )
            await send(
                {
                    "type": "http.response.body",
                    "body": json.dumps(
                        {
                            "type": "https://claims.local/errors/bad_request",
                            "title": "Header too large",
                            "status": 431,
                            "code": "bad_request",
                            "detail": "Authorization header too large",
                            "trace_id": rid,
                        }
                    ).encode(),
                }
            )
            return

        async def send_wrapper(m: dict[str, Any]) -> None:
            nonlocal status
            if m["type"] == "http.response.start":
                status = m["status"]
                m = {
                    **m,
                    "headers": list(m.get("headers", []))
                    + [
                        (b"x-request-id", rid.encode()),
                        (b"x-content-type-options", b"nosniff"),
                        (b"referrer-policy", b"no-referrer"),
                    ],
                }
            await send(m)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            from app.core import metrics

            metrics.observe(
                scope["method"], metrics.route_label(scope), status, time.perf_counter() - t0
            )
            self.log.info(
                "request",
                extra={
                    "method": scope["method"],
                    "path": scope["path"],
                    "status": status,
                    "duration_ms": round((time.perf_counter() - t0) * 1000, 1),
                },
            )
