# 04-03 — vision-service (image quality, stamps, OCR, escalation)

Owner: **Dev A**. Port 8300. FastAPI + ONNX Runtime (CPU) + OpenCV + PaddleOCR. Status: PROPOSED where marked.

## 0. Table of contents
1. Goal · 2. Inputs/Outputs · 3. Data model · 4. API · 5. Build tasks · 6. Key logic · 7. Config · 8. Edge cases · 9. Tests · 10. Acceptance · 11. Dependencies · 12. Kickoff prompt

## 1. Goal
Cheap, deterministic-first vision checks on document page images:
1. **Legibility:** is the page readable (blur, skew, brightness, contrast, cropped edges, resolution)?
2. **Stamps/signatures:** is a hospital stamp / doctor stamp / signature / seal present, what does the stamp say, and does it match the hospital registry?
3. **Escalation:** use a vision-capable cloud LLM ONLY when code-based results are uncertain, under a strict budget and masking.

Why it matters:
- Hospital completeness (principle 7): a missing stamp or blurry page becomes a **needs-info request**, not a rejection.
- Insurer authenticity: stamp present/registry match feeds the authenticity score; vision never decides alone.
- Doc-pipeline: page quality feeds the `bad_page_quality` gate.

Design stance: deterministic CV (classical + a small ONNX detector) first; LLM is a tie-breaker that can only add `needs_human_confirm`, never override code (P1, P5).

## 2. Inputs / Outputs
**In:** page images from MinIO (`{case_id}/{doc_id}/pages/{n}.png` written by doc-pipeline) referenced by `{system, bucket, key}` or a list of page keys; optionally `expect` (list of stamp kinds required for this doc type) and `doc_type`.
**Out:** `PageQuality[]`, `StampFinding[]`, `VisionReport`; optional escalation result. Reports are cached at `{case_id}/{doc_id}/vision.json` in the owning bucket (read/write by `vision_svc` only on that filename — add statement to policy `vision-ro.json`: PutObject on `*/vision.json`).

Callers: hospital-api completeness engine (via crew or n8n), hospital-crew Document Intake Agent, doc-pipeline (quality only), insurer-crew Authenticity agent.

## 3. Data model (`services/vision-service/app/models.py`)
```python
class PageQuality(BaseModel):
    page: int
    width: int
    height: int
    dpi_est: int | None  # from metadata or A4 heuristic (px/8.27in)
    blur_score: float  # variance of Laplacian on normalised gray
    skew_deg: float  # +/- degrees
    brightness: float  # mean gray 0..255
    contrast: float  # (p95 - p5)/255
    cropped_edges: bool
    text_density: float  # fraction of dark connected components in text-like size range
    blank_page: bool
    legible: bool
    reasons: list[
        str
    ]  # blurry, low_contrast, too_dark, too_bright, skewed, cropped, low_resolution, blank


class StampFinding(BaseModel):
    page: int
    bbox: tuple[int, int, int, int]  # px x0,y0,x1,y1 in original image coords
    kind: Literal["hospital_stamp", "doctor_stamp", "signature", "seal"]
    detector: Literal["onnx", "classical", "llm"]
    det_conf: float
    ocr_text: str | None
    ocr_conf: float | None
    matches_registry: bool | None
    registry_hospital_id: str | None
    registry_score: float | None
    ink_color: Literal["blue", "purple", "red", "black", "other"] | None


class Escalation(BaseModel):
    page: int
    reason: str
    alias: str
    result: dict | None  # {"present": bool, "kind": str, "text": str|None}
    needs_human_confirm: bool  # always True for absence verdicts
    cost_tokens: int | None


class VisionReport(BaseModel):
    doc_id: UUID
    system: Literal["hospital", "insurer"]
    pages: list[PageQuality]
    stamps: list[StampFinding]
    required_stamp_present: bool | None  # None when 'expect' not provided
    missing_kinds: list[str]
    all_pages_legible: bool
    escalated: bool
    escalations: list[Escalation]
    needs_human_confirm: bool
    version: str
    timings_ms: dict[str, int]
```

