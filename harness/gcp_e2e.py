"""Live end-to-end checks against the deployed CENE services on Google Cloud (docs/08_GCP_Test_Plan.md).

    python -m harness.gcp_e2e --phases A,B,C --out results.json
    python -m harness.gcp_e2e --phases D --out d.json
    python -m harness.gcp_e2e --phases F --cold --out f.json      # F3: first call is measured as cold

URLs default to the Cloud Run services in --project/--region; identity tokens come from gcloud
(refreshed every 45 minutes) unless --id-token is given. Prints one line per check:
    ID | expected | actual | PASS/FAIL
and writes every check, with its evidence (HTTP status, decision, trace id), to --out.
Context Tokens are never printed or saved in full (first 20 characters only).
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import shutil
import statistics
import subprocess
import sys
import time
import uuid
from pathlib import Path

import requests

PROJECT = os.environ.get("CENE_GCP_PROJECT", "<gcp-project-id>")
REGION = "australia-southeast2"
AUDIT_BUCKET = f"{PROJECT}-cene-audit"

# Phase D: (uc, product, fault or None, expected tier after the fault, wait_seconds for SLA-expiry rows)
PHASE_D = [
    ("uc01", "ap_invoices", {"type": "stale", "product": "ap_invoices", "age_seconds": 5400}, 2, 0),
    ("uc02", "contractor_invoices", {"type": "regenerate", "product": "contractor_invoices", "profile": "stress",
                                     "rows": 300, "seed": 21, "start": 1000}, 3, 0),
    ("uc02", "contractor_invoices", {"type": "duplicate_cluster", "product": "contractor_invoices",
                                     "template_row": 1, "count": 30, "id_column": "invoice_id", "id_prefix": "CI-",
                                     "id_start": 910000}, 3, 0),
    ("uc03", "lab_results", {"type": "scale_column", "product": "lab_results", "column": "creatinine",
                             "factor": 88.4, "fraction": 0.3, "set": {"creatinine_unit": "umol/L"}}, 3, 0),
    ("uc04", "fx_ticks", None, 3, 280),
    ("uc05", "vendor_bids", {"type": "lineage", "node": "approved_spec_repo", "health": 0.55}, 2, 0),
    ("uc06", "orders", None, 1, 0),
    ("uc07", "credit_applications", {"type": "set_value", "product": "credit_applications",
                                     "where": {"app_id": "LA-00030"}, "set": {"bureau_age_days": 45}}, 3, 0),
    ("uc08", "adverse_events", {"type": "null_spike", "product": "adverse_events", "column": "severity_code",
                                "fraction": 0.05}, 3, 0),
    ("uc08", "adverse_events", {"type": "lineage", "node": "cro_pipeline_v4", "health": 0.0}, 3, 0),
    ("uc09", "scada_telemetry", None, 3, 15),
    ("uc11", "engine_telemetry", {"type": "set_value", "product": "engine_telemetry",
                                  "where": {"tail_number": "VH-QPA"}, "set": {"vibration_ips": 0.0}}, 3, 0),
    ("uc12", "benefit_claims", {"type": "set_value", "product": "benefit_claims", "fraction": 0.1,
                                "set": {"income_source": "averaged_annual"}}, 3, 0),
]


def gcloud(*args: str) -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd") or "gcloud"
    return subprocess.run([exe, *args], check=True, capture_output=True, text=True).stdout.strip()


def jwt_payload(tok: str) -> dict:
    p = tok.split(".")[1]
    return json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4)))


def short(tok: str | None) -> str:
    return (tok or "")[:20] + "..."


def pct(xs: list[float], q: float) -> float:
    xs = sorted(xs)
    k = max(0, min(len(xs) - 1, int(round(q / 100 * len(xs) + 0.5)) - 1))
    return round(xs[k], 1)


class Live:
    def __init__(self, a):
        self.project, self.region = a.project, a.region
        self.control = (a.control_url or self._url("cene-control")).rstrip("/")
        self.gateway = (a.gateway_url or self._url("cene-gateway")).rstrip("/")
        self.tools = (a.tools_url or self._url("cene-tools")).rstrip("/")
        self.agent_sa = a.agent_sa or f"cene-agent@{a.project}.iam.gserviceaccount.com"
        self._fixed_id, self._id, self._id_at, self._agent_id = a.id_token, None, 0.0, None
        self.user = gcloud("config", "get-value", "account")
        self.http = requests.Session()
        self.checks: list[dict] = []
        self.extra: dict = {}

    def _url(self, svc: str) -> str:
        return gcloud("run", "services", "describe", svc, "--region", self.region, "--project", self.project,
                      "--format=value(status.url)")

    # ------------------------------------------------------------------ identities
    @property
    def id(self) -> str:
        if self._fixed_id:
            return self._fixed_id
        if not self._id or time.time() - self._id_at > 2700:
            self._id, self._id_at = gcloud("auth", "print-identity-token"), time.time()
        return self._id

    def agent_id_token(self, audience: str | None = None) -> str:
        return self.sa_id_token(self.agent_sa, audience)

    def sa_id_token(self, sa: str, audience: str | None = None) -> str:
        """Identity token for a service account by impersonation (needs serviceAccountTokenCreator on it)."""
        audience = audience or self.gateway
        cache = self.__dict__.setdefault("_sa_ids", {})
        if (sa, audience) not in cache:
            cache[(sa, audience)] = gcloud("auth", "print-identity-token", f"--impersonate-service-account={sa}",
                                           f"--audiences={audience}", "--include-email")
        return cache[(sa, audience)]

    # ------------------------------------------------------------------ http helpers
    def get(self, url: str, auth: str | None = "user", **kw):
        h = {"Authorization": f"Bearer {self.id}"} if auth == "user" else {}
        t0 = time.perf_counter()
        r = self.http.get(url, headers=h, timeout=60, **kw)
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        return r

    def post(self, url: str, body: dict, bearer: str | None = None, headers: dict | None = None, auth=True):
        h = {"Content-Type": "application/json", **(headers or {})}
        if auth:
            h["Authorization"] = f"Bearer {bearer or self.id}"
        t0 = time.perf_counter()
        r = self.http.post(url, data=json.dumps(body), headers=h, timeout=60)
        r.elapsed_ms = (time.perf_counter() - t0) * 1000
        return r

    def quality(self, product: str) -> dict:
        r = self.get(f"{self.control}/v1/quality/{product}")
        r.raise_for_status()
        return r.json()

    def token(self, agent_id="ap-agent", task="pay", products=("ap_invoices",), bearer=None):
        r = self.post(f"{self.control}/v1/tokens", {"agent_id": agent_id, "task": task, "products": list(products)},
                      bearer=bearer)
        r.raise_for_status()
        return r.json(), r

    def mcp(self, token: str | None, tool: str, args: dict, rid: str | None = None, proof: dict | None = None,
            bearer: str | None = None):
        req = {"jsonrpc": "2.0", "id": rid or uuid.uuid4().hex, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
        if proof is not None:
            req["params"]["_meta"] = {"binding_proof": proof}
        h = {"X-Context-Token": token} if token else {}
        r = self.post(f"{self.gateway}/mcp", req, bearer=bearer, headers=h)
        try:
            body = r.json()
        except ValueError:
            body = {"raw": r.text[:200]}
        code = (body.get("error") or {}).get("message") if isinstance(body, dict) else None
        return r, body, code

    def escrow(self, item: str, bearer: str | None = None, auth=True):
        r = self.post(f"{self.gateway}/v1/escrow/{item}/approve", {}, bearer=bearer, auth=auth)
        try:
            return r, r.json()
        except ValueError:
            return r, {"raw": r.text[:120]}

    # ------------------------------------------------------------------ recording
    def check(self, cid: str, expected: str, actual: str, ok: bool | None, r=None, **evidence):
        status = ok if ok == "INFO" else ("SKIP" if ok is None else ("PASS" if ok else "FAIL"))
        if r is not None:
            evidence.setdefault("http", r.status_code)
            if r.headers.get("x-cene-decision"):
                evidence.setdefault("decision", r.headers["x-cene-decision"])
            tr = r.headers.get("x-cloud-trace-context", "").split("/")[0].split(";")[0]
            if tr:
                evidence.setdefault("trace", tr)
            evidence.setdefault("ms", round(getattr(r, "elapsed_ms", 0), 1))
        rec = {"id": cid, "expected": expected, "actual": actual, "status": status, "evidence": evidence,
               "utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        self.checks.append(rec)
        print(f"{cid:5s} | {expected} | {actual} | {status}", flush=True)
        return rec


def bq_load(project: str, uc: str, fault: dict | None = None) -> str:
    cmd = [sys.executable, "-m", "harness.bq_load", uc, "--project", project]
    if fault:
        cmd += ["--fault", json.dumps(fault)]
    out = subprocess.run(cmd, capture_output=True, text=True)
    if out.returncode != 0:
        raise RuntimeError(f"bq_load {uc} failed: {out.stderr[-600:]}")
    return out.stdout.strip()


def binding_args(entry: dict, amount_fraction: float = 1.0) -> dict:
    b = entry["binding"]
    args = dict(b["k"])
    for a, m in (b.get("max") or {}).items():
        args[a] = round(m * amount_fraction, 2)
    return args


# ====================================================================== phase A
def phase_a(L: Live):
    for role, url in (("control", L.control), ("gateway", L.gateway)):
        r = L.get(f"{url}/health")
        body = r.json() if r.ok else {}
        L.check(f"A1-{role[0]}", f'200 {{"ok":true,"role":"{role}"}}', f"{r.status_code} {json.dumps(body)}",
                r.status_code == 200 and body == {"ok": True, "role": role}, r)
    for role, url in (("control", L.control), ("gateway", L.gateway)):
        r = L.get(f"{url}/health", auth=None)
        from_app = r.headers.get("content-type", "").startswith("application/json")
        L.check(f"A2-{role[0]}", "401/403 from Cloud Run (app not reached)",
                f"{r.status_code} {'app JSON' if from_app else 'Cloud Run page'}",
                r.status_code in (401, 403) and not from_app, r)
    images = {s: gcloud("run", "services", "describe", f"cene-{s}", "--region", L.region, "--project", L.project,
                        "--format=value(spec.template.spec.containers[0].image)") for s in ("control", "gateway", "tools")}
    tags = {s: i.rsplit(":", 1)[-1] for s, i in images.items()}
    L.extra["images"] = images
    L.check("A3", "all three services on the same tag", json.dumps(tags), len(set(tags.values())) == 1)
    pol = json.loads(gcloud("run", "services", "get-iam-policy", "cene-tools", "--region", L.region,
                            "--project", L.project, "--format=json"))
    invokers = sorted(m for b in pol.get("bindings", []) if b["role"] == "roles/run.invoker" for m in b["members"])
    r = L.post(f"{L.tools}/v1/tools/lookup_invoice", {"arguments": {"invoice_id": "INV-000001"}})
    gw = f"serviceAccount:cene-gateway@{L.project}.iam.gserviceaccount.com"
    roles = gcloud("projects", "get-iam-policy", L.project, "--flatten=bindings[].members",
                   f"--filter=bindings.members:{L.user}", "--format=value(bindings.role)").split()
    L.check("A4", "only gateway SA invokes tools; user gets 403",
            f"invokers={invokers}; user call {r.status_code} (user project roles {roles})",
            invokers == [gw] and r.status_code == 403, r, user_project_roles=roles)
    try:
        r = L.post(f"{L.tools}/v1/tools/lookup_invoice", {"arguments": {"invoice_id": "INV-000001"}},
                   bearer=L.agent_id_token(L.tools))
        L.check("A4b", "non-owner identity (agent SA) calling tools gets 403", f"{r.status_code}",
                r.status_code == 403, r, caller=L.agent_sa)
    except subprocess.CalledProcessError as ex:
        L.check("A4b", "non-owner identity (agent SA) calling tools gets 403",
                f"impersonation refused: {ex.stderr.strip()[-160:]}", None)


# ====================================================================== phase B
def phase_b(L: Live, load: bool = True):
    if load:
        print("     loading uc01 ...", flush=True)
        bq_load(L.project, "uc01")
    q = L.quality("ap_invoices")
    L.check("B1", "tier 1; fresh >= 0.9", f"tier {q['tier']}; fresh {q['vectors'].get('fresh')}; "
            f"age {round(q['detail'].get('age_seconds') or -1)}s", q["tier"] == 1 and q["vectors"].get("fresh", 0) >= 0.9,
            vectors=q["vectors"], reasons=q["reasons"], score=q["score"])
    tok, r = L.token()
    T = tok["token"]
    claims = jwt_payload(T)
    L.extra["B_jti"] = claims["jti"]
    binds = tok.get("bindings", {}).get("approve_payment", [])
    ok = (tok["tier"] == 1 and tok["tools"].get("lookup_invoice") == "direct"
          and tok["tools"].get("approve_payment") == "direct" and len(T) < 4096 and len(binds) > 0
          and "bindings_root" in claims["tools"]["approve_payment"])
    L.check("B2", "tier 1; lookup/approve direct; token < 4 KB; Merkle bindings",
            f"tier {tok['tier']}; tools {tok['tools']}; {len(T)} B; {len(binds)} bindings", ok, r,
            token=short(T), jti=claims["jti"])

    r, body, code = L.mcp(T, "lookup_invoice", {"invoice_id": "INV-000001"})
    row = ((body.get("result") or {}).get("row") or {}) if r.ok else {}
    stringly_null = [k for k, v in row.items() if isinstance(v, str) and v.lower() in ("nan", "none", "nat", "null")]
    ok = r.status_code == 200 and isinstance(row.get("amount"), (int, float)) and not stringly_null
    L.check("B3", "200; row with JSON types (amount number, nulls null)",
            f"{r.status_code}; amount {type(row.get('amount')).__name__}; string-nulls {stringly_null}", ok, r, row=row)

    by_inv = {}
    for e in binds:
        by_inv.setdefault(e["binding"]["k"]["invoice_id"], e)
    pool = list(by_inv.values())
    # B4 / B5: pay binding 0, then the same invoice again
    e0 = pool[0]
    r, body, code = L.mcp(T, "approve_payment", binding_args(e0), proof=e0)
    L.check("B4", "200 allowed", f"{r.status_code} {r.headers.get('x-cene-decision')} {code or ''}".strip(),
            r.status_code == 200 and r.headers.get("x-cene-decision") == "allowed", r,
            invoice=e0["binding"]["k"]["invoice_id"])
    r, body, code = L.mcp(T, "approve_payment", binding_args(e0), proof=e0)
    L.check("B5", "403 TEMPORAL_VIOLATION", f"{r.status_code} {code}", r.status_code == 403 and code == "TEMPORAL_VIOLATION", r)
    # B6 redirect to an unknown account
    e1 = pool[1]
    args = {**binding_args(e1), "account_id": "ACC-9999"}
    r, body, code = L.mcp(T, "approve_payment", args, proof=e1)
    L.check("B6", "403 BINDING_VIOLATION", f"{r.status_code} {code}", r.status_code == 403 and code == "BINDING_VIOLATION", r)
    # B7 swap: supplier A's invoice paid to supplier B's real account
    e2 = pool[2]
    other = next((e for e in pool[3:] if e["binding"]["k"]["account_id"] != e2["binding"]["k"]["account_id"]), None)
    if other is None:
        L.check("B7", "403 BINDING_VIOLATION", "no binding with a different account", None)
    else:
        args = {**binding_args(e2), "account_id": other["binding"]["k"]["account_id"]}
        r, body, code = L.mcp(T, "approve_payment", args, proof=e2)
        L.check("B7", "403 BINDING_VIOLATION", f"{r.status_code} {code}",
                r.status_code == 403 and code == "BINDING_VIOLATION", r)
    # B8 amount 1.5x the bound amount
    e3 = pool[4]
    r, body, code = L.mcp(T, "approve_payment", binding_args(e3, 1.5), proof=e3)
    L.check("B8", "403 (BINDING_VIOLATION or ARG_OUT_OF_BOUNDS)", f"{r.status_code} {code}",
            r.status_code == 403 and code in ("BINDING_VIOLATION", "ARG_OUT_OF_BOUNDS"), r,
            bound=e3["binding"].get("max"))
    r, body, code = L.mcp(T, "delete_vendor", {"vendor_id": "V-001"})
    L.check("B9", "403 TOOL_NOT_ALLOWED", f"{r.status_code} {code}", r.status_code == 403 and code == "TOOL_NOT_ALLOWED", r)
    r1, _, c1 = L.mcp(T, "lookup_invoice", {"invoice_id": "INV-000002"}, rid="fixed-1")
    r2, _, c2 = L.mcp(T, "lookup_invoice", {"invoice_id": "INV-000002"}, rid="fixed-1")
    L.check("B10", "200 then 403 REPLAY", f"{r1.status_code} then {r2.status_code} {c2}",
            r1.status_code == 200 and r2.status_code == 403 and c2 == "REPLAY", r2)
    r, _, code = L.mcp(None, "lookup_invoice", {"invoice_id": "INV-000003"})
    L.check("B11", "403 TOKEN_MISSING", f"{r.status_code} {code}", r.status_code == 403 and code == "TOKEN_MISSING", r)
    h, p, s = T.split(".")
    i = len(s) // 2
    bad_sig = h + "." + p + "." + s[:i] + ("A" if s[i] != "A" else "B") + s[i + 1:]
    r, _, code = L.mcp(bad_sig, "lookup_invoice", {"invoice_id": "INV-000003"})
    L.check("B12", "403 TOKEN_BAD_SIGNATURE", f"{r.status_code} {code}",
            r.status_code == 403 and code == "TOKEN_BAD_SIGNATURE", r)
    edited = dict(claims, tier=0)
    p2 = base64.urlsafe_b64encode(json.dumps(edited, separators=(",", ":")).encode()).decode().rstrip("=")
    r, _, code = L.mcp(h + "." + p2 + "." + s, "lookup_invoice", {"invoice_id": "INV-000003"})
    L.check("B13", "403 TOKEN_BAD_SIGNATURE", f"{r.status_code} {code}",
            r.status_code == 403 and code == "TOKEN_BAD_SIGNATURE", r)
    n = count_audit(L, lambda ev: ev.get("type") == "decision" and ev.get("decision") == "allowed"
                    and ev.get("tool") == "approve_payment" and ev.get("jti") == claims["jti"])
    L.check("B14", "exactly 1 allowed approve_payment in gateway audit", f"{n}", n == 1, jti=claims["jti"])


def count_audit(L: Live, pred, service: str = "cene-gateway", since: float | None = None) -> int:
    from google.cloud import storage

    since = since if since is not None else L.extra.get("run_start", 0)
    n = 0
    for blob in storage.Client(project=L.project).list_blobs(AUDIT_BUCKET, prefix=f"audit/{service}/"):
        if blob.time_created.timestamp() < since - 5:
            continue
        rec = json.loads(blob.download_as_bytes())
        if rec.get("kind") == "event" and pred(rec["event"]):
            n += 1
    return n


# ====================================================================== phase C
def phase_c(L: Live):
    print("     applying stale fault to ap_invoices ...", flush=True)
    bq_load(L.project, "uc01", {"type": "stale", "product": "ap_invoices", "age_seconds": 5400})
    q = L.quality("ap_invoices")
    fresh = q["vectors"].get("fresh", 1)
    L.check("C1", "tier 2; fresh < 0.9; reason mentions freshness",
            f"tier {q['tier']}; fresh {fresh}; reasons {q['reasons']}",
            q["tier"] == 2 and fresh < 0.9 and any("fresh" in x for x in q["reasons"]),
            vectors=q["vectors"], score=q["score"])
    tok, r = L.token()
    T = tok["token"]
    L.extra["C_jti"] = jwt_payload(T)["jti"]
    L.check("C2", "tier 2; approve_payment escrow", f"tier {tok['tier']}; approve_payment {tok['tools'].get('approve_payment')}",
            tok["tier"] == 2 and tok["tools"].get("approve_payment") == "escrow", r, token=short(T))
    pool = tok.get("bindings", {}).get("approve_payment", [])
    if not pool:
        L.check("C3", "200 escrow with item id", "no bindings in token", False)
        return
    # Phase B pays the first binding (once-per-invoice rule), so phase C takes bindings from the end.
    e = pool[-1]
    r, body, code = L.mcp(T, "approve_payment", binding_args(e), proof=e)
    item = ((body.get("result") or {}).get("escrow_id")) if r.ok else None
    L.check("C3", "200 decision escrow, escrow id",
            f"{r.status_code} {r.headers.get('x-cene-decision')} id={item} {code or ''}".strip(),
            r.status_code == 200 and r.headers.get("x-cene-decision") == "escrow" and bool(item), r,
            invoice=e["binding"]["k"]["invoice_id"])
    if not item:
        return
    r = L.get(f"{L.gateway}/v1/escrow")
    pend = {i["id"]: i for i in r.json().get("pending", [])} if r.ok else {}
    L.check("C4", "item listed as pending", f"{r.status_code}; listed={item in pend}",
            item in pend and pend[item]["status"] == "pending", r)
    r, out = L.escrow(item)
    L.check("C5", "approver 1 -> status pending", f"{r.status_code} {out}", r.status_code == 200 and out.get("status") == "pending", r)
    r, out = L.escrow(item)
    L.check("C6", "409 DUPLICATE_APPROVER", f"{r.status_code} {out.get('error')}",
            r.status_code == 409 and out.get("error") == "DUPLICATE_APPROVER", r)
    try:
        agent = L.agent_id_token()
    except subprocess.CalledProcessError as ex:
        L.check("C7", "approver 2 -> executed", f"impersonation refused: {ex.stderr.strip()[-200:]}", False)
        agent = None
    if agent:
        r, out = L.escrow(item, bearer=agent)
        L.check("C7", "approver 2 -> status executed", f"{r.status_code} {out.get('status')} {out.get('code') or out.get('error') or ''}".strip(),
                r.status_code == 200 and out.get("status") == "executed", r, approver=L.agent_sa)
        r, out = L.escrow(item, bearer=agent)
        L.check("C8", "409 ESCROW_CLOSED", f"{r.status_code} {out.get('error')}",
                r.status_code == 409 and out.get("error") == "ESCROW_CLOSED", r)
    else:
        L.check("C8", "409 ESCROW_CLOSED", "skipped: no second approver", None)
    r, out = L.escrow(item, auth=False)
    L.check("C9", "APPROVER_UNKNOWN or 401/403", f"{r.status_code} {out.get('error') or 'Cloud Run'}",
            r.status_code in (401, 403) or out.get("error") == "APPROVER_UNKNOWN", r)
    # Known design gap: the token requester (the user) approves an action requested under agent_id "ap-agent".
    r, body, _ = L.mcp(T, "approve_payment", binding_args(pool[-2]), proof=pool[-2])
    item2 = (body.get("result") or {}).get("escrow_id") if r.ok else None
    r, out = L.escrow(item2) if item2 else (r, {"error": "no escrow item"})
    L.check("C10", "requester's own approval refused (SELF_APPROVAL)",
            f"{r.status_code} {out.get('status') or out.get('error')}",
            r.status_code == 409 and out.get("error") == "SELF_APPROVAL", r, requester=L.user, agent_id="ap-agent",
            escrow_id=item2)
    L.extra["C_end"] = time.time()
    print("     reloading uc01 healthy ...", flush=True)
    bq_load(L.project, "uc01")
    q = L.quality("ap_invoices")
    L.check("C11", "tier 1 after healthy reload", f"tier {q['tier']}", q["tier"] == 1, vectors=q["vectors"])


# ====================================================================== phase C, three identities
def phase_c3(L: Live, approver_sa: str):
    """Two-person escrow under the requester check: cene-agent requests the token and submits the
    payment; the user and cene-approver approve; the requester's own approval is refused."""
    print("     reloading uc01 healthy, then applying the stale fault ...", flush=True)
    bq_load(L.project, "uc01")
    bq_load(L.project, "uc01", {"type": "stale", "product": "ap_invoices", "age_seconds": 5400})
    q = L.quality("ap_invoices")
    fresh = q["vectors"].get("fresh", 1)
    L.check("C1", "tier 2; fresh < 0.9", f"tier {q['tier']}; fresh {fresh}; reasons {q['reasons']}",
            q["tier"] == 2 and fresh < 0.9, vectors=q["vectors"], score=q["score"])
    agent_ctl, agent_gw = L.agent_id_token(L.control), L.agent_id_token(L.gateway)
    approver_gw = L.sa_id_token(approver_sa, L.gateway)
    tok, r = L.token(bearer=agent_ctl)
    T = tok["token"]
    claims = jwt_payload(T)
    L.extra["C3_jti"] = claims["jti"]
    L.check("C2", f"requested by cene-agent: tier 2; approve_payment escrow; req_by = {L.agent_sa}",
            f"tier {tok['tier']}; approve_payment {tok['tools'].get('approve_payment')}; req_by {claims.get('req_by')}",
            tok["tier"] == 2 and tok["tools"].get("approve_payment") == "escrow" and claims.get("req_by") == L.agent_sa,
            r, token=short(T), jti=claims["jti"])
    pool = tok.get("bindings", {}).get("approve_payment", [])
    e = pool[-1]
    r, body, code = L.mcp(T, "approve_payment", binding_args(e), proof=e, bearer=agent_gw)
    item = (body.get("result") or {}).get("escrow_id") if r.ok else None
    L.check("C3", "decision escrow, escrow id", f"{r.status_code} {r.headers.get('x-cene-decision')} id={item} {code or ''}".strip(),
            r.status_code == 200 and r.headers.get("x-cene-decision") == "escrow" and bool(item), r,
            invoice=e["binding"]["k"]["invoice_id"], caller=L.agent_sa)
    if not item:
        return
    L.extra["C3_escrow_id"] = item
    r = L.get(f"{L.gateway}/v1/escrow")
    pend = {i["id"]: i for i in r.json().get("pending", [])} if r.ok else {}
    L.check("C4", "item listed as pending", f"{r.status_code}; listed={item in pend}",
            item in pend and pend[item]["status"] == "pending", r)

    def approve(cid, expected, bearer, who, ok_fn, label):
        r, out = L.escrow(item, bearer=bearer)
        L.check(cid, expected, f"{r.status_code} {out.get('status') or out.get('error')}", ok_fn(r, out), r, approver=who)

    approve("C5", "approve as user: status pending", None, L.user,
            lambda r, o: r.status_code == 200 and o.get("status") == "pending", "user")
    approve("C6", "approve as user again: 409 DUPLICATE_APPROVER", None, L.user,
            lambda r, o: r.status_code == 409 and o.get("error") == "DUPLICATE_APPROVER", "user")
    approve("C6b", "approve as cene-agent (requester): 409 SELF_APPROVAL", agent_gw, L.agent_sa,
            lambda r, o: r.status_code == 409 and o.get("error") == "SELF_APPROVAL", "agent")
    approve("C7", "approve as cene-approver: status executed", approver_gw, approver_sa,
            lambda r, o: r.status_code == 200 and o.get("status") == "executed", "approver")
    approve("C8", "approve again: 409 ESCROW_CLOSED", approver_gw, approver_sa,
            lambda r, o: r.status_code == 409 and o.get("error") == "ESCROW_CLOSED", "approver")
    r, out = L.escrow(item, auth=False)
    L.check("C9", "no ID token: 401/403", f"{r.status_code} {out.get('error') or 'Cloud Run'}",
            r.status_code in (401, 403), r)
    L.extra["C3_end"] = time.time()
    found = []
    for _ in range(3):  # audit objects are written synchronously; retry briefly in case listing lags
        found = []
        count_audit(L, lambda ev: ev.get("type") == "escrow_executed" and ev.get("escrow_id") == item
                    and (found.append(ev) or True))
        if found:
            break
        time.sleep(3)
    approvers = found[0]["approvers"] if found else None
    want = [L.user, approver_sa]
    L.check("E4a", f"escrow_executed lists exactly {want}", f"{approvers} ({len(found)} event(s))",
            len(found) == 1 and approvers is not None and sorted(approvers) == sorted(want), escrow_id=item)
    print("     reloading uc01 healthy ...", flush=True)
    bq_load(L.project, "uc01")
    q = L.quality("ap_invoices")
    L.check("C11", "reload healthy: tier 1", f"tier {q['tier']}", q["tier"] == 1, vectors=q["vectors"])


