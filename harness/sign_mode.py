"""Sign an operating-mode statement with the oracle key and submit it to the control plane.

The person running this needs roles/cloudkms.signerVerifier on the *oracle* key only. Nobody else,
including the control plane's own service account, can sign mode statements.

    python -m harness.sign_mode --product contractor_invoices --mode stress --hours 12 \
        --reason "Cyclone declared" --oracle-key projects/P/locations/L/keyRings/cene/cryptoKeys/oracle/cryptoKeyVersions/1 \
        --control-url https://<cloud-run-url>
"""
from __future__ import annotations

import argparse
import time

import requests

from cene.modes import MODE_AUD, MODE_TYP
from cene.signer import KmsSigner
from cene.tokens import issue


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--product", required=True)
    ap.add_argument("--mode", required=True)
    ap.add_argument("--hours", type=float, default=12)
    ap.add_argument("--reason", required=True)
    ap.add_argument("--oracle-key", required=True)
    ap.add_argument("--control-url", required=True)
    ap.add_argument("--id-token", help="identity token for Cloud Run (gcloud auth print-identity-token)")
    a = ap.parse_args(argv)
    now = time.time()
    stmt = issue({"aud": MODE_AUD, "product": a.product, "mode": a.mode, "iat": now, "nbf": now,
                  "exp": now + a.hours * 3600, "reason": a.reason}, KmsSigner(a.oracle_key), typ=MODE_TYP)
    headers = {"Authorization": f"Bearer {a.id_token}"} if a.id_token else {}
    r = requests.post(a.control_url.rstrip("/") + "/v1/modes", json={"statement": stmt}, headers=headers, timeout=30)
    print(r.status_code, r.json())


if __name__ == "__main__":
    main()