## 4. API
| Method | Path | Description |
|---|---|---|
| POST | `/v1/quality` | `{system,bucket,keys[]}` → `list[PageQuality]` |
| POST | `/v1/stamps` | detect + OCR + registry match → `list[StampFinding]` |
| POST | `/v1/analyze` | quality + stamps + conditional escalation → `VisionReport` |
| POST | `/v1/escalate` | force LLM vision check `{system,bucket,key,bbox?,question,case_id}` (budget applies) |
| POST | `/v1/ocr` | OCR a crop/region (utility for crews) |
| GET | `/v1/health`, `/v1/version` | models loaded, versions |
Auth: JWT with `svc-crew|svc-n8n|svc-internal`, `system` claim = request `system`.

Examples:
```json
POST /v1/analyze
{"system":"hospital","case_id":"5b1f...","doc_id":"c0a8...","bucket":"hospital-docs",
 "keys":["5b1f.../c0a8.../pages/1.png","5b1f.../c0a8.../pages/2.png"],
 "doc_type":"final_bill","expect":["hospital_stamp"]}
```
Response 200:
```json
{"doc_id":"c0a8...","pages":[
  {"page":1,"blur_score":212.4,"skew_deg":1.2,"contrast":0.61,"brightness":181,"legible":true,"reasons":[]},
  {"page":2,"blur_score":58.0,"skew_deg":2.0,"contrast":0.55,"legible":false,"reasons":["blurry"]}],
 "stamps":[{"page":2,"kind":"hospital_stamp","detector":"onnx","det_conf":0.82,"bbox":[120,1650,640,1900],
            "ocr_text":"CITY CARE HOSPITAL REG NO 12345","ocr_conf":0.77,"matches_registry":true,"registry_hospital_id":"H-001","registry_score":93}],
 "required_stamp_present":false,"missing_kinds":["signature"],"all_pages_legible":false,
 "escalated":true,"escalations":[{"page":2,"reason":"expected_signature_not_found_page_legible","alias":"vision-cloud",
   "result":{"present":false},"needs_human_confirm":true}],
 "needs_human_confirm":true,"version":"1.0.2"}
```
Errors: 404 `object_not_found`, 413 `image_too_large` (> 40 MP), 415 `unsupported_image`, 429 `escalation_budget_exhausted` only for `/v1/escalate` (analyze degrades gracefully and sets `escalation_skipped_budget` in reasons), 503 `model_not_loaded`.

## 5. Build tasks

1. **Scaffold** `services/vision-service/`: `pyproject.toml` (fastapi, uvicorn, onnxruntime, opencv-python-headless, numpy, paddlepaddle (CPU), paddleocr, pillow, rapidfuzz, httpx, boto3, redis, presidio-image-redactor, pydantic v2), `Dockerfile` (python 3.12-slim, `libgl1-mesa-glx` not needed with headless; models cached under `/models`), `app/main.py`, `app/settings.py`, `app/auth.py` (same helper pattern as infra doc). DoD: `/v1/health` OK; image < 2.5 GB.
2. **Image IO** `app/imageio.py`: load from MinIO (`vision_svc` user), EXIF-orient, convert to RGB/gray, downscale rule (longest side ≤ 2000 px for detection, original kept for OCR crops), reject > 40 MP.
3. **Quality metrics** `app/quality.py` — implement each metric precisely:
   - `blur_score = cv2.Laplacian(gray_norm, cv2.CV_64F).var()` computed on a 1600 px-wide normalised copy (so scores are comparable across resolutions).
   - `skew_deg`: Canny → probabilistic Hough lines (min length 0.3*width) → median angle of near-horizontal lines (|angle| < 20°); fallback `cv2.minAreaRect` of text-pixel mask; clamp ±45.
   - `contrast = (p95 - p5)/255` of gray histogram; `brightness = mean`.
   - `cropped_edges`: dark-pixel fraction in the outer 1.5% border strips on each side > 0.02 while text-density in the adjacent 5% band > 0.05 (text running off the page).
   - `text_density`/`blank_page`: connected components on binarised page (Sauvola threshold) filtered by size; `blank_page` if density < 0.002.
   - `dpi_est`: from PNG pHYs/EXIF if present; else `width_px / 8.27` for A4-like aspect (|aspect-1.414|<0.1).
   - `legible` rule (config, PROPOSED defaults): `blur_score >= VISION_BLUR_MIN (80)` AND `contrast >= 0.20` AND `abs(skew) <= 8` AND `brightness in [40, 235]` AND NOT `blank_page`; `cropped_edges` and `dpi<150` are reasons but only make `legible=False` when combined with blur<120 (soft rule).