# ====================================================================== fail closed (temporary gateway)
def phase_fc(L: Live, fc_url: str, calls: int = 10, cold: bool = False):
    """A gateway whose Redis address answers nothing must deny every call with STATE_UNAVAILABLE, quickly,
    without reaching the tools service, and audit each denial. Tokens come from the normal control plane."""
    import datetime as dt

    fc = fc_url.rstrip("/")
    tok, _ = L.token()
    T = tok["token"]
    pool = tok.get("bindings", {}).get("approve_payment", [])
    service = fc.split("//")[1].split("-")[0:3]  # cene-gateway-failclosed
    service = "-".join(service)
    t_start = dt.datetime.now(dt.timezone.utc)

    def call(tool, args, proof=None):
        req = {"jsonrpc": "2.0", "id": uuid.uuid4().hex, "method": "tools/call",
               "params": {"name": tool, "arguments": args}}
        if proof is not None:
            req["params"]["_meta"] = {"binding_proof": proof}
        r = L.post(f"{fc}/mcp", req, headers={"X-Context-Token": T})
        try:
            code = (r.json().get("error") or {}).get("message")
        except ValueError:
            code = None
        return r, code

    if cold:  # the first request this revision serves: measured, no pass mark (like F3)
        r, code = call("lookup_invoice", {"invoice_id": "INV-000001"})
        L.check("FC1-cold", "first request to a new revision: time to deny (measured, no pass mark)",
                f"{r.status_code} {code} in {r.elapsed_ms:.0f} ms", "INFO", r)
    h = L.get(f"{fc}/health")
    r, code = call("lookup_invoice", {"invoice_id": "INV-000002"})
    L.check("FC1", "after /health: lookup_invoice -> 403 STATE_UNAVAILABLE in < 3 s",
            f"/health {h.status_code}; {r.status_code} {code} in {r.elapsed_ms:.0f} ms",
            h.status_code == 200 and r.status_code == 403 and code == "STATE_UNAVAILABLE" and r.elapsed_ms < 3000, r)
    e = pool[-1]
    r, code = call("approve_payment", binding_args(e), proof=e)
    fc2 = L.check("FC2", "approve_payment (valid binding) -> 403 STATE_UNAVAILABLE; no call reaches cene-tools",
                  f"{r.status_code} {code} in {r.elapsed_ms:.0f} ms", r.status_code == 403 and code == "STATE_UNAVAILABLE", r,
                  invoice=e["binding"]["k"]["invoice_id"])
    xs, codes = [], []
    for i in range(calls):
        r, code = call("lookup_invoice", {"invoice_id": f"INV-{i + 1:06d}"})
        xs.append(r.elapsed_ms)
        codes.append((r.status_code, code))
    all_denied = all(s == 403 and c == "STATE_UNAVAILABLE" for s, c in codes)
    L.extra["fc3_ms"] = [round(x, 1) for x in xs]
    L.check("FC3", f"{calls} calls in a row all denied; median and max time to deny",
            f"{sum(s == 403 and c == 'STATE_UNAVAILABLE' for s, c in codes)}/{calls} denied; "
            f"median {statistics.median(xs):.0f} ms, max {max(xs):.0f} ms", all_denied,
            median_ms=round(statistics.median(xs), 1), max_ms=round(max(xs), 1))
    t_end = dt.datetime.now(dt.timezone.utc)
    # FC2 (continued): cene-tools must have received no request during the fail-closed calls
    print("     waiting 45 s for Cloud Logging, then checking cene-tools ...", flush=True)
    time.sleep(45)
    flt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="cene-tools" AND '
           f'httpRequest.requestUrl:"/v1/tools/" AND timestamp>="{t_start.isoformat()}" AND '
           f'timestamp<="{(t_end + dt.timedelta(seconds=5)).isoformat()}"')
    hits = json.loads(gcloud("logging", "read", flt, "--project", L.project, "--format=json", "--limit=50") or "[]")
    fc2["evidence"]["tools_requests_in_window"] = len(hits)
    fc2["actual"] += f"; cene-tools requests in window: {len(hits)}"
    if hits:
        fc2["status"] = "FAIL"
    print(f"FC2   | (tools log check) | {len(hits)} request(s) | {'PASS' if not hits else 'FAIL'}", flush=True)
    # FC4: every denial audited as STATE_UNAVAILABLE by the temporary gateway
    n = count_audit(L, lambda ev: ev.get("type") == "decision" and ev.get("decision") == "denied"
                    and ev.get("code") == "STATE_UNAVAILABLE", service=service, since=t_start.timestamp())
    other = count_audit(L, lambda ev: ev.get("type") == "decision" and ev.get("code") != "STATE_UNAVAILABLE",
                        service=service, since=t_start.timestamp())
    want = calls + 2 + (1 if cold else 0)
    L.check("FC4", f"{want} decision events, all denied STATE_UNAVAILABLE", f"{n} STATE_UNAVAILABLE, {other} other",
            n == want and other == 0, audit_prefix=f"audit/{service}/")


