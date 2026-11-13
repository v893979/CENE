# CENE: Context-Enforcement Network Engine

CENE stops an autonomous AI agent from acting on bad data or on instructions outside its task, and it can prove every decision afterwards. It sits outside the agent, between the model and its tools, so a prompt injection that takes over the model still cannot make an out-of-scope action happen.

**Author:** Vaibhav Rastogi, Independent Researcher, Melbourne, Australia (ORCID [0009-0006-0900-4376](https://orcid.org/0009-0006-0900-4376)). Related to AU patent application 2026208113.

## How it works

1. **Data quality gates the agent's authority.** Each data product the agent will use is scored on freshness, lineage, completeness, drift and integrity, with zero-tolerance rules on top. The score sets a tier:
   - Tier 1: the agent may act;
   - Tier 2: actions need two approvers;
   - Tier 3: no tools at all.
2. **A short-lived signed Context Token** (ES256; Cloud KMS or a local key) lists exactly which tools the task may call, with which arguments, on which rows (Merkle-committed bindings), and under which limits across calls.
3. **A gateway checks every tool call** against the token:
   - out-of-scope calls are refused with a reason code (`TOOL_NOT_ALLOWED`, `ARG_OUT_OF_BOUNDS`, `BINDING_VIOLATION`, `TEMPORAL_VIOLATION`, …);
   - risky calls are held in escrow until two people approve;
   - anything uncertain fails closed.
4. **Every decision goes into a hash-chained audit log** with signed checkpoints, held in write-once storage on Google Cloud.

## Repository layout

| Path | Contents |
|---|---|
| `cene/` | Core library: control plane, tokens, contracts, Merkle bindings, gateway, escrow, audit, quality engine, signers |
| `services/` | HTTP API. One image; the role (`control`, `gateway`, `tools`) is set by `CENE_SERVICE` |
| `agent/` | LLM agent runner that calls tools through the gateway |
| `harness/` | The 12 use-case scenarios, fault injection, calibration, BigQuery loader, Google Cloud end-to-end tests |
| `cene_agentdojo/` | AgentDojo evaluation: the adapter, policy `banking_v1`, runner, Kaggle notebook generator, model server, analysis scripts |
| `notebooks/agentdojo_cene.ipynb` | The Kaggle notebook used for the model runs |
| `data/` | Synthetic, seeded data products (`data/DATA_DICTIONARY.md`) |
| `tests/` | Unit, scenario and evaluation-code tests |
| `deploy/terraform/` | Example Google Cloud deployment. Everything is set through variables: copy `terraform.tfvars.example`, and pass the state bucket at `terraform init` (see `backend.tf`) |
| `docs/` | Objectives, requirements, design, low-level design, test scenarios, runbook, data, test plans, and `docs/results/` (result files and reports) |

## Run the tests

Python 3.11 or later.

```bash
pip install -r requirements.txt
python -m pytest
```

Some tests need extra packages and are skipped without them:
- the AgentDojo adapter tests need `agentdojo==0.1.35`;
- the model-server tests need `transformers==5.22.2`.

With both installed, the full suite is 91 tests.

To watch one use case step by step:

```bash
python -m harness.runner -v uc01
```

## Run the AgentDojo evaluation

The evaluation uses AgentDojo 0.1.35 (benchmark v1.2.2, banking suite, attack `important_instructions`), unchanged: its own tasks, attacks and defences. The plan, with every pre-registered rule and each amendment, is `docs/09_AgentDojo_Plan.md`.

**Model-free checks (G1, G2)** run on a laptop in a few minutes:

```bash
pip install agentdojo==0.1.35
python -m cene_agentdojo.run G1 G2 --out g1_g2.json
```

**Model runs (M1–M8)** cover four defences, each with and without the attack:
- M1/M2: no defence;
- M3/M4: CENE;
- M5/M6: spotlighting;
- M7/M8: the PI detector.

These were run on Kaggle (2 × T4) with Gemma 4 12B-it and Qwen3-8B in fp16, served by `cene_agentdojo/serve_hf.py`. The steps are in `docs/10_AgentDojo_Kaggle_Steps.md`; the settings are in `cene_agentdojo/run_config.yaml`.

**Analysis** of a folder of raw results:

```bash
python -m cene_agentdojo.analyse_m RAW_DIR m_results.json
python -m cene_agentdojo.compare_models compare.json gemma4-12b=m_results_gemma.json qwen3-8b=m_results_qwen.json
python -m cene_agentdojo.analyse_extra GEMMA_RAW_DIR QWEN_RAW_DIR extra.json
```

The raw episodes for both models are in the results dataset (see below). The reports are `docs/results/agentdojo/agentdojo_20261112.md` (Gemma) and `docs/results/agentdojo/agentdojo_model2_20261113.md` (Gemma vs Qwen).

## Pre-registration

The CENE policy for the AgentDojo evaluation was written before any attack or model run, then frozen. These are the files that make up the policy:
- the CENE library, `cene/`;
- the banking contract;
- the policy extraction, `policy.py`;
- the adapter;
- the hijack scoring.

All are byte-identical to the tag **`agentdojo-policy-v1`** (commit `7f04c1b510298bf0beafece8e3b59bd6e41d902d`) in the original private development repository. Every model run recorded that commit and confirmed the policy files unchanged. The tag's date can be shown to reviewers on request.

| File | SHA-256 |
|---|---|
| `cene/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `cene/audit.py` | `a0ecedb2dcf5f7c119a8f757c989a55c4c8d7f7c6b40fa0cb944066f1564953e` |
| `cene/contracts.py` | `5d1061b70e8a995b646ca81e7c2ea7b19089e398e4236bcb039509a941891efd` |
| `cene/control_plane.py` | `d0aa97cccee8c52547b7c5a00019bb93a7565d2d720b0d49414d785f462bcdbc` |
| `cene/datastore.py` | `47505ae4b2cc72c2e7c4a574188e7844bc09216b844708a9586a27259af83055` |
| `cene/escrow.py` | `3bf986786fb363f28989ebeea7ee73d6a86a3282e679dbee7d9dfcc2e81cb60d` |
| `cene/gateway.py` | `de7e2abacf1f729fa104420c830db55a4d241f726d711a2632e53c06188225bb` |
| `cene/merkle.py` | `6f31a308c7a53cb81618941f849e50eb28aab747524a43660090b0f9ef97996a` |
| `cene/modes.py` | `39f5cf48808c06941b2671505b29884111b33b0f0eeaf22944a0a1d176b5eccf` |
| `cene/platform.py` | `699a5154407641d17fc145ef09f18258bfa4636d4382a0c0f51f9e78ba3c6404` |
| `cene/quality/__init__.py` | `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855` |
| `cene/quality/engine.py` | `c51968c800d0c868252d5d5212ae2ee9ea3b4b6ee47c8cc1653c45bae92db9d9` |
| `cene/quality/metrics.py` | `5314f8f92316bd5023b7ad3977ea0f8e5968a02ae523f8418b5a688c0a189c9a` |
| `cene/screening.py` | `d42e9183573b522c3fe745d1591016efa81a60080eb7c0643b9785e8fc0b288d` |
| `cene/signer.py` | `cd1be1a8db4ecd1f9d09586ff494a69e55bcf821f1df90538ddfc7fffdad40d1` |
| `cene/stores.py` | `00529e0d1702d22dc6314dea943e13b2846923d2904201c4738b653129538ca5` |
| `cene/tokens.py` | `ba8c0c7261907aac2b34cc1319ae05dd8d859b939a5b765e24c43d83e342ce32` |
| `cene/tools.py` | `453cbec62285d23d163b79a64490a2e38c5b1fe2802628a944cceb4d84664aa3` |
| `cene/util.py` | `a0961ce37e46fad686d6b2a9946f47dd1b01ea647f4a59086bdf920a7466b814` |
| `cene_agentdojo/adapter.py` | `3d205aa694ab1d5a5de35c158a3c6764e04344a0b507872fd14568f3abc92d41` |
| `cene_agentdojo/contracts/banking_v1.yaml` | `08161857ea4702657fe72ef393ea437e6063ff512c0d8ca8dd909ca25fc29dc0` |
| `cene_agentdojo/hijack.py` | `2a584ed5ddf5a81a85f48364f4b117e0e559c53055ae525b949ad41f5063cba5` |
| `cene_agentdojo/policy.py` | `a3261d36d9fa90bb3a95aa2622e85dd279da16da742c2cb69e8a4a9f3d70c201` |

Check them with `sha256sum` (on Windows, check out with `core.autocrlf=false` first: line-ending conversion changes the bytes).

The run settings were frozen the same way, as `agentdojo-run-r3` (Gemma 4) and `agentdojo-run-r4-model2` (Qwen3-8B) in the private repository. This public release is tagged `v1.0.0`.

## Placeholders

Deployment-specific values in the result files and docs are replaced by placeholders: `<gcp-project-id>`, `<cene-control-url>`, `<cene-gateway-url>`, `<cene-agent-service-account>`, `<tfstate-bucket>`, `<tester-google-account>`, `<kaggle-user>`. Set your own through `CENE_GCP_PROJECT`, the Terraform variables and the command-line flags.

## Results and paper

- **Paper:** reference to be added. Earlier versions are SSRN [7348880](https://ssrn.com/abstract=7348880) and [7633858](https://ssrn.com/abstract=7633858).
- **Results dataset:** *CENE results v1*, DOI to be added. It holds every raw result: laptop, Google Cloud, AgentDojo and Kaggle runs, with checksums and an index from each number in the paper to its source file.
- **How to cite:** see `CITATION.cff`.

## License

Apache License 2.0: see `LICENSE` and `NOTICE`. Copyright 2026 Vaibhav Rastogi.

Third-party components are listed in `NOTICE`:
- two response-template data structures copied from Hugging Face Transformers (Apache-2.0);
- AgentDojo task and environment text in the recorded results and test fixtures (AgentDojo, © its authors, MIT licence; `LICENSES/AgentDojo-MIT.txt`).
