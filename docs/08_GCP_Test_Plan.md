# 08 — Google Cloud Test Plan (brief for Claude Code)

This is the work order for the Claude Code session running in Cloud Shell. It says what to deploy, what to test, what result each test should give, and how to report. Read `docs/04_Low_Level_Design.md`, `docs/05_Test_Scenarios.md` and `docs/06_Runbook.md` first.

## 0. Environment and rules

| Item | Value |
|---|---|
| Project | `<gcp-project-id>` |
| Region | `australia-southeast2` (BigQuery location too) |
| Image | `australia-southeast2-docker.pkg.dev/<gcp-project-id>/cene/cene:<tag>`. v3 is the latest. Use v4, v5 and so on for each rebuild |
| Services | `cene-control`, `cene-gateway`, `cene-tools` (Cloud Run, authenticated) |
| Audit bucket | `gs://<gcp-project-id>-cene-audit` |
| Dataset | `cene` |

**Rules:**
1. **Never** run anything irreversible without asking the user first. That includes:
   - `terraform destroy`
   - `gcloud projects delete`
   - `lock_audit_retention = true`
   - deleting the dataset or the bucket
   - KMS key destruction
2. Don't grant broad roles (Owner, Editor, `*Admin`) to service accounts. If a permission is missing, add the narrowest role in `deploy/terraform/main.tf` and say why in the report.
3. Don't create GPU VMs or other paid resources beyond what Terraform already defines. The LLM agent stage (runbook §3) is **out of scope** for this run.
4. Don't write new prompt-injection or bypass payloads. The only screener input allowed is the harness test marker `[[CENE-TEST-FLAG]]`, or cases taken verbatim from a published benchmark (AgentDojo, InjecAgent).
5. **Git:**
   - work on a branch `gcp-test-<yyyymmdd>`;
   - never force-push and never push to `main`;
   - give each fix its own commit, with a test that fails before the fix and passes after;
   - run `python -m pytest -q` before every commit. All tests must pass.
6. **Don't change a test's expected result to make it pass.** If the live system behaves differently from this plan, record it as a finding. Fix the code only if the plan's expectation is right.
7. Don't print or commit secrets (tokens, keys). Context Tokens in logs should be truncated to the first 20 characters.

**Things that will catch you out:**
- **State is held in memory.** The gateway (max 1 instance) holds replay, temporal and escrow state. Control (max 1) holds modes. Both scale to zero when idle, which wipes that state. Run each phase in one sitting. If a result looks like the state was lost, check the logs for `Started server process` and note it.
- **Every `/mcp` call needs a new JSON-RPC `id`.** The exception is the replay test. A reused id is refused with `REPLAY`.
- **Bindings in large tokens.** When a token has more than 20 bindings, they are committed as a Merkle root. The `/v1/tokens` response includes `bindings.<tool>[i] = {binding, proof}`. A write call must send `params._meta.binding_proof = <that entry>`, and its arguments must come from `entry.binding`. See `agent/runner.py` lines 90–110.
- **Faults replace, they don't add up.** `harness.bq_load --fault` regenerates the base table and applies one fault. To go back to healthy, run `bq_load ucNN` again without `--fault`.
- **Freshness depends on time.** Some products have short SLAs (UC04 FX, UC09 SCADA), so their tier changes within seconds of loading. Record the age reported by `/v1/quality` alongside the tier. Reload `uc01` right before phase B, because its one-hour SLA expires.
- **Cloud Run reserves paths ending in `z`.** Use `/health`, not `/healthz`.

Setup for every shell:
```bash
cd ~/CENE_E2E_GCP && source .venv/bin/activate
PROJECT=<gcp-project-id>
CONTROL=$(terraform -chdir=deploy/terraform output -raw control_url)
GATEWAY=$(terraform -chdir=deploy/terraform output -raw gateway_url)
ID=$(gcloud auth print-identity-token)     # refresh about every 50 minutes
```

## 1. Deploy