4. **Deskew/perspective** `app/geometry.py`: for photographed pages find the largest quadrilateral contour (area > 40% of image) → `getPerspectiveTransform` to a flat page; if not found, rotate by `-skew_deg` when |skew| ≤ 15, else keep original and mark `skewed`. Quality metrics are computed on the corrected image; stamp bboxes are mapped back to original coords with the inverse transform.
5. **Classical stamp detector** `app/stamp_cv.py`:
   - Convert to HSV; masks: blue (H 95-135, S>60, V>50), purple (H 125-160), red (H 0-10 ∪ 170-180, S>80). Morphological close (5×5) then open (3×3).
   - Connected components → contour filtering: area 0.2%-12% of page; circularity `4πA/P² > 0.55` (round seals) OR rectangle fill ratio > 0.5 with aspect 1.2-5 (rect stamps).
   - Ink density: stamps are sparse text — require 8-45% foreground pixels inside bbox.
   - Signature heuristic: dark/blue thin-stroke component with high stroke-length/area ratio, no regular text baseline, located within lower 40% of page or next to words "Signature"/"Sign" (OCR proximity search); bbox area 0.1%-4%.
   - Output kind guess: round → `seal`/`hospital_stamp`; rectangle with ≥ 3 text lines → `hospital_stamp`; has "Dr." text in OCR → `doctor_stamp`.
   - `det_conf` = weighted score (shape 0.4, ink density 0.2, colour 0.2, text-lines 0.2) clipped 0..1.
6. **ONNX detector** `app/stamp_onnx.py`: YOLOv8n exported to ONNX (input 640×640, letterbox, NMS in post-processing with IoU 0.5, conf 0.25 candidate floor). Classes: `hospital_stamp, doctor_stamp, signature, seal`. Model path `STAMP_MODEL_PATH`; if missing, service runs classical-only and reports `detector_mode: classical` in `/v1/version`. Training assets: `training/make_synthetic_stamps.py` (random hospital names, round/rect/oval templates, colours, rotation ±25°, partial occlusion by text, JPEG compression, paper textures, photocopy noise; overlays on ReportLab pages from the synthetic corpus; writes YOLO labels), `training/train.md` (ultralytics command `yolo detect train data=stamps.yaml model=yolov8n.pt imgsz=640 epochs=60`, export `format=onnx opset=17 simplify=True`), `training/eval.py` (precision/recall on a held-out set). Train offline on CPU (≈ 1-2 h for 3k images at nano scale) or a free GPU notebook; commit only the ONNX file via git-lfs or MinIO artifact (≤ 15 MB).
7. **Detection fusion** `app/fusion.py`: run both detectors; merge boxes with IoU > 0.4 (keep ONNX box, record both confidences). Fused conf: both agree → `max(onnx, cv)+0.05`; only ONNX → `onnx*0.95`; only classical → `cv*0.8`. Disagreement flag when one detector finds an object the other does not within the expected region (used by escalation).
8. **OCR** `app/ocr.py`: PaddleOCR (`use_angle_cls=True`, langs `en` and `hi`; load models once, lazily per language). For stamp crops: pad 8 px, upscale ×2 if short side < 120, try rotations 0/90/180/270 plus ±15° for round stamps (text on arcs: unwrap with `cv2.warpPolar` and OCR the strip), pick the highest mean conf; `ocr_conf` = mean line conf. Normalise: uppercase, collapse spaces, strip punctuation noise.
9. **Registry match** `app/registry.py`: fetch hospital registry from `hospital-api GET /internal/hospitals` (fields `id, name, aliases[], reg_no, city`) using the `hospital-internal` client; cache 10 min. Match score = max of `rapidfuzz.fuzz.token_set_ratio` on name/aliases and exact match on `reg_no` substring (reg no exact ⇒ score 100). `matches_registry = score ≥ 85`. For insurer side: registry endpoint on insurer-api (`/internal/network-hospitals`, Dev B doc 03) — configured by `REGISTRY_URL`; same schema (document the schema in the contract folder `contract/openapi/internal-registry.yaml`, PROPOSED).
10. **Escalation** `app/escalate.py`: details in §6.3. Image redaction with Presidio Image Redactor + OCR text boxes: blur boxes whose OCR text matches PII recognizers (names after "Patient", phone, Aadhaar, addresses) BEFORE sending. Request via llm-gateway alias `vision-cloud` (Gemini Flash vision), JSON schema output `{present:boolean, kind:string, text:string|null, notes:string}`. Budget accounting in Redis `vision:esc:{case_id}` (INCR with 7 d TTL) and per-doc counter in the report.
11. **Orchestrator** `app/analyze.py` (§6.1) with result cache keyed `(image sha256 list, expect, version)`; writes `vision.json`.
12. **Metrics/observability** `app/obs.py`: histograms per stage; counters for detector agreement, escalation causes; Langfuse trace for each escalation (masked image hash only, not the image).
13. **Tests + fixtures** per §9, including the synthetic stamp generator reused from training.

