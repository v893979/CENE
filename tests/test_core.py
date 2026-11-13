import math

import pandas as pd
import pytest

from cene.audit import AuditLog, MemorySink, verify_records
from cene.escrow import EscrowError, EscrowService
from cene.gateway import Denied, _binding_ok, _check_arg
from cene.quality import metrics
from cene.signer import LocalSigner
from cene.stores import MemoryStore
from cene.tokens import Keyring, TokenError, issue, verify
from cene.util import Clock, b64url, b64url_decode


@pytest.fixture
def signer():
    return LocalSigner("k1")


def _claims(now, **kw):
    c = {"aud": "cene-gateway", "sub": "a", "jti": "j", "iat": now, "nbf": now, "exp": now + 60}
    c.update(kw)
    return c


# ------------------------------------------------------------------ tokens
def test_token_roundtrip(signer):
    now = 1000.0
    tok = issue(_claims(now), signer)
    assert verify(tok, Keyring({"k1": signer.public_pem()}), now, "cene-gateway")["sub"] == "a"


@pytest.mark.parametrize("case,code", [
    ("expired", "TOKEN_EXPIRED"), ("audience", "TOKEN_WRONG_AUDIENCE"), ("unknown_key", "TOKEN_UNKNOWN_KEY"),
    ("tampered", "TOKEN_BAD_SIGNATURE"), ("garbage", "TOKEN_MALFORMED"), ("future", "TOKEN_NOT_YET_VALID"),
])
def test_token_rejections(signer, case, code):
    now = 1000.0
    ring = Keyring({"k1": signer.public_pem()})
    tok = issue(_claims(now), signer)
    if case == "expired":
        now += 120
    elif case == "audience":
        tok = issue(_claims(now, aud="other"), signer)
    elif case == "unknown_key":
        tok = issue(_claims(now), LocalSigner("k2"))
    elif case == "tampered":
        h, p, s = tok.split(".")
        tok = h + "." + b64url(b64url_decode(p).replace(b'"sub":"a"', b'"sub":"b"')) + "." + s
    elif case == "garbage":
        tok = "not.a.token"
    elif case == "future":
        tok = issue(_claims(now + 100), signer)
    with pytest.raises(TokenError) as e:
        verify(tok, ring, now, "cene-gateway")
    assert e.value.code == code


# ------------------------------------------------------------------ argument checks and bindings
def test_arg_checks():
    _check_arg("id", {"pattern": r"^INV-\d{6}$"}, "INV-000001")
    for spec, val in [({"pattern": r"^INV-\d{6}$"}, "INV-1"), ({"pattern": r"^INV-\d{6}$"}, 5),
                      ({"type": "number", "max": 10}, 11), ({"type": "number"}, "5"), ({"type": "number"}, True),
                      ({"enum": ["A"]}, "B"), ({"pattern": ".*"}, {"x": 1})]:
        with pytest.raises(Denied):
            _check_arg("x", spec, val)


def test_bindings_are_joint():
    b = [{"k": {"inv": "I1", "acc": "A1"}, "max": {"amt": 100.0}},
         {"k": {"inv": "I2", "acc": "A2"}, "max": {"amt": 50.0}}]
    assert _binding_ok(b, {"inv": "I1", "acc": "A1", "amt": 100})
    assert not _binding_ok(b, {"inv": "I1", "acc": "A2", "amt": 10})  # swapped account
    assert not _binding_ok(b, {"inv": "I2", "acc": "A2", "amt": 51})  # over amount


# ------------------------------------------------------------------ quality metrics
def test_freshness_curve():
    assert metrics.freshness(10, 60) == 1.0
    assert math.isclose(metrics.freshness(120, 60), 0.5)
    assert metrics.freshness(None, 60) == 0.0
    t = metrics.seconds_until_freshness_below(0, 60, 0.9)
    assert math.isclose(metrics.freshness(t, 60), 0.9, rel_tol=1e-9)


def test_completeness_and_zero_tolerance():
    df = pd.DataFrame({"a": [1, None, 3, 4], "b": ["x", "y", "", "z"], "u": ["mg", "mg", "umol", "mg"]})
    cols = {"a": {"criticality": 3}, "b": {"criticality": 1}, "missing": {"criticality": 1}}
    s, crit, rates = metrics.completeness(df, cols)
    assert rates["a"] == 0.25 and rates["b"] == 0.25 and rates["missing"] == 1.0 and crit == 0.75
    g, broken = metrics.zero_tolerance(df, [{"column": "u", "rule": "equals", "value": "mg"}])
    assert g == 0.0 and broken == ["u:equals"]


