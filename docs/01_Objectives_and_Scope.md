# 01 — Objectives and Scope

**Project:** CENE end-to-end platform (Context-Enforcement Network Engine)
**Author:** Vaibhav Rastogi, Independent Researcher, Melbourne, Australia
**Version:** 1.0 · November 2026
**Related work:** AU patent application 2026208113; field guide *Architecting Deterministic Governance for Autonomous AI Agents on Google Cloud Platform* (Chapters 4 and 7–13, Appendix G); Kaggle prototype `<kaggle-user>/cern-prototype` (version 365076142).

---

## 1. Purpose

The Kaggle prototype tested the parts of the architecture one at a time, inside one notebook. This project builds the **whole path as a running system**, from a governed data product to an agent's action on a real API. It then tests that path against the twelve use cases in the field guide.

The question it answers: **can an autonomous agent be stopped from acting on bad data or out-of-scope instructions without stopping it from doing its normal work, and can every decision be proven afterwards?**

## 2. Objectives

| ID | Objective | How success is measured |
|---|---|---|
| O1 | Score every data product an agent uses, in real time, on five quality measures, and turn the score into a capability tier | All 12 scenarios reach the expected tier; false-alarm rate on healthy data ≤ 5% (calibration run) |
| O2 | Give each agent task a short-lived, signed Context Token that lists exactly which tools it may use, with which arguments, on which rows | Tokens verify only with the issuing key; any edit, forgery or expiry is refused |
| O3 | Enforce the token on every tool call, outside the agent's process, and fail closed | Every out-of-scope call in the scenarios is denied with the correct reason code; no out-of-scope call executes |
| O4 | Hold risky actions for two different people, and stop actions that are fine alone but harmful together | Escrow needs two distinct approvers, never the agent; duplicate, velocity and per-address limits hold across sessions |
| O5 | Keep a tamper-evident record of every decision | Audit chain verifies; editing any record is detected |
| O6 | Run on Google Cloud with keys in Cloud KMS / Cloud HSM, and measure overhead | Stage 1 deployed by Terraform; p50/p95/p99 latency reported under load |
| O7 | Measure prompt-injection resistance on a recognised public benchmark | Attack success and task completion, with and without the gateway, on AgentDojo (banking) or InjecAgent |

## 3. Scope

**In scope (Stage 1, this repository):**
- quality engine (freshness, lineage, completeness, drift, integrity; zero-tolerance rules; gate; tiers);
- data contracts in YAML;
- control plane: token issue, delegation, signed emergency modes;
- user-space enforcement gateway (MCP JSON-RPC);
- two-person escrow;
- mock tool servers;
- hash-chained audit log with signed checkpoints;
- LLM agent runner;
- scenario harness for the 12 use cases;
- calibration tool;
- local mode (laptop) and Google Cloud mode (Cloud Run, BigQuery, KMS/HSM, Cloud Storage, optional Memorystore);
- Terraform.

**Stage 2 (designed in `04_Low_Level_Design.md` §12, not built here):** the in-kernel eBPF enforcer on a Linux VM, then GKE Standard:
- a BPF LSM connect hook;
- TLS uprobes;
- cgroup egress filtering;
- `bpf_sock_destroy`.

**Stage 3:** load test on a small GKE cluster, then tear down.

**Out of scope:**
- real payment, clinical, SCADA or banking systems (mock tools stand in);
- production identity federation;
- multi-region availability;
- Dataplex integration beyond the BigQuery control tables (a later option);
- writing new prompt-injection attacks. Injection resistance is measured only with published benchmark suites.

## 4. Stages and deliverables

| Stage | Where | Deliverable | Rough cost |
|---|---|---|---|
| 1a | Laptop / this repo | All 12 scenarios pass locally; 31 automated tests | Free |
| 1b | Google Cloud (Cloud Run, BigQuery, KMS, Storage) | Same scenarios against deployed services; agent runs through the gateway | ~A$20–60/month at low use; switch KMS to HSM adds a few dollars/month |
| 1c | GPU VM (L4) or Kaggle | Gemma agent on benchmark tasks with and without the gateway | GPU hours only while testing |
| 2 | Compute Engine Ubuntu VM | eBPF enforcer; bypass test; latency vs user-space gateway | A few dollars/month |
| 3 | GKE Standard (temporary) | Load test, then delete | Node hours for a few days |

## 5. Assumptions and constraints

- Personal project in a dedicated Google Cloud project, with budget alerts set.
- No real personal, financial or health data. All data is synthetic or from public datasets.
- Single region (Melbourne, `australia-southeast2`).
- Without Redis, state lives in memory, so the gateway and control plane run as single instances. That's acceptable for Stage 1 testing.
- The gateway closes the HTTP connection on a denial. A kernel-level TCP reset is Stage 2.

## 6. Glossary

| Term | Meaning |
|---|---|
| Data product | A governed table an agent reads (e.g. `ap_invoices`) |
| Data contract (ADC) | YAML file defining a product's columns, rules, SLAs, tools, limits |
| d-CQI | Weighted sum of five quality measures |
| Gate (Φ) | Product of per-measure penalties; any weak measure pulls the score down |
| Γ (zero-tolerance) | 0 if any zero-tolerance rule is broken, else 1 |
| Tier 1 / 2 / 3 | Direct action / two-person escrow / no tools |
| Context Token | Signed JWS listing a task's tools, argument bounds, bound rows, limits and expiry |
| Binding | A specific combination of argument values the token allows (e.g. this invoice → this account → at most this amount) |
| Temporal rule | A limit across calls: once per key, count within a window, sum within a window |
| Taint | Marker on a session after it reads untrusted content; later write actions go to escrow |
| Escrow | Holding area where an action waits for two distinct approvers |
| Oracle key | Separate KMS key used only to sign emergency-mode statements |
