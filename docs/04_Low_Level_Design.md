# 04 — Low-Level Design

## 1. Repository layout

```
cene_e2e/
├── cene/                      core library (no web framework, no cloud dependency in local mode)
│   ├── util.py                clock (adjustable), canonical JSON, base64url, SHA-256
│   ├── signer.py              LocalSigner, KmsSigner (ES256), verify_es256
│   ├── tokens.py              issue / verify compact JWS (Context Tokens, mode statements)
│   ├── merkle.py              Merkle commitments for large binding sets
│   ├── contracts.py           data-contract schema, defaults, registry
│   ├── datastore.py           LocalDataStore (pandas), BigQueryDataStore
│   ├── stores.py              MemoryStore, RedisStore (replay ids, taint, temporal windows)
│   ├── quality/metrics.py     five vectors + zero-tolerance gatekeeper
│   ├── quality/engine.py      score, gate, tier, time-until-stale
│   ├── modes.py               oracle-signed mode registry
│   ├── control_plane.py       issue(), delegate()
│   ├── gateway.py             handle() - the enforcement decision
│   ├── escrow.py              two-person approval
│   ├── tools.py               mock tool servers
│   ├── audit.py               hash chain, checkpoints, sinks, verify_records()
│   ├── screening.py           content screeners: none, marker (tests), Model Armor
│   └── platform.py            wiring for local and gcp modes
├── services/app.py            Starlette HTTP API (roles: control, gateway, tools, all)
├── agent/runner.py            multi-turn LLM agent via the gateway; task-file evaluation
├── harness/                   datagen, faults, runner (12 scenarios), calibrate, bq_load, sign_mode, export_contracts
├── harness/scenarios/*.yaml   one file per use case: contracts + data + steps
├── harness/contracts/*.yaml   contracts exported for the services
├── tests/                     unit tests + all scenarios
├── deploy/terraform/          Google Cloud infrastructure
├── Dockerfile, docker-compose.yml, requirements.txt
└── docs/                      this specification
```

## 2. Configuration (environment variables)

| Variable | Used by | Meaning | Default |
|---|---|---|---|
| `CENE_SERVICE` | app | `control`, `gateway`, `tools` or `all` | `all` |
| `CENE_MODE` | all | `local` or `gcp` | `local` |
| `CENE_CONTRACTS_DIR` | all | Folder of contract YAML files | `harness/contracts` |
| `CENE_SEED_SCENARIO` | app (local) | Seed in-memory data from a scenario (e.g. `uc01`) | — |
| `GOOGLE_CLOUD_PROJECT` | gcp | Project id | — |
| `CENE_DATASET` | gcp | BigQuery dataset | `cene` |
| `CENE_TOKEN_KEY` | control, gateway | KMS key version for tokens (control signs; gateway reads public key) | — |
| `CENE_ORACLE_KEY` | control | KMS key version for mode statements (public key only) | — |
| `CENE_AUDIT_KEY` | control, gateway | KMS key version for audit checkpoints | token key |
| `CENE_AUDIT_BUCKET` | gcp | Audit bucket | — (memory) |
| `CENE_AUDIT_FILE` | local | JSON-lines audit file | — (memory) |
| `CENE_REDIS_URL` | gateway | Shared state; empty = in-memory, single instance | — |
| `CENE_TOOLS_URL` | gateway | Tools service URL (otherwise in-process mock) | — |
| `CENE_REDIS_TIMEOUT_S` | gateway, control | Connect and reply timeout for Redis, in seconds; no retries, so an unavailable store is refused quickly with `STATE_UNAVAILABLE` | `1.0` |
| `CENE_TOOLS_TIMEOUT_S` | gateway | Read timeout for calls to the tools service, sized to cover a cold start; no retry, since write tools are not idempotent | `30` |
| `CENE_TOKEN_TTL` | control | Default token lifetime in seconds | 900 |
| `CENE_SCREENER` | gateway | `none`, `marker` (tests) or `model_armor` | `none` |
| `CENE_MODEL_ARMOR_TEMPLATE` | gateway | `projects/P/locations/L/templates/T` | — |

## 3. Context Token

### 3.1 Format