def test_flatline_detected():
    ref = pd.DataFrame({"x": range(200)}, dtype=float)
    cur = pd.DataFrame({"x": [5.0] * 100})
    s, d = metrics.drift(cur, ref, ["x"], ["x"], {"w1_iqr_threshold": 0.35, "flatline_ratio": 0.05})
    assert s <= 0.1 and d["flatlined"] == ["x"]


# ------------------------------------------------------------------ audit
def test_audit_chain(signer):
    sink, clock = MemorySink(), Clock()
    log = AuditLog(sink, signer, clock, checkpoint_every=3)
    for i in range(7):
        log.append({"type": "decision", "i": i})
    log.checkpoint()
    assert verify_records(sink.records, signer.public_pem())[0]
    sink.records[2]["event"]["i"] = 99
    assert not verify_records(sink.records, signer.public_pem())[0]


def test_audit_close_signs_the_tail(signer):
    # Cloud Run instances stop long before checkpoint_every events, so closing must sign the tail
    sink, clock = MemorySink(), Clock()
    log = AuditLog(sink, signer, clock, checkpoint_every=100)
    log.close()
    assert sink.records == []  # nothing to sign
    for i in range(3):
        log.append({"type": "decision", "i": i})
    log.close()
    assert sink.records[-1]["kind"] == "checkpoint" and sink.records[-1]["seq"] == 3
    log.close()  # already signed: no duplicate checkpoint
    assert sum(r["kind"] == "checkpoint" for r in sink.records) == 1
    assert verify_records(sink.records, signer.public_pem())[0]


# ------------------------------------------------------------------ escrow
def test_escrow_two_distinct_people(signer):
    clock = Clock()
    log = AuditLog(MemorySink(), signer, clock)
    esc = EscrowService(log, clock)
    esc.executor = lambda item: {"status": "executed"}
    item = esc.submit({"sub": "agent", "jti": "j", "escrow": {"approvers_required": 2}}, "pay", {}, "tier2")
    with pytest.raises(EscrowError):
        esc.approve(item["id"], "agent")
    assert esc.approve(item["id"], "alice")["status"] == "pending"
    with pytest.raises(EscrowError):
        esc.approve(item["id"], "alice")
    assert esc.approve(item["id"], "bob")["status"] == "executed"


def test_escrow_refuses_the_token_requester(signer):
    # On Cloud Run agent_id is free text from the request body; the authenticated requester is req_by
    clock = Clock()
    esc = EscrowService(AuditLog(MemorySink(), signer, clock), clock)
    esc.executor = lambda item: {"status": "executed"}
    claims = {"sub": "ap-agent", "req_by": "alice@example.com", "jti": "j", "escrow": {"approvers_required": 2}}
    item = esc.submit(claims, "pay", {}, "tier2")
    with pytest.raises(EscrowError) as e:
        esc.approve(item["id"], "alice@example.com")
    assert e.value.code == "SELF_APPROVAL"
    assert esc.approve(item["id"], "bob@example.com")["status"] == "pending"


def test_bq_load_commit_time_is_when_the_table_landed():
    import datetime as dt

    from harness.bq_load import commit_timestamp

    landed = dt.datetime(2026, 11, 7, 21, 0, 30, tzinfo=dt.timezone.utc)
    generated = landed.timestamp() - 20  # rows generated 20 s before the BigQuery load finished
    # healthy load: the data became readable when the load finished, not when it was generated
    assert commit_timestamp(landed, generated, generated) == landed
    # stale fault: the fault's age is kept relative to the landing time
    assert commit_timestamp(landed, generated, generated - 5400) == landed - dt.timedelta(seconds=5400)


def test_app_builds_the_platform_at_startup(monkeypatch):
    # FC1: the first request must not pay for building contracts, keys and clients (about 2.3 s on Cloud Run)
    import asyncio

    import services.app as A

    monkeypatch.setenv("CENE_SEED_SCENARIO", "uc01")
    monkeypatch.setattr(A, "P", None)

    async def run():
        async with A.lifespan(A.app):
            return A.P

    assert asyncio.run(run()) is not None