## 6. Key logic

### 6.1 Orchestration
```python
async def analyze(req) -> VisionReport:
    imgs = [await imageio.load(k) for k in req.keys]
    qs = [quality.measure(geometry.correct(i)) for i in imgs]
    stamps = []
    for i, (img, q) in enumerate(zip(imgs, qs), start=1):
        if q.blank_page:
            continue  # nothing to find
        det_onnx = stamp_onnx.detect(img) if onnx_loaded else []
        det_cv = stamp_cv.detect(img)
        dets = fusion.merge(det_onnx, det_cv)
        for d in dets:
            ocr = ocr_stamp(crop(img, d.bbox), kind=d.kind)
            m = registry.match(ocr.text) if d.kind in {"hospital_stamp", "seal"} else None
            stamps.append(
                StampFinding(page=i, **d.dict(), ocr_text=ocr.text, ocr_conf=ocr.conf, **(m or {}))
            )
    present = presence(stamps, req.expect, min_conf=DET_CONF_MIN)  # {kind: bool}
    esc = []
    for trig in escalation_triggers(qs, stamps, req, present):
        if budget.allows(req.case_id, req.doc_id):
            esc.append(await escalate(trig))
        else:
            reasons.add("escalation_skipped_budget")
    return assemble(...)
```
`presence(stamps, expect, min_conf)`: kind K is present if any stamp with `kind == K` and `det_conf >= 0.5` (and for `hospital_stamp`: either `matches_registry` is True or `ocr_text` is non-empty with `ocr_conf >= 0.5`, because a stamp from an unknown hospital is still "a stamp" but is flagged for the insurer authenticity check). `required_stamp_present = all(present[k] for k in expect)` or `None` when `expect` empty.

### 6.2 Which pages/regions are checked
By `doc_type` (config `stamp_required.yaml`, PROPOSED):
| DocType | Expected | Where |
|---|---|---|
| `prescription` | doctor_stamp or signature (not required to carry a hospital stamp) | each page with medicines |
| `pharmacy_bill`, `procedure_bill`, `final_bill`, `itemised_bill` | **hospital_stamp REQUIRED** (user decision: bills must be stamped by the hospital) | last page (all pages if not found on the last) |
| `discharge_summary` (optional doc) | hospital_stamp, doctor_stamp or signature | last page |
| `lab_report`, `radiology_report` | hospital_stamp or signature | last page |
| `preauth_approval` | insurer stamp optional | none |
| `claim_form` (optional doc) | signature | last page |
| others | none | none |
"or" combos are modelled as `expect_any=[[...],[...]]`; the API accepts `expect` (all) and `expect_any` (at least one group). Search is limited to the expected page(s) first (last page default; all pages if nothing found on the last — avoids missing a stamp that sits on page n-1 of multi-page docs). Page choice is deterministic from the page count and doc type, not from the LLM.