Compact JWS: `base64url(header) . base64url(payload) . base64url(signature)`. The header is `{"alg":"ES256","kid":"<key id>","typ":"CENE-CT"}`. The signature is raw r‖s (64 bytes) over `header.payload`. Cloud KMS returns DER, which `signer.der_to_raw` converts. JSON is canonical (sorted keys, no spaces).

### 3.2 Claims

| Claim | Type | Meaning |
|---|---|---|
| `iss` / `aud` | string | `cene-control-plane` / `cene-gateway` |
| `sub` | string | Agent id |
| `jti` | hex | Token id (replay ids and taint are keyed by it) |
| `iat`, `nbf`, `exp` | epoch s | Issue, not-before, expiry. Tier-1 expiry = min(TTL, time until the stalest product's freshness drops below its gate) |
| `task` | string | Free text, for audit |
| `tier` | 1–3 | Worst tier across products |
| `products` | map | `{product: {score, tier, mode}}` |
| `tools` | map | `{tool: {kind: read/write, mode: direct/escrow, product, args, bindings? / bindings_root?, bindings_count?}}` |
| `tools.*.args` | map | `{arg: {pattern?, enum?, type?, min?, max?}}`. Tool limits such as `amount_max` are folded into `max` |
| `tools.*.bindings` | list | ≤ 20 entries inline: `{k: {arg: value,...}, max: {arg: number}}` |
| `tools.*.bindings_root` | hex | Merkle root when there are more than 20 entries (§3.3) |
| `temporal` | list | Rules for tools in this token (§6.2) |
| `taint` | map | `{untrusted_tools: [...], on_taint: escrow/deny}` |
| `escrow` | map | `{approvers_required: n}` |
| `depth`, `parent` | int, hex | Delegation depth; SHA-256 of the parent token |

Example (UC01, bindings trimmed): see `docs/scenario_report.md`. Typical size is 1.6 KB with a Merkle root, against 32 KB with 278 inline bindings.

### 3.3 Merkle-committed bindings

- `leaf = SHA256(0x00 ‖ canonical(binding))`;
- `node = SHA256(0x01 ‖ left ‖ right)`;
- an odd node at any level is promoted unchanged.

`POST /v1/tokens` returns `bindings: {tool: [{binding, proof}]}`. When calling a tool, the agent puts the entry for its chosen row in `params._meta.binding_proof`. The gateway:
1. recomputes the root from the binding and proof, and compares it with `bindings_root`;
2. checks the arguments against that binding, i.e. the keys are equal and each value is ≤ its `max`.

A row the control plane never issued, or an edited maximum, has no valid proof.

### 3.4 Delegation

`POST /v1/tokens/delegate {parent_token, agent_id, tools: {tool: {limits?: {arg_max: n}, bindings_subset?: [...]}}}`. The child:
- has only the listed tools;
- may set limits only to values ≤ the parent's;
- may keep only a subset of bindings, as `{binding, proof}` entries when the parent used a root;
- keeps the same argument patterns;
- has `exp ≤ parent.exp` and `depth = parent.depth + 1` (maximum 2);
- has `parent = SHA256(parent token)`.

Tools whose bindings aren't narrowed keep the parent's root, so the parent's proofs remain valid.

## 4. Data contract schema

| Key | Required | Meaning |
|---|---|---|
| `product` | yes | Table name |
| `owner` | no | Accountable team |
| `key_column` | no | Natural key (documentation) |
| `freshness.sla_seconds` | yes | Allowed age of the last committed load |
| `lineage.upstreams` | no | Nodes whose health multiplies into `lin` |
| `columns.<name>` | yes | `type` (string, number or timestamp); `criticality` (1–3; 3 = critical); `pattern`; `allowed`; `min`; `max` |
| `zero_tolerance` | no | `[{column, rule, value/values}]`; rule ∈ not_null, equals, in, min, max, not_in_source |
| `duplicate_keys` | no | Columns whose identical combinations (groups of 3 or more) count as a suspicious cluster |
| `quality.weights` | no | fresh, lin, comp, drift, poison (sum 1; default 0.2 each) |
| `quality.gate` | no | Thresholds per vector (default lin 0.6, drift 0.5, poison 0.6, comp_crit 0.9, fresh 0.9) |
| `quality.tiers` | no | tier1 0.85, tier2 0.70 |
| `quality.poison_tolerance` | no | Fraction of bad rows that drives `poison` to 0 (default 0.05) |
| `quality.drift` | no | `w1_iqr_threshold` 0.35, `flatline_ratio` 0.05, `mmd_scale` 2.0 |
| `modes.<name>` | no | Declared alternative reference profiles (e.g. stress) |
| `tools.read.<tool>` | no | `{handler: lookup/external, key, args}` |
| `tools.write.<tool>` | no | `{args, limits: {<arg>_max}, bind: {keys, max: {arg: column}, where, require_not_null}, always_escrow, handler}` |
| `temporal` | no | `[{name, tool, type: once_per/count_within/sum_within, by: [args or "agent"], window_seconds, max?, field?}]` |
| `taint` | no | `{untrusted_tools, on_taint}` |
| `escrow.approvers_required` | no | Default 2 |

## 5. Quality computations

Let `age` = now − last committed load and `SLA` = `freshness.sla_seconds`.

| Vector | Definition |
|---|---|
| `fresh` | 1 if age ≤ SLA, else exp(−ln2 · (age − SLA)/SLA). Unknown load time gives 0 |
| `lin` | ∏ health(upstream); a missing node gives 0 |
| `comp` | Σ βc (1 − nullc) / Σ βc, with β = criticality. Blank strings count as null; a missing column has null rate 1 |
| `comp_crit` | min over critical columns of (1 − nullc) |
| `drift` | min(S_mmd, S_w1, S_flat), detailed below |
| `poison` | clip(1 − (rule-violation rate + max(0, outlier rate − 0.01) + duplicate-cluster rate) / tolerance, 0, 1) |
| Γ | 0 if any zero-tolerance rule fails, else 1 |

The three drift components:
- **S_mmd**: standardise numeric columns by the reference. Compute the unbiased RBF MMD² (median-heuristic bandwidth) between up to 300 current rows and the reference. Compare it with the 95th percentile of MMD² between 20 random half-splits of the reference (q95). S_mmd = exp(−max(0, MMD²/q95 − 1)/mmd_scale).
- **S_w1**: for each numeric column, the 1-Wasserstein distance divided by the reference IQR; take the worst column. S_w1 = exp(−max(0, w − θw)/θw).
- **S_flat**: 0.1 if any critical numeric column's standard deviation falls below `flatline_ratio` × its reference standard deviation, else 1.

The outlier rate is the fraction of rows whose Ledoit–Wolf Mahalanobis distance against the reference exceeds the χ² 99.9% quantile.

Combining them:
- **d-CQI** = Σ wᵢ Sᵢ over fresh, lin, comp, drift and poison;
- **Φ** = ∏ₖ min(1, Sₖ/θₖ) over the gate keys;
- **score** = Γ · Φ · d-CQI;
- **tier** = 1 if score ≥ tier1, 2 if score ≥ tier2, else 3.

Rows that break rules, are outliers or belong to duplicate clusters are listed in `flagged_rows` and are never bound into tokens.

**Time until stale** = SLA · (1 + log₂(1/θ_fresh)) − age. Tier-1 tokens expire no later than this.

**Linear CQI**, reported for comparison only, = (w_f·fresh + w_l·lin + w_c·(1 − mean null rate)) / (w_f + w_l + w_c).

## 6. Gateway

### 6.1 Decision order (`Gateway.handle`)

| # | Check | Code on failure |
|---|---|---|
| 0 | `method == tools/call` | METHOD_NOT_ALLOWED |
| 1 | Token present, well-formed, `typ`/`alg`, known `kid`, valid signature, `aud`, `nbf`/`exp` (±2 s leeway) | TOKEN_MISSING / TOKEN_MALFORMED / TOKEN_UNKNOWN_KEY / TOKEN_BAD_SIGNATURE / TOKEN_WRONG_AUDIENCE / TOKEN_NOT_YET_VALID / TOKEN_EXPIRED |
| 2 | Request id present and unused for this `jti` (SET NX) | REQUEST_ID_MISSING / REPLAY |
| 3 | Tool in token | TOOL_NOT_ALLOWED |
| 4 | Arguments exact; each passes pattern (full match), enum, numeric type and min/max | ARG_MISSING / ARG_UNEXPECTED / ARG_OUT_OF_BOUNDS |
| 5 | Inline binding matches, or Merkle proof valid and matching | BINDING_VIOLATION |
| 6 | Temporal rules | TEMPORAL_VIOLATION (detail = rule name) |
| 7 | Tainted session + write tool: escrow, or deny if `on_taint: deny` | TAINTED_SESSION |
| 8 | Mode escrow → submit and return `pending_approval` | — |
| 9 | Call tool; record temporal events; set taint if the tool is untrusted, or if the tool is a read tool and the screener flags its result (audit event `screening_flagged`) | — |
| * | Any state-store exception in steps 2, 6 or 7 | STATE_UNAVAILABLE |

Every outcome is audited with the token id, agent, tool, and SHA-256 of the canonical arguments. Denials return HTTP 403 with `Connection: close`.

### 6.2 Temporal rules and state keys

| Key | Value | Expiry |
|---|---|---|
| `rid:{jti}:{request id}` | "1" | token expiry + 60 s |
| `taint:{jti}` | "1" | token expiry |
| `tmp:{rule}:{sha256(by-values)[:24]}` | sorted set or list of (timestamp, value) | window length |

- `once_per`: deny if any event exists in the window.
- `count_within`: deny if count + 1 > max.
- `sum_within`: deny if the sum of values + this call's `field` > max.

`by` may name arguments, or `agent` for the token subject. Because keys use business values and not the token, limits survive new sessions.

**Known race:** the check and the record are separate operations. Under parallel calls with Redis, make them atomic with a Lua script per rule, as a Stage-1b hardening item.

## 7. HTTP API

| Role | Method & path | Body → response |
|---|---|---|
| any | `GET /health` | `{ok, role}` (`/healthz` works locally only; Cloud Run reserves paths ending in "z") |
| control | `GET /v1/quality/{product}` | Full quality report (vectors, Γ, Φ, score, tier, reasons, detail) |
| control | `POST /v1/tokens` | `{agent_id, task, products[], tools?[]}` → `{token, tier, expires, tools{name: mode}, bindings{tool: [{binding, proof}]}, reports}` |
| control | `POST /v1/tokens/delegate` | `{parent_token, agent_id, tools}` → `{token, expires, bindings}` or 403 `{error}` |
| control | `POST /v1/modes` | `{statement}` → `{product, mode, expires}` or 403 `{error}` |
| control | `GET /v1/keys` | Public keys `[{kid, alg, pem}]` |
| gateway | `POST /mcp` | JSON-RPC `tools/call`; header `X-Context-Token`; optional `params._meta.binding_proof` → 200 result / `pending_approval`, or 403 error `{code: -32001, message: <CODE>}`; response header `X-CENE-Decision` |
| gateway | `GET /v1/escrow` | Pending items (without token claims) |
| gateway | `POST /v1/escrow/{id}/approve` · `/reject` | Approver = email in the caller's verified Cloud Run identity token (local: `X-Approver`) → status or 409 `{error}` |
| tools | `POST /v1/tools/{tool}` | `{arguments, agent}` → tool result (callable only by the gateway's service account) |
| tools | `GET /v1/executed` | Actions executed so far (mock) |

On Cloud Run, the `Authorization` header always carries the caller's Google identity token, and the Context Token goes in `X-Context-Token`.

## 8. Audit record format

- **Event:** `{"kind":"event","seq":n,"event":{ts, type, ...},"hash":h_n}` with h₀ = SHA256("CENE-AUDIT-GENESIS") and hₙ = SHA256(hₙ₋₁ ‖ canonical(event)).
- **Checkpoint:** every 100 events and on demand, `{"kind":"checkpoint","seq":n,"body":{seq, head, ts, kid},"sig":b64url(ES256)}`.

Event types:
- `token_issued`, `token_delegated`;
- `decision` (allowed, escrow or denied);
- `escrow_submitted`, `escrow_approved`, `escrow_rejected`, `escrow_executed`, `escrow_execution_denied`;
- `mode_switched`, `mode_rejected`.

**Storage.** Local: JSON lines. Cloud: one object per record in `gs://<project>-cene-audit/audit/<service>/<chain id>/<seq>-<kind>.json`, with a bucket retention policy. Each service instance keeps its own chain, and the chain id (instance start time plus a random suffix) keeps chains apart across services and restarts. Objects are created with `if_generation_match=0`, so a record can never be overwritten, and writers need only `roles/storage.objectCreator`. Verify each chain folder separately.

**Verification:** `cene.audit.verify_records(records, audit_public_pem)`.

## 9. State, scaling and persistence

| State | Stage 1 (no Redis) | With Redis / Firestore |
|---|---|---|
| Replay ids, taint, temporal windows | Gateway memory → single instance | Memorystore Redis → gateway can scale out |
| Escrow items | Gateway memory | Move to Firestore (collection `escrow`, document per item, transactions for approvals) |
| Active modes | Control memory → single instance | Store signed statements in Firestore; any instance re-verifies them |
| Quality reports | Computed per token request | Cache per product for N seconds (e.g. 30 s) to bound BigQuery cost |

## 10. Google Cloud resources and IAM

| Resource | Name | Notes |
|---|---|---|
| KMS key ring | `cene` | Region = Cloud Run region |
| KMS keys | `token`, `oracle`, `audit` | ASYMMETRIC_SIGN, EC_SIGN_P256_SHA256, protection SOFTWARE or HSM |
| BigQuery | dataset `cene`; tables per product, `<product>__ref_<mode>`, `_cene_load_log`, `_cene_lineage_health` | Loaded by `harness/bq_load.py` |
| Cloud Storage | `<project>-cene-audit` | Retention policy; lock with `lock_audit_retention=true` (irreversible) |
| Cloud Run | `cene-control`, `cene-gateway`, `cene-tools` | One image, role by env var; no public invoker |
| Memorystore | `cene-state` (optional) | Basic 1 GB + VPC connector |

| Principal | Token key | Oracle key | Audit key | BigQuery | Audit bucket | Invoke |
|---|---|---|---|---|---|---|
| `cene-control` | signerVerifier | publicKeyViewer | signerVerifier | dataViewer + jobUser | objectCreator | — |
| `cene-gateway` | publicKeyViewer | publicKeyViewer | signerVerifier | dataViewer + jobUser | objectCreator | tools (+ `roles/modelarmor.user` when screening is on) |
| `cene-tools` | — | — | — | dataViewer + jobUser | — | — |
| `cene-agent` | — | — | — | — | — | control, gateway |
| Named officers (`oracle_signers`) | — | signerVerifier | — | — | — | control |
| Operators/approvers (`operators`) | — | — | — | — | — | control, gateway |

## 11. Extending

- **New data product:** add a contract YAML, load the table and its reference into BigQuery, and add a scenario to `harness/scenarios/`.
- **Real tool:** replace the handler in `cene/tools.py` with an API client. Keep the tool server callable only by the gateway's service account.
- **Dataplex:** populate `_cene_load_log` from Dataplex or BigQuery job metadata, and `_cene_lineage_health` from Dataplex lineage plus pipeline status. Data contracts can be stored as Dataplex aspects.

## 12. Stage 2: in-kernel enforcer (design; not yet built)

**Target:** Ubuntu 24.04 VM (kernel 6.8), then GKE Standard nodes with kernel ≥ 6.5, running as a privileged DaemonSet. The agent's container must run without `CAP_SYS_ADMIN` or `CAP_BPF` and not on the host network.

| Hook | Purpose |
|---|---|
| BPF LSM `socket_connect` on the agent's cgroup | Allow only destinations listed in the token (gateway or tool hosts); check expiry with `bpf_ktime_get_ns()`; return `-EPERM` otherwise |
| uprobes on `SSL_write` (the agent's TLS library) | Read each request before encryption; run the argument DFA from a `BPF_MAP_TYPE_ARRAY`; keep the DFA state across chunks in socket-local storage |
| cgroup_skb egress / TC egress | Drop packets on sockets marked non-compliant |
| BPF socket iterator + `bpf_sock_destroy()` kfunc | Abort the non-compliant connection (kernel sends RST) |
| Ring buffer | Stream decisions to the node agent → audit log |

A node agent (user space) does three jobs:
- verifies Context Tokens;
- writes the allowed destinations, expiry, DFA tables and HMAC epoch keys into pinned maps under `/sys/fs/bpf/cene`;
- forwards ring-buffer events to the audit log.

**Measures to report:**
- overhead per call, compared with the Stage-1 gateway;
- time from violation to reset;
- the bypass test: a direct connection from the agent container to a tool host must be refused.
