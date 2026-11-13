# 06 — Deployment Runbook

## 0. Before you start

- Create a **dedicated Google Cloud project** (e.g. `cene-test-<yourname>`), so everything can be deleted in one step.
- Set **budget alerts**: Billing → Budgets & alerts → A$50 and A$100.
- Install the tools (§0.1): Google Cloud CLI (`gcloud`), Terraform ≥ 1.5, Python 3.11+, Docker (optional).
- New accounts usually come with US$300 free credit. GPU quota for new projects is often 0, so request L4 quota in `australia-southeast1` or `-2` early (IAM & Admin → Quotas).

### 0.1 Installing the tools

**Easiest option: Google Cloud Shell.** Click *Activate Cloud Shell* in the Google Cloud console. It already has `gcloud`, Terraform and Python, so nothing needs installing. Upload or `git clone` this folder there.

**Windows (PowerShell):**

```powershell
winget install --id Hashicorp.Terraform --exact      # Terraform  (or: choco install terraform -y)
winget install --id Google.CloudSDK --exact          # Google Cloud CLI (or the installer from cloud.google.com/sdk)
winget install --id Python.Python.3.12 --exact       # Python, if not already installed
# close and reopen PowerShell so the new PATH is picked up, then check:
terraform -version
gcloud --version
python --version
```

**macOS (Homebrew):**

```bash
brew tap hashicorp/tap && brew install hashicorp/tap/terraform
brew install --cask google-cloud-sdk
```

