# 02 — Requirements

Each requirement has an ID, an acceptance criterion, and where it is verified. **T** = `tests/test_core.py`; **UCnn** = the scenario in `harness/scenarios/`; **L** = load test (Stage 3); **M** = manual check on Google Cloud.

## 1. Functional requirements

### 1.1 Data contracts and quality

| ID | Requirement | Acceptance criterion | Verified by |
|---|---|---|---|
| FR-01 | Each data product has a YAML contract defining its columns, criticality (1–3), patterns, ranges, allowed values, freshness SLA, upstream lineage, zero-tolerance rules, tools, limits, temporal rules, taint and escrow settings | Contracts load and validate; weights must sum to 1 | `ContractRegistry.from_dir` · all UCs |
| FR-02 | Freshness is computed from the last committed load, not from when the table was queried | Stale load → `fresh` < 0.9 even though rows are present | UC01, UC04 |
| FR-03 | Lineage is the product of upstream health scores; a missing upstream counts as 0 | Unapproved pipeline → `lin` = 0 → Tier 3 | UC05, UC08 |
| FR-04 | Completeness is criticality-weighted, and the minimum completeness of critical columns is gated separately | 30% nulls in one critical column → below Tier 1 while linear CQI stays > 0.85 | UC03 |
| FR-05 | Drift compares current data with the active reference profile using MMD², per-column Wasserstein distance and flat-line detection | Regime shift and frozen feeds → `drift` < 0.5 | UC04, UC07, UC09 |
| FR-06 | Integrity counts contract-rule violations, multivariate outliers and duplicate clusters; flagged rows are excluded from token bindings | Cloned or rule-breaking rows cannot be acted on | UC02, UC05, UC12 |
| FR-07 | Zero-tolerance rules (not_null, equals, in, min, max, not_in_source) force Tier 3 when broken | Unit change, missing severity, stale bureau data, sensor 0.0, averaged income → Tier 3 | UC03, UC07, UC08, UC11, UC12 |
| FR-08 | Score = Γ × Φ × d-CQI; tiers by contract thresholds (default 0.85 / 0.70) | Reported with all vectors and reasons | All UCs |
| FR-09 | The linear CQI is reported alongside for comparison | Shown in every evaluation | All UCs |
| FR-10 | A product may declare alternative reference profiles ("modes"); switching requires a statement signed by the oracle key, for the declared mode only, until it expires | Wrong key or undeclared mode rejected; expiry returns to baseline | UC02 |

### 1.2 Tokens

| ID | Requirement | Acceptance criterion | Verified by |
|---|---|---|---|
| FR-11 | Tokens are ES256 JWS signed by Cloud KMS (local key in development) and carry the claims listed in LLD §3 | Verify with the published public key | T |
| FR-12 | Tier 1 → write tools direct; Tier 2 → write tools escrow; Tier 3 → no tools; worst tier across a task's products wins | Modes in token match tier | UC01, UC05 |
| FR-13 | Tier-1 token lifetime ≤ time until the stalest product falls below its freshness gate | 2-minute SLA → TTL < 140 s; 5-second SLA → TTL < 6 s | UC04, UC09 |
| FR-14 | Bindings list the exact allowed argument combinations from current, unflagged rows, with per-row maxima | Redirect, account swap, over-amount, missing depot scan → BINDING_VIOLATION | UC01, UC05, UC06 |
| FR-15 | Delegation can only remove tools, lower limits and keep a subset of bindings; depth ≤ 2; child expiry ≤ parent | Escalation and depth 3 refused | UC10 |
| FR-16 | A task may request only the tools it needs (least privilege) | `tools` parameter narrows the token | API |

### 1.3 Enforcement gateway

| ID | Requirement | Acceptance criterion | Verified by |
|---|---|---|---|
| FR-17 | Every tool call is checked in the order in LLD §6; the first failure denies the call | Correct reason code for each case | All UCs, T |
| FR-18 | Missing, malformed, forged, edited, expired or wrong-audience tokens are refused | TOKEN_* codes | T, UC11, UC10 |
| FR-19 | Request ids are single-use per token | Second use → REPLAY | UC01 |
| FR-20 | Arguments must match exactly: none missing, none extra, each within pattern / enum / numeric bounds | ARG_* codes | UC03, UC05, UC09, T |
| FR-21 | Temporal rules hold across tokens and sessions (state keyed by business keys, not by token) | Duplicate payment, hourly ceiling, per-contractor ceiling, per-address count, per-district ceiling | UC01, UC02, UC04, UC06, UC12 |
| FR-22 | After an untrusted tool result, later write calls in that session go to escrow (or are denied, per contract) | Tainted session → escrow | UC03 |
| FR-23 | Any state-store error denies the call (fail closed) | STATE_UNAVAILABLE | T (manual: stop Redis) |
| FR-24 | Denials close the connection | `Connection: close` on 403 | M |

### 1.4 Escrow, tools, audit

