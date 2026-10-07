# Contract changelog
Joint file: both developers approve every change. Versions follow docs/implementation/01-shared-contract/01-api-contract-v1.md §11.

## 1.1.0 (PROPOSED additions, additive)
- `X-Journey-Id` header (propagated, not signed); `journey_id` on `ClaimSubmission` and audit events (excluded from the hash body).
- `POST /v1/insurer-callbacks/documents/{doc_id}/refresh-url`.
- Error codes `hospital_blacklisted`, `hospital_mismatch`, `payload_too_large`.
- `DocType` gains `prescription` and `procedure_bill` (default document requirements: prescriptions and bills).
- Insurer state machine: `ready_for_decision` may go straight to `approved|partially_approved` (system auto-approve at or below `T_auto`, audit `decision.auto_approved`).
- Package additions: Redis idempotency store, ASGI contract middleware (HMAC, rate limit, idempotency, ids), OpenAPI generator, `testing.hospital_sim`.

- Hospital state machine: `submitted -> ready_for_review` is allowed (receiver rejected the submission with a terminal 4xx, so nothing was accepted; doc 06 §8.1).
- Audit event types for claim building, submission and callbacks; `testing.insurer_sim` (reference insurer for the hospital-side tests, usable by Dev B as a contract fixture).

## 1.1.0 additions folded in from the insurer side (Dev B), all additive
- Python API: lower-case enum member access (`ClaimType.cashless`) as a lookup alias of the upper-case members; `StepName` and
  `INSURER_TO_HOSPITAL`; `ProblemError`, `InvalidTransition` (a `ProblemError`), `install_handlers` and the error codes `forbidden_role`,
  `not_found`, `request_in_progress`, `query_closed`, `stale_etag`; signing helpers (`utc_ts`, `build_headers`, secret lists for rotation,
  `skew_seconds`); `CONTRACT_VERSION` / `SUPPORTED_VERSIONS`.
- Models: `QueryCallback`, `DecisionCallback`, `SettlementCallback`, `HealthResponse`, `ContractInfo`; `SettlementNotice.status` (paid | reversed)
  and `utr` up to 64 characters; `Acknowledgement.contract_version`; `Decision.reviewer_ids` up to 3; `BillLine.line_id`, `DocumentRef.mime_type`
  and `url_expires_at` optional (the receiver assigns `L001..`). Cross-field validation stays in `ClaimSubmission` (stricter than the insurer's own copy).
- Insurer state machine: withdraw edges to `closed`, `verifying -> escalated`, `escalated -> ready_for_decision | needs_info`,
  `ready_for_decision -> verifying | rejected`, `awaiting_approval -> ready_for_decision`, `approved | partially_approved -> closed` (zero payable).
  `ready_for_decision | awaiting_approval -> needs_info`: a reviewer who is about to decide can still ask the hospital a question.
- `claim_contract.insurer_side`: the insurer's keyed idempotency stores, ContractAuthMiddleware, OutboxSender, audit chain, validation and sample builders.

## 1.0.0 (baseline, tag `contract-v1.0` pending Dev B review)
- Models, enums, state machines, signing (vectors V1/V2), idempotency, inbox sequencing, outbox sender, audit hash chain.
