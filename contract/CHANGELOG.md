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

## 1.0.0 (baseline, tag `contract-v1.0` pending Dev B review)
- Models, enums, state machines, signing (vectors V1/V2), idempotency, inbox sequencing, outbox sender, audit hash chain.