**Ubuntu / Debian (HashiCorp's apt repository):**

```bash
wget -O- https://apt.releases.hashicorp.com/gpg | sudo gpg --dearmor -o /usr/share/keyrings/hashicorp-archive-keyring.gpg
echo "deb [signed-by=/usr/share/keyrings/hashicorp-archive-keyring.gpg] https://apt.releases.hashicorp.com $(lsb_release -cs) main" | sudo tee /etc/apt/sources.list.d/hashicorp.list
sudo apt update && sudo apt install terraform
```

Then sign in and check the Terraform code before creating anything:

```bash
gcloud auth login
gcloud auth application-default login          # credentials Terraform and the Python scripts use
cd deploy/terraform
terraform init                                  # downloads the Google provider (>= 6.0)
terraform validate                              # checks the configuration; creates nothing
terraform fmt -check                            # optional: formatting check
```

`terraform validate` has not yet been run on this configuration. If it reports an error, fix it before `terraform apply`, or send me the message.

## 1. Stage 1a — run everything locally (free)

```bash
cd CENE_E2E_GCP
python -m venv .venv && source .venv/bin/activate      # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m pytest                                        # 31 tests: 19 unit tests + 12 scenarios
python -m harness.runner -v uc01                        # watch one use case step by step
```

To try the HTTP API locally:

```bash
CENE_SERVICE=all CENE_SEED_SCENARIO=uc01 uvicorn services.app:app --port 8080
# PowerShell: $env:CENE_SERVICE="all"; $env:CENE_SEED_SCENARIO="uc01"; uvicorn services.app:app --port 8080
curl localhost:8080/v1/quality/ap_invoices
curl -X POST localhost:8080/v1/tokens -H "Content-Type: application/json" \
     -d '{"agent_id":"ap-agent","task":"pay","products":["ap_invoices"]}'
```

With Docker: `docker compose up --build` (single "all" service plus Redis).

## 2. Stage 1b — deploy to Google Cloud

Bash (Cloud Shell, macOS, Linux). In PowerShell, set variables with `$env:PROJECT="cene-test-yourname"; $env:REGION="australia-southeast2"` and use `$env:PROJECT` in place of `$PROJECT`.

```bash
export PROJECT=cene-test-yourname REGION=australia-southeast2
gcloud config set project $PROJECT
gcloud auth application-default login

# Terraform needs these two APIs before it can enable the others (new projects often have them off)
gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com --project=$PROJECT

cd deploy/terraform
cp terraform.tfvars.example terraform.tfvars          # set project_id, your email in oracle_signers/operators
terraform init
# 1) APIs and the image repository first
terraform apply -target=google_project_service.apis -target=google_artifact_registry_repository.repo
# 2) build and push the image (from the repo root)
cd ../.. && gcloud builds submit --tag $REGION-docker.pkg.dev/$PROJECT/cene/cene:v1 . && cd deploy/terraform
# 3) everything else
terraform apply
terraform output
```

Load test data into BigQuery, one scenario at a time:

```bash
cd ../..
python -m harness.bq_load uc01 --project $PROJECT --dataset cene
```

The loader creates the `cene` dataset and its two control tables (`_cene_load_log`, `_cene_lineage_health`) if they don't exist. So you can also load and explore data in BigQuery **before** running Terraform; the person running it needs BigQuery Admin, or Data Editor + Job User. Use the same location as Terraform (`--location australia-southeast2`, the default).

If the loader created the dataset first, tell Terraform to adopt it instead of trying to create it again (otherwise `terraform apply` stops with *Already exists*):

```bash
cd deploy/terraform
terraform import google_bigquery_dataset.cene projects/$PROJECT/datasets/cene
terraform import google_bigquery_table.load_log projects/$PROJECT/datasets/cene/tables/_cene_load_log
terraform import google_bigquery_table.lineage projects/$PROJECT/datasets/cene/tables/_cene_lineage_health
terraform apply
```

Call the services. Cloud Run requires an identity token, and the Context Token goes in its own header:

```bash
CONTROL=$(terraform -chdir=deploy/terraform output -raw control_url)
GATEWAY=$(terraform -chdir=deploy/terraform output -raw gateway_url)
ID=$(gcloud auth print-identity-token)
curl -H "Authorization: Bearer $ID" $CONTROL/v1/quality/ap_invoices
curl -H "Authorization: Bearer $ID" -H "Content-Type: application/json" -X POST $CONTROL/v1/tokens \
     -d '{"agent_id":"ap-agent","task":"pay","products":["ap_invoices"]}' > token.json
# tool call: Authorization = your identity, X-Context-Token = the CENE token
curl -H "Authorization: Bearer $ID" -H "X-Context-Token: $(jq -r .token token.json)" \
     -H "Content-Type: application/json" -X POST $GATEWAY/mcp \
     -d '{"jsonrpc":"2.0","id":"1","method":"tools/call","params":{"name":"lookup_invoice","arguments":{"invoice_id":"INV-000001"}}}'
```

Apply a fault and re-check:

```bash
python -m harness.bq_load uc03 --project $PROJECT --fault '{"type":"scale_column","product":"lab_results","column":"creatinine","factor":88.4,"fraction":0.3,"set":{"creatinine_unit":"umol/L"}}'
curl -H "Authorization: Bearer $ID" $CONTROL/v1/quality/lab_results     # expect tier 3
```

Approve an escrowed action. Your identity becomes the approver, and two different people are needed:

```bash
curl -H "Authorization: Bearer $ID" $GATEWAY/v1/escrow
curl -H "Authorization: Bearer $ID" -X POST -d '{}' $GATEWAY/v1/escrow/<id>/approve
```

Declare an emergency mode (UC02). Only principals in `oracle_signers` can do this:

```bash
python -m harness.sign_mode --product contractor_invoices --mode stress --hours 12 --reason "Cyclone declared" \
  --oracle-key $(terraform -chdir=deploy/terraform output -raw oracle_key_version) \
  --control-url $CONTROL --id-token $ID
```

## 3. Stage 1c — run the LLM agent through the gateway

**Option A, GPU VM with vLLM.**
- Create a VM with one NVIDIA L4 (e.g. `g2-standard-8`), a Deep Learning VM image, and a 100 GB disk.
- Serve an open model with vLLM's OpenAI-compatible server: `vllm serve <model id> --max-model-len 8192`. Check that your vLLM version supports the model.
- A 12B model in 16-bit weights is about 24 GB, which is too tight for one 24 GB L4. Use a quantised checkpoint or `g2-standard-24` (2× L4).
- **Stop the VM after each test batch.**

**Option B, Vertex AI.** Use the Vertex AI OpenAI-compatible endpoint with a hosted model. This is easier, but the results aren't comparable with your Kaggle Gemma results.

**Option C, Kaggle (development only).** Run vLLM in a Kaggle notebook and point the runner at it.

```bash
python -m agent.runner --llm-url http://VM_IP:8000/v1 --model <model id> \
  --control-url $CONTROL --gateway-url $GATEWAY --id-token $(gcloud auth print-identity-token) \
  --tasks agent/tasks_example.jsonl --out traces.jsonl
```

For prompt-injection measurement, follow `05_Test_Scenarios.md` §5. Use benchmark test cases only.

## 4. Hardening switches

| Change | How |
|---|---|
| Keys in Cloud HSM | `kms_protection_level = "HSM"` creates new keys. Update the env vars or re-apply. Gateways pick up the new public key on restart |
| Scale out | `enable_redis = true`; move escrow and modes to Firestore before raising `max_instance_count` above 1 |
| Write-once audit | `lock_audit_retention = true`. **Irreversible:** objects can't be deleted until retention ends |
| Atomic temporal checks | Add a Lua script per rule in `RedisStore` (LLD §6.2) |
| Content screening with Model Armor | Create a Model Armor template in the console (enable prompt-injection/jailbreak and malicious-URL filters), then set `model_armor_template = "projects/P/locations/L/templates/T"` and re-apply. Terraform enables the API and grants the gateway `roles/modelarmor.user` |

## 5. Teardown

```bash
cd deploy/terraform && terraform destroy
# or delete the whole project:
gcloud projects delete $PROJECT
```

If you locked the audit bucket, its objects (and therefore the bucket) remain until the retention period ends.

## 6. Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `Cloud Resource Manager API has not been used in project ... or it is disabled` | New project without that API | `gcloud services enable cloudresourcemanager.googleapis.com serviceusage.googleapis.com`, wait a minute, re-run `terraform apply` |
| `terraform` or `gcloud` not recognised (Windows) | PATH not refreshed after install | Close and reopen PowerShell; check `winget list Hashicorp.Terraform` |
| `terraform apply`: dataset *Already exists* | The loader created it first | Run the three `terraform import` commands in §2 |
| `bq_load`: *Permission denied* creating the dataset | Your account lacks BigQuery Admin | Create the dataset with Terraform first, or grant yourself BigQuery Admin on the test project |
| 401/403 from Cloud Run before reaching the app | Missing identity token, or caller lacks `run.invoker` | `gcloud auth print-identity-token`; add yourself to `operators` |
| Every gateway call `TOKEN_UNKNOWN_KEY` | Gateway can't read the token public key, or keys were rotated | Check `publicKeyViewer` on the token key; restart the gateway |
| `TOKEN_EXPIRED` immediately | Tier-1 token for a product with a short SLA, or clock skew | Expected for short SLAs (UC04/UC09); otherwise check `/v1/quality` age |
| Everything Tier 3 with `no_load_record` | `_cene_load_log` has no row for the product | Run `harness.bq_load`, or insert a load record |
| Everything Tier 3 with drift reasons | No reference table, or reference from a different distribution | Load `<product>__ref_baseline`; run calibration |
| `STATE_UNAVAILABLE` | Redis unreachable from Cloud Run | Check the VPC connector and `CENE_REDIS_URL`. This is fail-closed behaviour working as designed |
| Escrow approve returns `APPROVER_UNKNOWN` | Call made without an identity token | Call with `Authorization: Bearer $(gcloud auth print-identity-token)` |