# ====================================================================== phase D
def phase_d(L: Live, only: list[str] | None = None):
    def snap(product):
        q = L.quality(product)
        return {"tier": q["tier"], "score": q["score"], "vectors": q["vectors"], "reasons": q["reasons"],
                "age_s": None if q["detail"].get("age_seconds") is None else round(q["detail"]["age_seconds"], 1)}

    rows = []
    for n, (uc, prod, fault, exp_fault, wait) in enumerate(PHASE_D, 1):
        if only and uc not in only:
            continue
        cid = f"D{n:02d}"
        t0 = time.time()
        bq_load(L.project, uc)
        load_s = round(time.time() - t0, 1)
        healthy = snap(prod)
        L.check(f"{cid}h", f"{uc} {prod} healthy tier 1", f"tier {healthy['tier']} (age {healthy['age_s']}s, "
                f"load took {load_s}s) {healthy['reasons']}", healthy["tier"] == 1, **healthy)
        if fault:
            bq_load(L.project, uc, fault)
        elif wait:
            time.sleep(wait)
        after = snap(prod)
        label = fault["type"] if fault else (f"wait {wait}s past SLA" if wait else "no fault")
        L.check(f"{cid}f", f"{uc} {label} -> tier {exp_fault}", f"tier {after['tier']} (age {after['age_s']}s) "
                f"{after['reasons']}", after["tier"] == exp_fault, fault=fault, **after)
        if fault:
            bq_load(L.project, uc)
        rows.append({"id": cid, "uc": uc, "product": prod, "healthy": healthy, "after": after, "expected": exp_fault})
    L.extra["phase_d"] = rows


