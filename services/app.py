"""HTTP layer (Starlette, the framework FastAPI is built on). One image, three roles:

  CENE_SERVICE=control   control plane: quality reports, tokens, delegation, signed modes
  CENE_SERVICE=gateway   enforcement gateway (MCP JSON-RPC) + two-person escrow
  CENE_SERVICE=tools     mock tool servers (payments, refunds, dispatch, ...)
  CENE_SERVICE=all       everything in one process (laptop demo)

Run:  uvicorn services.app:app --port 8080
Local demo data: set CENE_SEED_SCENARIO=uc01 to load that scenario's data products at start-up.
"""
from __future__ import annotations

import contextlib
import os

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from cene.escrow import EscrowError
from cene.platform import Settings, build_platform, build_tool_service
from cene.tokens import TokenError

ROLE = os.environ.get("CENE_SERVICE", "all")
P = None


def platform():
    global P
    if P is None:
        settings = Settings.from_env()
        scenario = os.environ.get("CENE_SEED_SCENARIO")
        if settings.mode == "local" and scenario:
            from harness.runner import ScenarioRun, load_scenarios

            run = ScenarioRun(load_scenarios([scenario])[0])  # builds and seeds a local platform
            P = run.p
        elif ROLE == "tools":
            P = build_tool_service(settings)  # least privilege: no signing or verification keys
        else:
            P = build_platform(settings)
        tools_url = os.environ.get("CENE_TOOLS_URL")
        if tools_url and ROLE == "gateway":
            P.gateway.tools = HttpToolClient(tools_url)
    return P


class HttpToolClient:
    """Gateway -> tools service over HTTP (Cloud Run service-to-service, identity token auth)."""

    def __init__(self, base_url: str, timeout: float | None = None):
        self.base = base_url.rstrip("/")
        # Long enough to cover a tools cold start (container start + first BigQuery query took ~11 s).
        # No retry on timeout: write tools such as approve_payment are not idempotent.
        self.timeout = timeout if timeout is not None else float(os.environ.get("CENE_TOOLS_TIMEOUT_S", "30"))

    def _headers(self):
        try:
            import google.auth.transport.requests
            import google.oauth2.id_token

            tok = google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), self.base)
            return {"Authorization": f"Bearer {tok}"}
        except Exception:
            return {}

    def call(self, tool, args, claims=None):
        import requests

        r = requests.post(f"{self.base}/v1/tools/{tool}", json={"arguments": args, "agent": (claims or {}).get("sub")},
                          headers=self._headers(), timeout=(10, self.timeout))
        r.raise_for_status()
        return r.json()


def _bearer(req: Request) -> str | None:
    """The Context Token travels in X-Context-Token. On Cloud Run the Authorization header carries the
    caller's Google identity token (checked by Cloud Run IAM), so it is used for the Context Token
    only in local mode."""
    tok = req.headers.get("x-context-token")
    if tok:
        return tok
    h = req.headers.get("authorization", "")
    if os.environ.get("CENE_MODE", "local") == "local" and h.lower().startswith("bearer "):
        return h[7:]
    return None


def _caller_identity(req: Request) -> str | None:
    """Email of the authenticated caller. On Cloud Run, IAM has already verified the identity token in
    the Authorization header before the request reaches us, so we only read its email claim.
    In local mode the approver may be named in the X-Approver header (testing only)."""
    import base64
    import json

    h = req.headers.get("authorization", "")
    if os.environ.get("CENE_MODE", "local") == "gcp":
        try:
            payload = h[7:].split(".")[1]
            return json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))["email"]
        except Exception:
            return None
    return req.headers.get("x-approver")


# ------------------------------------------------------------------------- control plane
async def health(req):
    return JSONResponse({"ok": True, "role": ROLE})


async def quality(req):
    p = platform()
    return JSONResponse(p.quality.evaluate(p.contracts.get(req.path_params["product"])).to_dict())


async def tokens(req):
    body = await req.json()
    p = platform()
    try:
        requester = _caller_identity(req) if os.environ.get("CENE_MODE", "local") == "gcp" else None
        out = p.control.issue(body["agent_id"], body.get("task", ""), body["products"], body.get("tools"),
                              requested_by=requester)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return JSONResponse({"token": out["token"], "tier": out["claims"]["tier"], "expires": out["claims"]["exp"],
                         "tools": {k: v["mode"] for k, v in out["claims"]["tools"].items()},
                         "bindings": out["bindings"],
                         "reports": {k: {x: v[x] for x in ("tier", "score", "reasons", "vectors")}
                                     for k, v in out["reports"].items()}})