1. `git pull`. Confirm all three services run the newest tag:
   ```bash
   gcloud run services describe cene-<svc> --region australia-southeast2 \
     --format='value(spec.template.spec.containers[0].image)'
   ```
2. After any code fix, rebuild and redeploy:
   ```bash
   gcloud builds submit --tag .../cene:vN .
   terraform -chdir=deploy/terraform apply -var image_tag=vN
   ```
3. Then run `terraform -chdir=deploy/terraform plan`. It must show no changes other than the image.

## 2. Tests and expected results

Write a script, `harness/gcp_e2e.py`, that runs phases A–F against the live URLs. It should:
- use `requests`, with `--control-url`, `--gateway-url` and `--id-token` arguments;
- print one line per check: `ID | expected | actual | PASS/FAIL`;
- write a JSON results file.

Keep it as reusable as `harness/runner.py`.

### A. Platform health

| ID | Check | Expected |
|---|---|---|
| A1 | `GET /health` on control and gateway, with the ID token | 200, `{"ok":true,"role":"control"}` / `"gateway"` |
| A2 | The same call **without** the ID token | 401 or 403 from Cloud Run. The request never reaches the app |
| A3 | Cloud Run images | All three services run the same tag |
| A4 | `cene-tools` IAM | Only the gateway's service account can invoke it. A direct call from the user's account gets 403 |

### B. UC01 accounts-payable flow (live: KMS-signed tokens, BigQuery data)

Before starting, run `python -m harness.bq_load uc01 --project $PROJECT`.

| ID | Step | Expected |
|---|---|---|
| B1 | `GET /v1/quality/ap_invoices` | `tier: 1`; `fresh` vector at least 0.9 |
| B2 | `POST /v1/tokens` `{"agent_id":"ap-agent","task":"pay","products":["ap_invoices"]}` | `tier: 1`; tools `lookup_invoice: direct`, `approve_payment: direct`; token shorter than 4 KB; `bindings.approve_payment` present (Merkle) |
| B3 | `lookup_invoice INV-000001` | HTTP 200; `result` contains the row, with JSON types: `amount` is a number, nulls are `null` |
| B4 | `approve_payment` using binding *i* (an open invoice) with its proof | 200, decision `allowed` |
| B5 | The same invoice again, with a new request id | 403, `TEMPORAL_VIOLATION` |
| B6 | Binding *j* with `account_id` set to `ACC-9999` (redirect) | 403, `BINDING_VIOLATION` |
| B7 | Supplier A's invoice paid to supplier B's real account (swap) | 403, `BINDING_VIOLATION` |
| B8 | Amount 1.5 × the bound amount | 403. Record the code (`BINDING_VIOLATION` or `ARG_OUT_OF_BOUNDS`) |
| B9 | Tool not in the token, e.g. `delete_vendor` | 403, `TOOL_NOT_ALLOWED` |
| B10 | `lookup_invoice` with id `fixed-1`, then the same id again | First 200, second 403 `REPLAY` |
| B11 | No `X-Context-Token` header | 403, `TOKEN_MISSING` |
| B12 | Token with one changed character in the signature part | 403, `TOKEN_BAD_SIGNATURE` |
| B13 | Token with its payload edited (e.g. `tier` changed) and the original signature kept | 403, `TOKEN_BAD_SIGNATURE` |
| B14 | `GET /v1/executed` cannot be reached by the user (tools service) | Covered by A4. Instead, count `allowed` payments in the gateway audit records: exactly 1 |

### C. Tier 2 and two-person escrow (UC01, continued)

Use two real identities:
- **Approver 1** is the user: `$ID`.
- **Approver 2** is the agent service account:
  ```bash
  gcloud auth print-identity-token \
    --impersonate-service-account=$(terraform -chdir=deploy/terraform output -raw agent_service_account) \
    --audiences=$GATEWAY
  ```
  If impersonation is refused, grant the user `roles/iam.serviceAccountTokenCreator` on that one service account only (with a Terraform change or a one-off `gcloud` command), and note it in the report.

