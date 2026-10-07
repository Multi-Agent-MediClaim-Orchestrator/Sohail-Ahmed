# tpa-sim

Scripted stand-in for the insurer/TPA and bank, so the hospital side (Dev A) can be built against it. It validates nothing about the claim's
merits: it follows a scenario script. Contract endpoints are `/v1/hospital-api/*` (HMAC + idempotency via `claim_contract`), callbacks go to
`/v1/insurer-callbacks/*` on the hospital, control plane is `/sim/*`, bank is `/bank/*` and `/preauth/*`.

```bash
uvicorn tpa_sim.main:app --port 8500
curl -X POST localhost:8500/sim/chaos -d '{"preset":"flaky"}'          # off | flaky | slow | hostile
curl localhost:8500/sim/scenarios                                       # 14 scenarios; add yours with POST /sim/scenarios (YAML)
```

Select a scenario per claim with the `X-Sim-Scenario` header, or let `match:` rules in a scenario route automatically
(`claim_type`, `claimed_amount_gt/lt`, `patient_name` glob). Steps: `status`, `query`, `decision`, `settlement`, `wait_response`;
step options: `after`, `manual` (fires only through `POST /sim/claims/{ref}/fire-next`), `signing: bad_secret|skew_plus_10m` (probe steps the
hospital must reject), `duplicate`, `on_timeout`.

Determinism: callbacks get strictly increasing sequence numbers assigned when the body is built; retries re-send identical bytes with the same
idempotency key (backoff 1/4/16/60/300/900/1800/3600 s, scaled by `TPA_SIM_RETRY_SCALE`). Tests run entire scenarios under `FakeClock`; the goldens
in `tests/golden/` pin the ordered `(kind, sequence, status)` trace of each scenario.

Settings (`TPA_SIM_*`): `HOSP_KEYS` (key id → secret), `INS_TO_HOSP_SECRET`, `HOSP_BASE_URL`, `TIME_SCALE`, `RETRY_SCALE`, `CHAOS_SEED`, `DB_URL`
(SQLite; claims, events and logs are persisted, restart recovery is untested). Defaults are development secrets only.
