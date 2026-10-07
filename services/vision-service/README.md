# vision-service

`make run-vision` (port 8300, localhost). `POST /v1/quality {document_id, url}` returns hospital-api's quality body
`{quality_score, flags, has_required_stamp}`; `POST /v1/analyze` returns the full report (per-page metrics, stamps,
required/missing kinds, escalation). Classical OpenCV detection (blue/purple/red ink blobs, round seals, signature
strokes), tesseract OCR of the crop, fuzzy match against `GET /v1/internal/hospitals`. Bills must carry the hospital
stamp (config in `vision/analyze.py: EXPECT`). Escalation uses the LOCAL vision model only, is budgeted per document
and per day, and only ever asks a human to confirm.