| ID | Step | Expected |
|---|---|---|
| C1 | `python -m harness.bq_load uc01 --project $PROJECT --fault '{"type":"stale","product":"ap_invoices","age_seconds":5400}'`, then `/v1/quality/ap_invoices` | `tier: 2`; `fresh` below 0.9; reasons mention freshness |
| C2 | New token | `tier: 2`; `approve_payment: escrow` |
| C3 | `approve_payment` with a valid binding | 200, decision `escrow`, with an escrow item id |
| C4 | `GET /v1/escrow` | The item is listed as pending |
| C5 | Approve as approver 1 | `status: pending` |
| C6 | Approve again as approver 1 | 409, `DUPLICATE_APPROVER` |
| C7 | Approve as approver 2 | `status: executed` |
| C8 | Approve an already-executed item | 409, `ESCROW_CLOSED` |
| C9 | Approve with no ID token (via a path that reaches the app) | `APPROVER_UNKNOWN` or 401/403 |

**Known design gap to confirm and record, not hide:**
- On Google Cloud, a token's `agent_id` comes from the request body, not from the caller's identity. `SELF_APPROVAL` only compares the approver with that string. So the person who requested the token could approve the escrow themselves.
- Test this: request a token as the user, then approve as the same user. Expected with the current code: the first approval is accepted, which is the gap.
- Proposed fix, as a separate commit: in `gcp` mode, record the caller's ID-token email in the token (`req_by`). Escrow then refuses that identity as an approver with `SELF_APPROVAL`. Optionally, only allow `agent_id`s that are mapped to the caller.

Finally, reload `uc01` without a fault and confirm the product is back to Tier 1.

### D. Data-quality tiers on BigQuery, all 12 use cases

For each row:
1. Run `bq_load ucNN` (healthy) and check the tier.
2. Run `bq_load ucNN --fault '<fault>'` and check the tier again.
3. Run `bq_load ucNN` to reset.

Expected tiers come from the local scenario files. Run `python -m harness.runner -v` first, and save its report for comparison.

| UC | Product | Healthy | Fault | Expected after fault |
|---|---|---|---|---|
| 01 | ap_invoices | 1 | `{"type":"stale","product":"ap_invoices","age_seconds":5400}` | 2 |
| 02 | contractor_invoices | 1 | `{"type":"regenerate","product":"contractor_invoices","profile":"stress","rows":300,"seed":21,"start":1000}` | 3 |
| 02 | contractor_invoices | 1 | `{"type":"duplicate_cluster","product":"contractor_invoices","template_row":1,"count":30,"id_column":"invoice_id","id_prefix":"CI-","id_start":910000}` | 3 |
| 03 | lab_results | 1 | `{"type":"scale_column","product":"lab_results","column":"creatinine","factor":88.4,"fraction":0.3,"set":{"creatinine_unit":"umol/L"}}` | 3 |
| 04 | fx_ticks | 1 (check within seconds of loading) | none: wait past the SLA | 3 (record age) |
| 05 | vendor_bids | 1 | `{"type":"lineage","node":"approved_spec_repo","health":0.55}` | 2 |
| 06 | orders | 1 | none | 1 |
| 07 | credit_applications | 1 | `{"type":"set_value","product":"credit_applications","where":{"app_id":"LA-00030"},"set":{"bureau_age_days":45}}` | 3 |
| 08 | adverse_events | 1 | `{"type":"null_spike","product":"adverse_events","column":"severity_code","fraction":0.05}` | 3 |
| 08 | adverse_events | 1 | `{"type":"lineage","node":"cro_pipeline_v4","health":0.0}` | 3 |
| 09 | scada_telemetry | 1 (check within seconds) | none: wait past the SLA | 3 (record age) |
| 11 | engine_telemetry | 1 | `{"type":"set_value","product":"engine_telemetry","where":{"tail_number":"VH-QPA"},"set":{"vibration_ips":0.0}}` | 3 |
| 12 | benefit_claims | 1 | `{"type":"set_value","product":"benefit_claims","fraction":0.1,"set":{"income_source":"averaged_annual"}}` | 3 |