### 6.3 Escalation rules
A page-level escalation trigger fires when ANY of:
| Trigger | Condition |
|---|---|
| `uncertain_detection` | an expected-kind detection with `0.35 <= det_conf < 0.65` |
| `detector_disagreement` | ONNX and classical disagree on an expected region |
| `low_ocr_conf` | expected `hospital_stamp` found but `ocr_conf < 0.6` |
| `expected_not_found_legible` | expected kind absent on a **legible** page, doc_type requires it (reduces false "missing stamp" blockers) |
| `registry_mismatch_close` | registry score in [70, 85) |
Not triggered for illegible pages (cheaper action: ask for re-scan).
Budget (PROPOSED): ≤ 2 escalations per document, ≤ 6 per case, ≤ `ESC_DAILY_CAP` (default 60) overall to respect free-tier quotas; counters in Redis. When the gateway returns 429 after fallback, escalation is skipped with reason `escalation_unavailable`.
Escalation call:
1. Crop the candidate region with 15% padding (or the bottom 35% of the page for `expected_not_found_legible`).
2. Redact PII in the crop (Presidio image redactor on OCR boxes), downscale ≤ 1024 px, JPEG q85.
3. Prompt (versioned `prompts/vision_stamp.md`): "Is there a hospital stamp, doctor stamp, or handwritten signature in this image? Return JSON schema." Temperature 0.
4. Parse result; **cross-check in code:** the claimed stamp text, if returned, is fuzzy-matched against registry; LLM text never marks `matches_registry` true by itself — only a match between the LLM-read text and the registry sets `matches_registry` (score ≥ 85) and `detector="llm"`.
5. Output sets `Escalation.needs_human_confirm=True` for absence verdicts and for any presence verdict with `matches_registry != True`. The completeness engine treats LLM-only presence as "present, pending confirm" (info), and LLM-confirmed absence as a **needs-info** item, never a rejection.

### 6.4 How callers interpret results
- Hospital completeness (04 completeness doc): `required_stamp_present is False` ⇒ item `missing_stamp` severity `blocker` with action "upload a stamped copy"; `needs_human_confirm=True` ⇒ officer must tick confirm; `all_pages_legible=False` ⇒ `illegible_document` blocker listing pages.
- Insurer Authenticity agent: `matches_registry False` or stamp absent ⇒ lowers authenticity score by configured weights (insurer doc 08), never rejects automatically.

### 6.5 Caching and determinism
Same images + same version ⇒ identical report (no randomness; ONNX deterministic; thresholds from config). Version string includes model hash and config hash. LLM escalation results are cached per `(crop sha256, prompt version)` for 7 days.

## 7. Config
| Var | Default | Notes |
|---|---|---|
| `VISION_BLUR_MIN` | 80 | on 1600 px-wide normalised copy |
| `VISION_CONTRAST_MIN` | 0.20 | |
| `VISION_SKEW_MAX` | 8 | degrees |
| `VISION_BRIGHT_MIN/MAX` | 40/235 | |
| `STAMP_MODEL_PATH` | `/models/stamp_yolov8n.onnx` | optional |
| `DET_CONF_MIN` | 0.5 | presence threshold |
| `OCR_LANGS` | en,hi | |
| `ESC_PER_DOC` / `ESC_PER_CASE` / `ESC_DAILY_CAP` | 2 / 6 / 60 | |
| `LLM_GATEWAY_URL`, `LLM_GATEWAY_KEY` | | alias `vision-cloud` |
| `REGISTRY_URL`, `REGISTRY_CLIENT_ID/SECRET` | | per system |
| `VISION_WORKERS` | 1 | models loaded once |
| `MAX_IMAGE_MP` | 40 | |
| `ONNX_THREADS` | 4 | intra-op threads |
Thresholds may be overridden at runtime from hospital `confidence_gates` config (`vision.*` keys) fetched like doc-pipeline does (cached 60 s).