def _failing_platform():
    raise RuntimeError("KMS unreachable")


def test_startup_failure_is_fatal_on_gcp(monkeypatch):
    import asyncio

    import services.app as A

    monkeypatch.setenv("CENE_MODE", "gcp")
    monkeypatch.setattr(A, "platform", _failing_platform)

    async def run():
        async with A.lifespan(A.app):
            pass

    with pytest.raises(RuntimeError, match="KMS unreachable"):
        asyncio.run(run())


def test_startup_failure_exits_the_server_non_zero():
    # The real process: uvicorn aborts on a failed lifespan start-up, so Cloud Run never routes traffic to it
    import subprocess
    import sys

    code = ("import services.app as A, uvicorn\n"
            "def boom():\n    raise RuntimeError('KMS unreachable')\n"
            "A.platform = boom\n"
            "uvicorn.run(A.app, host='127.0.0.1', port=0, log_level='error')\n")
    env = {**__import__("os").environ, "CENE_MODE": "gcp", "CENE_SERVICE": "gateway"}
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, r.stdout + r.stderr
    assert "KMS unreachable" in r.stdout + r.stderr


def test_startup_failure_falls_back_to_lazy_locally(monkeypatch):
    import asyncio

    import services.app as A

    monkeypatch.setenv("CENE_MODE", "local")
    monkeypatch.setattr(A, "platform", _failing_platform)

    async def run():
        async with A.lifespan(A.app):
            return "served"

    assert asyncio.run(run()) == "served"


def test_bq_load_records_the_load_with_a_dml_insert():
    # A load job for the one-row load-log entry adds several seconds before freshness is visible (UC09, 5 s SLA)
    import datetime as dt

    from harness.bq_load import record_load

    class FakeJob:
        def result(self):
            return []

    class FakeBQ:
        def __init__(self):
            self.queries, self.loads = [], []

        def query(self, sql, job_config=None):
            self.queries.append((sql, {p.name: p.value for p in job_config.query_parameters}))
            return FakeJob()

        def load_table_from_dataframe(self, *a, **k):
            self.loads.append(a)
            return FakeJob()

    bq = FakeBQ()
    ts = dt.datetime(2026, 11, 8, 1, 0, 0, tzinfo=dt.timezone.utc)
    record_load(bq, "p.cene", "scada_telemetry", ts)
    assert bq.loads == []
    (sql, params), = bq.queries
    assert sql.strip().upper().startswith("INSERT INTO `P.CENE._CENE_LOAD_LOG`")
    assert params == {"product": "scada_telemetry", "ts": ts}


def test_token_records_the_requester():
    from harness.runner import ScenarioRun, load_scenarios

    p = ScenarioRun(load_scenarios(["uc01"])[0]).p
    out = p.control.issue("ap-agent", "pay", ["ap_invoices"], requested_by="alice@example.com")
    assert out["claims"]["req_by"] == "alice@example.com"
    assert "req_by" not in p.control.issue("ap-agent", "pay", ["ap_invoices"])["claims"]


# ------------------------------------------------------------------ state store
def test_memory_store_windows():
    s = MemoryStore()
    assert s.set_nx("k", "1", 10, now=0) and not s.set_nx("k", "1", 10, now=5) and s.set_nx("k", "1", 10, now=11)
    s.add_event("e", 0, 5, ttl=100)
    s.add_event("e", 50, 7, ttl=100)
    assert [v for _, v in s.events_since("e", 10)] == [7]


# ------------------------------------------------------------------ Merkle bindings and fail-closed gateway
from cene import merkle  # noqa: E402


def test_merkle_proofs():
    bs = [{"k": {"i": f"I{j}"}, "max": {"a": float(j)}} for j in range(37)]
    root, proofs = merkle.build(bs)
    assert all(merkle.verify(b, p, root) for b, p in zip(bs, proofs))
    assert not merkle.verify({"k": {"i": "I1"}, "max": {"a": 999.0}}, proofs[1], root)  # altered maximum
    assert not merkle.verify(bs[2], proofs[3], root)                                      # wrong proof