async def delegate(req):
    body = await req.json()
    p = platform()
    try:
        out = p.control.delegate(body["parent_token"], p.keyring, body["agent_id"], body["tools"])
    except TokenError as e:
        return JSONResponse({"error": e.code}, status_code=403)
    return JSONResponse({"token": out["token"], "expires": out["claims"]["exp"], "bindings": out["bindings"]})


async def modes(req):
    body = await req.json()
    try:
        return JSONResponse(platform().modes.switch(body["statement"]))
    except TokenError as e:
        return JSONResponse({"error": e.code}, status_code=403)


async def keys(req):
    p = platform()
    return JSONResponse({"keys": [{"kid": k, "alg": "ES256", "pem": v.decode()} for k, v in p.keyring.keys.items()]})


# ------------------------------------------------------------------------- gateway + escrow
async def mcp(req):
    body = await req.body()
    from cene.gateway import parse_json

    resp, dec = platform().gateway.handle(_bearer(req), parse_json(body))
    status = {"denied": 403, "error": 502}.get(dec["decision"], 200)
    headers = {"Connection": "close", "X-CENE-Decision": dec["decision"]} if dec["close"] else \
        {"X-CENE-Decision": dec["decision"]}
    return JSONResponse(resp, status_code=status, headers=headers)


async def escrow_list(req):
    return JSONResponse({"pending": platform().escrow.list_pending()})


async def escrow_act(req):
    body = await req.json()
    p = platform()
    approver = _caller_identity(req)
    if not approver:
        return JSONResponse({"error": "APPROVER_UNKNOWN"}, status_code=401)
    try:
        if req.path_params["action"] == "approve":
            out = p.escrow.approve(req.path_params["item"], approver)
        else:
            out = p.escrow.reject(req.path_params["item"], approver, body.get("reason", ""))
    except EscrowError as e:
        return JSONResponse({"error": e.code}, status_code=409)
    return JSONResponse(out)


# ------------------------------------------------------------------------- tools
async def tool_call(req):
    body = await req.json()
    out = platform().tools.call(req.path_params["tool"], body.get("arguments", {}), {"sub": body.get("agent")})
    return JSONResponse(out)


async def executed(req):
    return JSONResponse({"executed": platform().tools.executed})


ROUTES = {
    "control": [Route("/v1/quality/{product}", quality), Route("/v1/tokens", tokens, methods=["POST"]),
                Route("/v1/tokens/delegate", delegate, methods=["POST"]), Route("/v1/modes", modes, methods=["POST"]),
                Route("/v1/keys", keys)],
    "gateway": [Route("/mcp", mcp, methods=["POST"]), Route("/v1/escrow", escrow_list),
                Route("/v1/escrow/{item}/{action:str}", escrow_act, methods=["POST"])],
    "tools": [Route("/v1/tools/{tool}", tool_call, methods=["POST"]), Route("/v1/executed", executed)],
}
# "/health" (not "/healthz": Cloud Run reserves URL paths ending in "z")
routes = [Route("/health", health), Route("/healthz", health)]
for role, r in ROUTES.items():
    if ROLE in (role, "all"):
        routes += r


@contextlib.asynccontextmanager
async def lifespan(app):
    # Build the platform (contracts, keys, data/audit clients) for this role before serving, so Cloud Run's
    # start-up probe waits for it and the first request does not (it added about 2.3 s; GCP check FC1).
    # On Google Cloud a failed set-up (e.g. KMS unreachable) is fatal: the exception aborts the lifespan,
    # uvicorn exits non-zero, and Cloud Run never routes traffic to the instance. Locally, a failure is
    # logged and the platform is built lazily on the first request, as before.
    try:
        platform()
    except Exception as e:
        print(f"platform start-up failed: {type(e).__name__}: {e}", flush=True)
        if os.environ.get("CENE_MODE", "local") == "gcp":
            raise
    yield
    # Cloud Run sends SIGTERM before stopping an instance: sign the tail of this instance's audit chain.
    if P is not None and getattr(P, "audit", None) is not None:
        try:
            P.audit.close()
        except Exception as e:  # never block shutdown; the unsigned tail is still hash-chained
            print(f"audit checkpoint on shutdown failed: {type(e).__name__}: {e}", flush=True)


app = Starlette(routes=routes, lifespan=lifespan)
