"""Vision tie-breaker (doc 03 §6.3): a LOCAL vision model looks at the bottom of the page when code found no stamp on a
legible page. Never cloud: the crop may contain patient details. Budgeted; the answer only ever asks a human to confirm."""

from __future__ import annotations

import base64
import io
import json
import time
from typing import Any

import httpx
import numpy as np
from PIL import Image

from vision.settings import Settings

PROMPT = ('Look at this page crop from a hospital document. Is there a hospital stamp, a doctor stamp or a handwritten signature? '
          'Reply only with JSON: {"present": true|false, "kind": "hospital_stamp|doctor_stamp|signature|none", "text": "<text on the stamp or null>"}.')  # fmt: skip


class Escalator:
    def __init__(self, s: Settings, client: httpx.AsyncClient | None = None) -> None:
        self.s, self.c = s, client or httpx.AsyncClient(timeout=180)
        self.per_doc: dict[str, int] = {}
        self.day = (time.strftime("%Y-%m-%d"), 0)

    def allowed(self, doc_id: str) -> bool:
        today = time.strftime("%Y-%m-%d")
        if self.day[0] != today:
            self.day = (today, 0)
        return (
            self.per_doc.get(doc_id, 0) < self.s.esc_per_doc and self.day[1] < self.s.esc_daily_cap
        )

    async def check(
        self, page: np.ndarray, wanted: list[str], doc_id: str
    ) -> dict[str, Any] | None:
        if not self.allowed(doc_id):
            return None
        h = page.shape[0]
        crop = Image.fromarray(page[int(h * 0.65) :])
        crop.thumbnail((1024, 1024))
        buf = io.BytesIO()
        crop.save(buf, "JPEG", quality=85)
        uri = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
        self.per_doc[doc_id] = self.per_doc.get(doc_id, 0) + 1
        self.day = (self.day[0], self.day[1] + 1)
        try:
            r = await self.c.post(f"{self.s.llm_base_url}/chat/completions", json={
                "model": self.s.vision_model, "temperature": 0, "stream": False, "response_format": {"type": "json_object"}, "reasoning_effort": "none",
                "messages": [{"role": "user", "content": [{"type": "text", "text": PROMPT}, {"type": "image_url", "image_url": {"url": uri}}]}]})  # fmt: skip
            r.raise_for_status()
            res = json.loads(r.json()["choices"][0]["message"]["content"])
        except (httpx.HTTPError, ValueError, KeyError):
            return None
        return {
            "page": 0,
            "reason": "expected_not_found_legible",
            "alias": self.s.vision_model,
            "result": {
                "present": bool(res.get("present")),
                "kind": res.get("kind"),
                "text": res.get("text"),
            },
            "needs_human_confirm": True,
        }
