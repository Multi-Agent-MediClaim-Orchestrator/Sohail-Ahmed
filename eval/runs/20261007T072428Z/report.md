# Evaluation 20261007T072428Z

cases=300 seed=42 llm_mode=offline-stub

| suite | metric | value | n | 95% CI | target | pass |
|---|---|---|---|---|---|---|
| calc | calc_agreement_vs_reference | 1.0 | 296 | (0.9872, 1.0) | 0.995 | True |
| gate | decision_route_correctness | 1.0 | 202 | (0.9813, 1.0) | 1.0 | True |
| gate | wrongful_auto_path | 0.0 | 202 | (-0.0, 0.0187) | 0.0 | True |
| mapper | mapper_rule_accuracy | 1.0 | 210 | (0.982, 1.0) | 0.97 | True |
| identity | identity_match_recall | 1.0 | 200 | (0.9812, 1.0) | 0.98 | True |
| identity | identity_mismatch_recall | 1.0 | 200 | (0.9812, 1.0) | 0.95 | True |
| rag | rag_recall@5 | 0.93 | 120 | None | 0.85 | True |
| rag | rag_mrr | 0.8002 | 120 | None | 0.6 | True |
| rag | rag_ndcg@5 | 0.8329 | 120 | None | 0.7 | True |
| rag | rag_citation_precision | 0.6104 | 120 | None | 0.9 | False |
| rag | rag_insufficient_evidence_accuracy | 1.0 | 120 | None | 0.9 | True |
| rag | rag_temporal_trap_accuracy | 1.0 | 120 | None | 0.95 | True |
| rag | rag_numeric_faithfulness | 1.0 | 120 | None | 1.0 | True |

## Not run (and why)

- **doc_parse_accuracy**: needs doc-pipeline (Dev A) and rendered images
- **vision_stamp_signature**: needs vision-service
- **pii_masking_recall**: needs Presidio in doc-pipeline
- **verification_pipeline_e2e**: run `pytest insurer/api/tests/test_golden_e2e.py` (needs Docker/Postgres)
- **llm_agent_quality**: needs the live gateway (Gemini/Ollama); offline stubs only here
- **calibration**: needs parser/vision confidences from the live pipeline
