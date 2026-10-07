# Trace conventions (04-04 §7.6)

| Langfuse field | Value |
|---|---|
| `session_id` | `claim_ref` (hospital id); `kb-ingest` for non-claim calls |
| trace name | `<system>.<stage>` e.g. `insurer.verification` |
| trace `user_id` | pseudonymous role (`system:insurer-crew`), never a person's name |
| generation name | agent name |
| tags | `system:<x>`, `agent:<x>`, `stage:<x>`, `cache:hit\|miss`, `fallback:<a>-><b>` |
| metadata | `prompt_version` (`<agent>@<n>`), `config_versions`, `llm_mode` |

Crews create the parent trace and pass `trace_id` in `metadata`; the gateway attaches its generation as a child.
Without a `trace_id` the gateway creates a root trace per call.

Required request metadata: `system` (`hospital|insurer|shared`), `agent`, `prompt_version`
(the last two are not required for the `eval-harness` and n8n keys).

## Quotas (QUOTAS.md in the doc layout)

Free-tier limits change; re-check before demos. Assumed: reason-cloud ~10 rpm, cleanup-cloud ~15 rpm, vision-cloud ~5 rpm
(set in `config.yaml`). When a quota is hit the router falls back to local Ollama. `LLM_MODE=local` stays fully offline.
Last checked: not independently verified from this repository — confirm in Google AI Studio.