def _uc01_run():
    import copy
    from harness.runner import ScenarioRun, load_scenarios

    sc = copy.deepcopy(load_scenarios(["uc01"])[0])
    sc["steps"] = [{"do": "issue", "as": "t", "agent": "a", "products": ["ap_invoices"]}]
    run = ScenarioRun(sc)
    run.run()
    return run


def test_gateway_requires_valid_binding_proof():
    run = _uc01_run()
    entry = run.binding_sets["t"]["approve_payment"][0]
    args = {**entry["binding"]["k"], "amount": entry["binding"]["max"]["amount"]}
    call = lambda rid, meta: run.p.gateway.handle(run.tokens["t"], {  # noqa: E731
        "jsonrpc": "2.0", "id": rid, "method": "tools/call",
        "params": {"name": "approve_payment", "arguments": args, **({"_meta": meta} if meta else {})}})[1]
    assert call("a", None)["code"] == "BINDING_VIOLATION"                                    # no proof
    forged = {"binding": {**entry["binding"], "max": {"amount": 1e9}}, "proof": entry["proof"]}
    assert call("b", {"binding_proof": forged})["code"] == "BINDING_VIOLATION"               # edited row
    assert call("c", {"binding_proof": entry})["decision"] == "allowed"


def test_gateway_fails_closed_when_state_store_is_down():
    run = _uc01_run()

    class Broken:
        def __getattr__(self, name):
            def boom(*a, **k):
                raise ConnectionError("redis down")
            return boom

    run.p.gateway.state = Broken()
    dec = run.p.gateway.handle(run.tokens["t"], {"jsonrpc": "2.0", "id": "x", "method": "tools/call",
                                                 "params": {"name": "lookup_invoice",
                                                            "arguments": {"invoice_id": "INV-000001"}}})[1]
    assert dec["decision"] == "denied" and dec["code"] == "STATE_UNAVAILABLE"


def _timed_lookup(run, rid="x"):
    import time

    t0 = time.monotonic()
    dec = run.p.gateway.handle(run.tokens["t"], {"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                                                 "params": {"name": "lookup_invoice",
                                                            "arguments": {"invoice_id": "INV-000001"}}})[1]
    return dec, time.monotonic() - t0


def test_gateway_fails_closed_fast_when_redis_is_unreachable(monkeypatch):
    # Nothing answers on 10.255.255.1: without timeouts the call would hang until the OS TCP timeout
    from cene.stores import RedisStore

    monkeypatch.setenv("CENE_REDIS_TIMEOUT_S", "1.0")
    run = _uc01_run()
    run.p.gateway.state = RedisStore("redis://10.255.255.1:6379/0")
    dec, took = _timed_lookup(run)
    assert dec["decision"] == "denied" and dec["code"] == "STATE_UNAVAILABLE"
    assert took < 2.0, f"took {took:.2f}s"


def test_gateway_fails_closed_fast_when_redis_never_replies(monkeypatch):
    # A server that accepts the connection but never answers: the read timeout must apply, with no retry
    import socket
    import threading

    from cene.stores import RedisStore

    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    held = []
    stop = threading.Event()

    def accept():
        srv.settimeout(0.2)
        while not stop.is_set():
            try:
                held.append(srv.accept()[0])
            except OSError:
                pass

    threading.Thread(target=accept, daemon=True).start()
    try:
        monkeypatch.setenv("CENE_REDIS_TIMEOUT_S", "0.5")
        run = _uc01_run()
        run.p.gateway.state = RedisStore(f"redis://127.0.0.1:{srv.getsockname()[1]}/0")
        dec, took = _timed_lookup(run)
        assert dec["decision"] == "denied" and dec["code"] == "STATE_UNAVAILABLE"
        assert took < 1.4, f"took {took:.2f}s (a retry would take about twice the timeout)"
    finally:
        stop.set()
        for c in held:
            c.close()
        srv.close()


# ------------------------------------------------------------------ content screening (Model Armor response parsing)
from cene.screening import MarkerScreener, ModelArmorScreener  # noqa: E402


