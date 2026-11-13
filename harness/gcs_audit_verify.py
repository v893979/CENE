"""Verify the audit hash chains stored in Cloud Storage (docs/08_GCP_Test_Plan.md phase E).

    python -m harness.gcs_audit_verify                       # E1 + E2 + E3 (inferred from IAM)
    python -m harness.gcs_audit_verify --window results.json \
        --expect '{"decision": 14, "escrow_submitted": 2}'   # E4: event counts in a test window

Layout: gs://<bucket>/audit/<service>/<chain id>/<seq>-<kind>.json (one chain per service instance).
Objects directly under audit/ were written before v3 and are reported as legacy.
Read-only: nothing in the bucket is modified or deleted; E2 tampers with a local copy only.
"""
from __future__ import annotations

import argparse
import copy
import json
import os
import shutil
import subprocess
import sys
from collections import Counter, defaultdict

from cene.audit import verify_records

PROJECT = os.environ.get("CENE_GCP_PROJECT", "<gcp-project-id>")
REGION = "australia-southeast2"


def gcloud(*args: str) -> str:
    exe = shutil.which("gcloud") or shutil.which("gcloud.cmd") or "gcloud"
    return subprocess.run([exe, *args], check=True, capture_output=True, text=True).stdout


def order(records: list[dict]) -> list[dict]:
    # a checkpoint carries the seq of the event it closes, so the event must come first
    return sorted(records, key=lambda r: (r["seq"], r["kind"] != "event"))


def load_chains(project: str, bucket: str):
    from google.cloud import storage

    chains, legacy = defaultdict(list), []
    for blob in storage.Client(project=project).list_blobs(bucket, prefix="audit/"):
        parts = blob.name.split("/")
        rec = json.loads(blob.download_as_bytes())
        if len(parts) == 4:
            chains[f"{parts[1]}/{parts[2]}"].append(rec)
        else:
            legacy.append((blob.name, rec))
    return {k: order(v) for k, v in chains.items()}, legacy


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--project", default=PROJECT)
    ap.add_argument("--bucket", default=f"{PROJECT}-cene-audit")
    ap.add_argument("--window", help="results JSON from harness.gcp_e2e; E4 counts events between its B and C checks")
    ap.add_argument("--expect", help='E4 expected counts by event type, e.g. {"decision": 14}')
    ap.add_argument("--out")
    a = ap.parse_args(argv)
    pem = gcloud("kms", "keys", "versions", "get-public-key", "1", "--key", "audit", "--keyring", "cene",
                 "--location", REGION, "--project", a.project).encode()
    chains, legacy = load_chains(a.project, a.bucket)
    checks = []

    def check(cid, expected, actual, ok, **ev):
        status = "SKIP" if ok is None else ("PASS" if ok else "FAIL")
        checks.append({"id": cid, "expected": expected, "actual": actual, "status": status, "evidence": ev})
        print(f"{cid:5s} | {expected} | {actual} | {status}", flush=True)

    per_chain = {}
    for name, recs in sorted(chains.items()):
        ok, msg = verify_records(recs, pem)
        per_chain[name] = {"valid": ok, "msg": msg, "events": sum(r["kind"] == "event" for r in recs),
                           "checkpoints": sum(r["kind"] == "checkpoint" for r in recs)}
    bad = {k: v for k, v in per_chain.items() if not v["valid"]}
    check("E1", "every chain folder valid; pre-v3 objects listed as legacy",
          f"{len(per_chain) - len(bad)}/{len(per_chain)} chains valid; {len(legacy)} legacy object(s)",
          not bad and bool(per_chain), chains=per_chain, legacy=[n for n, _ in legacy])

    target = max(chains, key=lambda k: len(chains[k]))
    copy_recs = copy.deepcopy(chains[target])
    events = [r for r in copy_recs if r["kind"] == "event"]
    victim = events[len(events) // 2]
    field = next(k for k in victim["event"] if k not in ("ts",))
    victim["event"][field] = "tampered"
    ok, msg = verify_records(copy_recs, pem)
    check("E2", f"valid: false naming seq {victim['seq']}", f"valid: {ok} ({msg})",
          ok is False and str(victim["seq"]) in msg, chain=target, field=field)

    role = json.loads(gcloud("iam", "roles", "describe", "roles/storage.objectCreator", "--format=json"))
    perms = role.get("includedPermissions", [])
    pol = json.loads(gcloud("storage", "buckets", "describe", f"gs://{a.bucket}", "--format=json",
                            "--project", a.project))
    retention = pol.get("retention_policy") or pol.get("retentionPolicy") or {}
    bucket_iam = json.loads(gcloud("storage", "buckets", "get-iam-policy", f"gs://{a.bucket}", "--format=json"))
    gw = f"serviceAccount:cene-gateway@{a.project}.iam.gserviceaccount.com"
    gw_roles = sorted(b["role"] for b in bucket_iam.get("bindings", []) if gw in b.get("members", []))
    can_overwrite = "storage.objects.delete" in perms or any(r != "roles/storage.objectCreator" for r in gw_roles)
    check("E3", "gateway SA cannot overwrite an object (403, create-only)",
          f"inferred from IAM: gateway bucket roles {gw_roles}; objectCreator has delete={'storage.objects.delete' in perms}; "
          f"retention {retention}", not can_overwrite, method="inferred (impersonation of the gateway SA not available)",
          retention=retention)

    if a.window and a.expect:
        res = json.load(open(a.window))
        import datetime as dt

        ts = [dt.datetime.strptime(c["utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc).timestamp()
              for c in res["checks"] if c["id"][0] in "BC" and c["id"] not in ("B1", "C1", "C11", "B14")]
        lo, hi = min(ts) - 30, max(ts) + 2
        counts = Counter()
        for name, recs in chains.items():
            for r in recs:
                if r["kind"] == "event" and lo <= r["event"]["ts"] <= hi:
                    counts[r["event"]["type"]] += 1
        exp = json.loads(a.expect)
        diff = {k: (counts.get(k, 0), v) for k, v in exp.items() if counts.get(k, 0) != v}
        check("E4", f"event counts in the B/C window = {exp}", f"{dict(counts)}", not diff, window=[lo, hi], diff=diff)

    if a.out:
        with open(a.out, "w") as f:
            json.dump({"checks": checks}, f, indent=2, default=str)
    return 0 if all(c["status"] != "FAIL" for c in checks) else 1


if __name__ == "__main__":
    sys.exit(main())
