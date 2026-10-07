# doc-pipeline

`make run-docpipe` (port 8200, localhost). Turns a document (presigned URL) into typed JSON passes for hospital-api.
Called by n8n flow F1: `POST /v1/parse {document_id, case_id, url}` -> `{job_id}`; `GET /v1/jobs/{id}` ->
`{state: succeeded|failed, result: {doc_type, doc_type_conf, passes[], needs_review, review_reasons, issues}}`.
Each entry of `passes` is exactly the body of hospital-api `POST /v1/internal/documents/{id}/parse`.

Stages: render (pdftotext text layer, else tesseract OCR; MinerU is used instead when its CLI is installed and
`DOCPIPE_PARSER` is `auto|mineru`) -> rule classification -> Presidio + Indian-ID recognizers (Verhoeff checksum) ->
stable-token masking (AES-GCM sealed map) -> guard -> pass A (local model, masked text) -> pass B (cloud model,
masked text, critical fields only, never for id_proof/cancelled_cheque) -> evidence check (a value not present in the
text is nulled) -> validation in Decimal -> gate. Bill lines come from code, not the model.
Confidence is the parser's (0.99 text layer, mean OCR confidence), never a model's.