# ====================================================================== phase F
def phase_f(L: Live, cold: bool = False):
    tok, r = L.token()
    T = tok["token"]
    lat = {}
    if cold:
        r, _, code = L.mcp(T, "lookup_invoice", {"invoice_id": "INV-000001"})
        lat["F3_cold_ms"] = round(r.elapsed_ms, 1)
        L.check("F3", "cold-start lookup_invoice (gateway + tools from zero)", f"{r.elapsed_ms:.0f} ms, {r.status_code}",
                r.status_code == 200, r)
    for i in range(5):
        L.mcp(T, "lookup_invoice", {"invoice_id": f"INV-{i + 1:06d}"})
    xs, fails = [], 0
    for i in range(100):
        r, _, _ = L.mcp(T, "lookup_invoice", {"invoice_id": f"INV-{(i % 200) + 1:06d}"})
        fails += r.status_code != 200
        xs.append(r.elapsed_ms)
    lat["F1"] = {"n": 100, "errors": fails, "p50": pct(xs, 50), "p95": pct(xs, 95), "p99": pct(xs, 99),
                 "mean": round(statistics.mean(xs), 1)}
    L.check("F1", "100 warm lookups: p50/p95/p99 ms", json.dumps(lat["F1"]), fails == 0)
    ys = []
    for _ in range(20):
        _, r = L.token()
        ys.append(r.elapsed_ms)
    lat["F2"] = {"n": 20, "p50": pct(ys, 50), "p95": pct(ys, 95), "mean": round(statistics.mean(ys), 1)}
    L.check("F2", "20 token issues: p50/p95 ms", json.dumps(lat["F2"]), True)
    lat["F4"] = bq_lookup_timing(L.project)
    L.check("F4", "BigQuery lookup alone: client and job ms", json.dumps(lat["F4"]), True)
    L.extra["latency"] = lat