## 8. Edge cases
| # | Case | Behaviour |
|---|---|---|
| 1 | Photo of a printed page at an angle | perspective correction; metrics on corrected image; if corners not found keep original and add reason `skewed` if skew > max |
| 2 | Stamp overlapping text | OCR noisy → `low_ocr_conf` → escalate or ask for a clearer scan; bbox still reported |
| 3 | Faint photocopied stamp | HSV segmentation fails (low saturation); ONNX may still fire; disagreement → escalate |
| 4 | Digital PDF with typed "Digitally signed by" text | doc-pipeline text finds it; caller passes `expect_any` satisfied by text evidence (completeness engine rule); vision skips |
| 5 | Very large images | downscale for detection (≤ 2000 px), OCR on original-resolution crops |
| 6 | Memory | single worker; models loaded once; `mem_limit: 2g`; OCR models lazily loaded (en first) |
| 7 | Non-document images (selfie, photo of a building) | quality computed; no stamps; `text_density` very low → reason `not_a_document`; no error |
| 8 | Black-and-white scans (stamp in black ink) | colour masks fail; ONNX + structure heuristics; `ink_color="black"`; signature detection weaker → rely on escalation |
| 9 | Stamp partially cut by page edge | detection allowed with `cropped_edges` flag; conf lowered 0.1 |
| 10 | Two stamps (hospital + doctor) overlapping | NMS keeps both when classes differ |
| 11 | Handwritten text mistaken for signature | signature requires location prior (lower page) OR nearby "signature" label OR ≥ 0.7 ONNX conf |
| 12 | Hindi stamp text | PaddleOCR hi pack; registry aliases include Hindi names when available |
| 13 | Registry endpoint down | `matches_registry=None`, report flag `registry_unavailable`; no escalation based on registry |
| 14 | Escalation budget exhausted | skip with reason; result stays code-only; completeness shows "stamp unclear—officer review" |
| 15 | Gateway 429/timeouts | skip escalation; do not retry beyond gateway's own fallback |
| 16 | PII in stamp area (doctor name) | doctor names on stamps are not patient PII; redaction targets patient fields only (labels list) — documented decision PROPOSED; redactor config in `config/redact_labels.yaml` |
| 17 | Rotated page 90/180° | OCR angle classifier plus page-level orientation by text-line direction; stamps detected after rotation fix |
| 18 | Multi-frame TIFF | each frame treated as a page by doc-pipeline's render stage (not here) |
| 19 | Concurrent analyze for the same doc | Redis lock `lock:vision:{doc_id}` 60 s; second caller reads cache |
| 20 | Corrupt/unsupported image | 415 `unsupported_image`; other pages still processed in batch calls (per-page errors reported) |

## 9. Tests

### 9.1 Fixtures (generated, committed as small PNGs + JSON ground truth under `services/vision-service/tests/fixtures/`, bulk set generated by the synthetic generator in 05-01)
- 40 quality pages: clean, blurred (Gaussian σ 1.5/3/5), rotated (2°, 6°, 12°), dark, overexposed, low-contrast, cropped, blank, low-res (100 dpi). Ground truth `legible` and `reasons`.
- 60 stamped docs: stamp kinds × colours × placements × degradations (photocopy, JPEG q30, rotation, overlap with text), 20 negatives with no stamp, 10 with signature only, 10 with a stamp from a hospital not in the registry.
- OCR set: 100 stamp crops with known text.