| ID | Requirement | Acceptance criterion | Verified by |
|---|---|---|---|
| FR-25 | Escrow executes only after N distinct approvers (default 2); the requesting agent can never approve; one rejection closes the item | SELF_APPROVAL, DUPLICATE_APPROVER, ESCROW_CLOSED | UC01, UC05, UC12, T |
| FR-26 | Approver identity comes from the authenticated caller, not the request body | Unauthenticated approval → 401 | M |
| FR-27 | Temporal rules are re-checked when an escrowed action finally runs | Denied if limits were reached meanwhile | Code (`execute_approved`) |
| FR-28 | Selected tools always need escrow regardless of tier (e.g. debt notices) | `always_escrow` | UC12 |
| FR-29 | Every decision is appended to a hash chain; checkpoints are signed by the audit key | Verify passes; any edit fails | UC01, UC08, T |
| FR-30 | Audit records are written to a Cloud Storage bucket with a retention policy (optionally locked) | Objects present; deletion blocked while retained | M |
| FR-31 | Results of read tools can be screened by a content screener (Google Cloud Model Armor); a positive finding taints the session, so later write actions go to escrow. Screening failures count as flagged (fail closed) | Flagged invoice note → next payment held; with screening off the same payment executes | UC01, T |

## 2. Non-functional requirements

| ID | Area | Requirement | Target | Verified by |
|---|---|---|---|---|
| NFR-01 | Latency | Gateway overhead per tool call (user space, excluding the tool) | p50 ≤ 5 ms, p99 ≤ 20 ms on Cloud Run; Stage 2 kernel target < 1 ms | L |
| NFR-02 | Latency | Token issue including quality evaluation (5,000-row sample) | p95 ≤ 2 s; cache reports per product for N seconds if needed | L |
| NFR-03 | Accuracy | False-alarm rate on healthy data | ≤ 5% (current: 0–5% on all 12 products) | `harness.calibrate` |
| NFR-04 | Availability | Fail closed when KMS, BigQuery or Redis is unavailable | No action executes without a verified token and state | M |
| NFR-05 | Security | No private key leaves KMS/HSM; services hold least-privilege IAM roles | IAM matrix in LLD §10 | M |
| NFR-06 | Security | No unauthenticated access to any service | No `allUsers` invoker | M |
| NFR-07 | Integrity | Audit log tamper-evident; optional write-once retention | Verify tool; bucket lock | T, M |
| NFR-08 | Operability | Every denial logs a reason code, token id, agent and argument hash | Audit records | All UCs |
| NFR-09 | Portability | Same code runs locally (no cloud dependencies) and on Google Cloud | `CENE_MODE=local/gcp` | This repo |
| NFR-10 | Cost | Stage 1 idle cost under A$60/month; GPU only while testing | Budget alerts | M |
| NFR-11 | Privacy | Synthetic or public data only; the audit log stores argument hashes, not values | Code | Review |
| NFR-12 | Reproducibility | Fixed seeds; scenario report regenerated by one command | `python -m harness.runner --report` | This repo |

## 3. Security requirements (threats the design must handle)

| Threat | Control | Requirement |
|---|---|---|
| Agent persuaded to pay a different account | Joint row bindings | FR-14 |
| Agent repeats or splits actions to get around per-call limits | Temporal rules shared across sessions | FR-21 |
| Agent acts on stale, frozen, drifted or corrupted data | Quality tiers, zero-tolerance rules, token lifetime | FR-02–FR-13 |
| Agent acts after reading untrusted content | Session taint → escrow; content screening (Model Armor) as an extra trigger | FR-22, FR-31 |
| Sub-agent given more authority than intended | Attenuated delegation | FR-15 |
| Token forged, edited, replayed or used late | Signature, audience, expiry, single-use request ids | FR-18, FR-19 |
| Someone declares a fake emergency to relax checks | Oracle-signed, declared, expiring modes | FR-10 |
| One insider approves a risky action alone | Two distinct approvers, identity from authentication | FR-25, FR-26 |
| Records altered after the fact | Hash chain + signed checkpoints + retention lock | FR-29, FR-30 |
| Agent bypasses the gateway by connecting to tools directly | Stage 1: tools callable only by the gateway's service account (Cloud Run IAM). Stage 2: kernel enforcement | NFR-06, Stage 2 |

## 4. Traceability: use cases → controls

| Use case | Main controls exercised |
|---|---|
| UC01 Invoice settlement | Bindings, once-per-invoice, freshness → escrow, two approvers, expiry, replay, content screening → taint, audit |
| UC02 Disaster payments | Signed stress mode, per-contractor ceiling, duplicate clusters |
| UC03 Clinical support | Unit zero-tolerance, critical-column gate, taint → escrow, argument bounds |
| UC04 Treasury FX | Hourly sum ceiling, short token lifetime, flat-line detection |
| UC05 Procurement | Enums, bindings, rule-violating rows excluded, lineage → escrow, rejection |
| UC06 Retail refunds | Depot-scan binding, per-address count across sessions |
| UC07 SME credit | Drift, zero-tolerance on bureau age |
| UC08 Clinical trial | Not-null zero-tolerance, lineage, audit proof |
| UC09 Grid dispatch | 5-second SLA token expiry, flat-line, setpoint bounds |
| UC10 Telecom retention | Attenuated delegation, depth limit, tamper detection |
| UC11 Aviation | Physical-floor zero-tolerance, missing/forged/edited tokens |
| UC12 Public benefits | Duplicate clusters, district ceiling, not-in-source rule, always-escrow |