def bq_lookup_timing(project: str, n: int = 20) -> dict:
    from google.cloud import bigquery

    bq = bigquery.Client(project=project)
    cfg = bigquery.QueryJobConfig(use_query_cache=False, query_parameters=[
        bigquery.ScalarQueryParameter("id", "STRING", "INV-000001")])
    client_ms, job_ms = [], []
    for _ in range(n):
        t0 = time.perf_counter()
        job = bq.query(f"SELECT * FROM `{project}.cene.ap_invoices` WHERE invoice_id=@id LIMIT 1", job_config=cfg)
        list(job.result())
        client_ms.append((time.perf_counter() - t0) * 1000)
        job_ms.append((job.ended - job.started).total_seconds() * 1000)
    return {"n": n, "client_p50": pct(client_ms, 50), "client_p95": pct(client_ms, 95),
            "job_p50": pct(job_ms, 50), "job_p95": pct(job_ms, 95)}


# ====================================================================== main
def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--region", default=REGION)
    ap.add_argument("--control-url")
    ap.add_argument("--gateway-url")
    ap.add_argument("--tools-url")
    ap.add_argument("--id-token", help="default: gcloud auth print-identity-token, refreshed every 45 min")
    ap.add_argument("--agent-sa", help="agent identity: token requester, and second approver in phase C")
    ap.add_argument("--approver-sa", help="phase C3: the second approver (default cene-approver@PROJECT)")
    ap.add_argument("--failclosed-url", help="phase FC: URL of a temporary gateway whose Redis is unreachable")
    ap.add_argument("--phases", default="A,B,C,D,F")
    ap.add_argument("--uc", nargs="*", help="phase D: only these use cases")
    ap.add_argument("--cold", action="store_true", help="phase F: measure the first call as a cold start (F3); phase FC: the first request as FC1-cold")
    ap.add_argument("--no-load", action="store_true", help="phase B: skip the uc01 reload")
    ap.add_argument("--out", default="gcp_e2e_results.json")
    a = ap.parse_args(argv)
    L = Live(a)
    L.extra["run_start"] = time.time()
    phases = [p.strip().upper() for p in a.phases.split(",")]
    print(f"control {L.control}\ngateway {L.gateway}\nuser {L.user}\n")
    for p in phases:
        print(f"--- phase {p}", flush=True)
        try:
            {"A": lambda: phase_a(L), "B": lambda: phase_b(L, not a.no_load), "C": lambda: phase_c(L),
             "C3": lambda: phase_c3(L, a.approver_sa or f"cene-approver@{a.project}.iam.gserviceaccount.com"),
             "FC": lambda: phase_fc(L, a.failclosed_url, cold=a.cold),
             "D": lambda: phase_d(L, a.uc), "F": lambda: phase_f(L, a.cold)}[p]()
        except Exception as e:  # record and keep going: a crash is itself a finding
            L.check(f"{p}!", "phase completes", f"{type(e).__name__}: {str(e)[:300]}", False)
    L.extra["run_end"] = time.time()
    summary = {}
    for c in L.checks:
        s = summary.setdefault(c["id"][0], {"PASS": 0, "FAIL": 0, "SKIP": 0, "INFO": 0})
        s[c["status"]] += 1
    out = {"control": L.control, "gateway": L.gateway, "user": L.user, "summary": summary, "checks": L.checks,
           "extra": L.extra}
    Path(a.out).write_text(json.dumps(out, indent=2, default=str))
    print("\nsummary", json.dumps(summary))
    return 0 if all(s["FAIL"] == 0 for s in summary.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