def test_model_armor_response_parsing():
    match = {"sanitizationResult": {"filterMatchState": "MATCH_FOUND", "invocationResult": "SUCCESS",
                                    "filterResults": {"pi_and_jailbreak": {"piAndJailbreakFilterResult": {
                                        "executionState": "EXECUTION_SUCCESS", "matchState": "MATCH_FOUND"}}}}}
    clean = {"sanitizationResult": {"filterMatchState": "NO_MATCH_FOUND", "invocationResult": "SUCCESS"}}
    assert ModelArmorScreener.parse(match) == {"flagged": True, "filters": ["piAndJailbreakFilterResult"]}
    assert ModelArmorScreener.parse(clean)["flagged"] is False
    assert MarkerScreener().screen("note [[CENE-TEST-FLAG]] x")["flagged"]


# ------------------------------------------------------------------ nullable arguments and escrow for unbound calls
def _hand_gateway(tool_spec: dict):
    """A real gateway with a hand-written token (one write tool) and a stub tool backend."""
    from cene.contracts import ContractRegistry
    from cene.platform import Settings, build_platform

    clock = Clock(start=1_800_000_000.0)
    p = build_platform(Settings(mode="local"), ContractRegistry([]), clock)

    class Stub:
        executed = []

        def call(self, tool, args, claims=None):
            self.executed.append((tool, dict(args)))
            return {"status": "done"}

    p.gateway.tools = Stub()
    now = clock.now()
    claims = {"iss": "cene-control-plane", "aud": "cene-gateway", "sub": "agent", "jti": "j1", "iat": now,
              "nbf": now, "exp": now + 600, "tier": 1, "tools": {"pay": {"kind": "write", "mode": "direct",
                                                                         "product": "x", **tool_spec}},
              "temporal": [], "taint": {"untrusted_tools": [], "on_taint": "escrow"},
              "escrow": {"approvers_required": 2}, "req_by": "user@example.com"}
    tok = issue(claims, p.token_signer)
    rid = iter(range(1000))
    call = lambda args: p.gateway.handle(tok, {"jsonrpc": "2.0", "id": next(rid), "method": "tools/call",  # noqa
                                               "params": {"name": "pay", "arguments": args}})[1]
    return p, call


def test_nullable_argument_accepts_none_only_when_declared():
    _check_arg("x", {"nullable": True, "type": "number", "max": 5}, None)
    with pytest.raises(Denied):
        _check_arg("x", {"type": "number", "max": 5}, None)
    with pytest.raises(Denied):
        _check_arg("x", {"nullable": True, "type": "number", "max": 5}, 6)   # a value is still checked
    # a binding ceiling does not apply to an unset nullable argument, but does apply to a set one
    b = [{"k": {"id": 7, "to": None}, "max": {"amt": 100.0}}]
    assert _binding_ok(b, {"id": 7, "to": None, "amt": None})
    assert not _binding_ok(b, {"id": 7, "to": None, "amt": 101})


def test_unbound_call_is_denied_by_default():
    spec = {"args": {"to": {}, "amt": {"type": "number", "max": 50}}, "bindings": [{"k": {"to": "A"}, "max": {}}]}
    p, call = _hand_gateway(spec)
    assert call({"to": "A", "amt": 5})["decision"] == "allowed"
    assert call({"to": "B", "amt": 5})["code"] == "BINDING_VIOLATION"
    assert p.gateway.tools.executed == [("pay", {"to": "A", "amt": 5})]


def test_unbound_call_goes_to_escrow_when_the_tool_says_so():
    spec = {"args": {"to": {}, "amt": {"type": "number", "max": 50}}, "bindings": [{"k": {"to": "A"}, "max": {}}],
            "on_unbound": "escrow"}
    p, call = _hand_gateway(spec)
    held = call({"to": "B", "amt": 5})
    assert held["decision"] == "escrow" and p.gateway.tools.executed == []           # held, not executed
    item = p.escrow.items[held["escrow_id"]]
    assert item["reason"] == "unbound"
    assert call({"to": "B", "amt": 51})["code"] == "ARG_OUT_OF_BOUNDS"              # bounds still deny
    assert call({"to": "A", "amt": 5})["decision"] == "allowed"                     # bound calls stay direct
    with pytest.raises(EscrowError):
        p.escrow.approve(item["id"], "user@example.com")                            # requester cannot approve
    p.escrow.approve(item["id"], "approver-1")
    assert p.escrow.approve(item["id"], "approver-2")["status"] == "executed"
    assert p.gateway.tools.executed[-1] == ("pay", {"to": "B", "amt": 5})