For each check, record the tier, the score, the five vectors and the reasons. **Any tier that differs from the local result is a finding.** Note whether the difference comes from BigQuery types (e.g. NULL handling, numeric types) or from timing.

After a lineage fault, reloading without `--fault` appends healthy lineage rows, which resets it.

Out of scope on Google Cloud this run (local harness only):
- UC02 oracle-signed modes. This needs the user in `oracle_signers`. If they are listed in `terraform.tfvars`, run runbook §2 "Declare an emergency mode" and expect the token's modes to change.
- UC10 delegation through `/v1/tokens/delegate`. Optional: depth 1 should work; depth 3 → `DELEGATION_TOO_DEEP`; widening a limit → `DELEGATION_ESCALATION`.

### E. Audit log on Cloud Storage

1. Write `harness/gcs_audit_verify.py`. It should:
   - list `gs://<gcp-project-id>-cene-audit/audit/<service>/<chain id>/`;
   - download every chain, sort the records by `seq`, and run `cene.audit.verify_records`;
   - get the audit public key with `gcloud kms keys versions get-public-key 1 --key audit --keyring cene --location australia-southeast2`.
2. Expected results:

| ID | Check | Expected |
|---|---|---|
| E1 | Each chain folder | `valid: true`. Chains started under the old naming (`audit/0000…`, before v3) are listed separately as legacy |
| E2 | Download one chain, change one field of one event **in the local copy**, verify again | `valid: false`, naming the sequence number |
| E3 | Try to overwrite an existing object as the gateway's service account (impersonate, or note it as inferred from IAM) | 403 (create-only) |
| E4 | Every decision from phases B and C appears as an event | Counts match the checks run |

Don't modify or delete objects in the bucket.

### F. Latency

| ID | Measure | Report |
|---|---|---|
| F1 | 100 `lookup_invoice` calls through the gateway (warm, after 5 discarded calls) | p50, p95, p99 in ms |
| F2 | 20 token issues | p50, p95 |
| F3 | Cold start: the first call after scaling to zero (wait 15 minutes or redeploy) | ms, reported separately |
| F4 | `bq query` time for the lookup on its own (from the tools service's logs, or a timed `bq` call) | ms, to show how much of F1 is BigQuery |

These are prototype numbers on a scale-to-zero setup. Report them as measured. Don't compare them with the Kaggle sidecar figures as if they were like for like.

### G. Optional: Model Armor

Only run this if the user has created a Model Armor template and set `model_armor_template`.

| ID | Check | Expected |
|---|---|---|
| G1 | Benign invoice note | Not flagged; the session isn't tainted |
| G2 | One prompt-injection case copied verbatim from AgentDojo or InjecAgent, placed in `vendor_notes` with a `set_value` fault, then read with `lookup_invoice` | Flagged; the next `approve_payment` in that session → `escrow` (taint) |

If no template exists, skip this and say so.

## 3. Report

Write `docs/results/gcp_run_<yyyymmdd>.md` and the raw `docs/results/gcp_run_<yyyymmdd>.json`, then commit them on the branch. The report should have these sections:

1. **Environment:** image tag, commit hash, date and time (UTC), Terraform variables (no secrets).
2. **Summary table:** passed, failed and skipped counts for each phase.
3. **Every check:** ID, expected, actual, PASS/FAIL, and evidence (HTTP status, decision code, trace id).
4. **Findings:**
   - each failure or difference, with the likely cause;
   - what was fixed (commit hash and test added);
   - what is left open.
5. **Latency table** (phase F).
6. **Changes to IAM or Terraform**, with the reason for each.
7. **For the research paper:** a short paragraph listing which results are now measured on Google Cloud (signing, verification, binding enforcement, temporal rules, escrow, fail-closed, quality tiers on BigQuery, audit verification, latency). It should also state what still isn't measured: LLM agent attack rates on a published benchmark, scale-out with Redis, HSM keys, and real-world data.

Stop after the report and tell the user:
- which branch to merge;
- which image tag is live;
- any decision that needs them, e.g. the agent identity fix, Model Armor, or adding themselves to `oracle_signers`.