### 9.2 Metrics targets
| Metric | Target |
|---|---|
| Quality `legible` classification accuracy | ≥ 0.95; false-legible rate on blurred σ≥3 ≤ 0.05 |
| Skew estimation error | median ≤ 1°, p95 ≤ 3° |
| Stamp detection recall / precision (fused) | ≥ 0.90 / ≥ 0.90 |
| Classical-only recall (fallback mode) | ≥ 0.75 |
| Stamp OCR exact-match | ≥ 0.80; normalised token F1 ≥ 0.90 |
| Registry match accuracy (right hospital / unknown) | ≥ 0.95 |
| Signature presence detection recall | ≥ 0.80 (escalation covers the rest) |
| Escalation rate on clean set | ≤ 0.10 |
| Latency quality+stamps per page (CPU, 8 vCPU) | ≤ 3 s; p95 ≤ 5 s |
| Startup time | < 40 s |

### 9.3 Unit tests
| Area | Cases |
|---|---|
| Blur metric | monotonic with blur σ; resolution invariance within ±15% |
| Skew | synthetic rotated text pages ±1..15° |
| Legibility rule | table-driven matrix over (blur, contrast, skew, brightness, blank) |
| Stamp CV | circles, rectangles, ink densities, false-positive on coloured logos/ highlights |
| Fusion | overlapping/disjoint boxes, conf formulas |
| Presence | `expect` and `expect_any` logic; last-page-first search, fallback to other pages |
| Registry | exact reg_no, fuzzy name, alias, below threshold, endpoint failure |
| Escalation triggers | each trigger with positive/negative |
| Budget | per-doc, per-case, daily; Redis outage → conservative deny |
| Redaction | patient name/phone boxes blurred before send (compare pixel regions) |
| Prompt safety | gateway mock records request: image dims ≤ 1024, no EXIF, no original filename, no case/patient ids in text prompt |

### 9.4 Integration
- Full `/v1/analyze` against MinIO + mock registry + mock gateway.
- Cross-system: insurer token cannot read hospital pages (403).
- Fault injection: ONNX model missing (classical-only), PaddleOCR load failure (reports `ocr_unavailable`, does not crash), gateway 500.
- Determinism: same input twice yields byte-identical JSON (excluding timings).

### 9.5 Training/eval scripts
`training/eval.py` prints per-class precision/recall/mAP@0.5 on the held-out set; CI fails the "model update" PR if recall drops > 3 pts vs the committed baseline (`training/baseline.json`).

## 10. Acceptance criteria
- [ ] §9.2 targets met on fixtures (classical-only mode meets its reduced target).
- [ ] Service starts in < 40 s with `mem_limit: 2g`, steady-state RSS < 1.6 GB.
- [ ] A synthetic discharge summary without a stamp yields `required_stamp_present=False`, and the hospital completeness engine raises `missing_stamp` (verified in the hospital completeness doc's integration test).
- [ ] No unredacted patient text/PII reaches the gateway mock (captured-body assertions).
- [ ] Escalation never flips a code-derived `matches_registry` and always sets `needs_human_confirm` for absence.
- [ ] Budget limits respected under a stress test (10 docs in one case → ≤ 6 escalations).
- [ ] Reports are deterministic and cached.

## 11. Dependencies
- Infra (04-01): MinIO user `vision_svc` (read pages, write `vision.json`), Redis, Keycloak roles.
- doc-pipeline (04-02): produces `pages/{n}.png`, consumes `PageQuality`.
- llm-gateway (04-04): alias `vision-cloud` with a virtual key and budget.
- Hospital API (02-dev-A docs 03/04): `/internal/hospitals` registry, completeness engine consumer; insurer-api (Dev B doc 03): `/internal/network-hospitals`.
- Synthetic data (05-01): stamp generator, ReportLab pages, ground-truth labels.

## 12. Claude Code kickoff prompt
> Read docs/implementation/04-shared-services/03-vision-service.md. Implement tasks 1-13. First the classical path (tasks 2-5, 8, 9, 11) with unit tests and fixtures, passing the classical-only metric targets; then fusion and the ONNX detector using a stub model if no trained weights exist; then escalation behind the redaction guard and the budget. Never send an unredacted image to the gateway and never let an LLM answer set `matches_registry`. Report which acceptance boxes are checked.
