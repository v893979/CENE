"""cene_agentdojo.compare_models helpers (second-model side-by-side), on small hand-made inputs."""
from __future__ import annotations

from cene_agentdojo.compare_models import mcnemar_exact, ordering, overlap, paired, split_non_attacker


def _ep(utility, security, error=None):
    return {"utility": utility, "security": security, "error": error}


def test_mcnemar_exact():
    assert mcnemar_exact(0, 0) is None
    assert mcnemar_exact(0, 3) == 0.25          # 2 * (1/8)
    assert mcnemar_exact(5, 5) == 1.0


def test_paired_leaves_out_errored_pairs():
    a = {"M2|u0|i0": _ep(True, True), "M2|u0|i1": _ep(False, True), "M2|u1|i0": _ep(True, False, "oom")}
    b = {"M4|u0|i0": _ep(True, False), "M4|u0|i1": _ep(False, False), "M4|u1|i0": _ep(True, False)}
    r = paired(a, b, "security", "M2", "M4")
    assert r["pairs"] == 2 and r["only_M2"] == 2 and r["only_M4"] == 0 and r["both"] == 0


def test_overlap_and_ordering():
    x = {"k": 1, "n": 10, "rate": 0.1, "wilson95": [0.02, 0.4]}
    y = {"k": 9, "n": 10, "rate": 0.9, "wilson95": [0.6, 0.98]}
    assert overlap(x, y) is False and overlap(x, x) is True and overlap(x, None) is None
    a = {"configs": {"M2": {"asr_excluding_errors": y}, "M4": {"asr_excluding_errors": x}}}
    assert [r["config"] for r in ordering(a, ["M2", "M4"], "asr_excluding_errors")] == ["M4", "M2"]


def test_split_non_attacker_by_recipient():
    att = "US133000000121212121212"
    analysis = {"cene": {"M4_non_attacker_writes_not_allowed": [
        {"pair": "M4|user_task_2|injection_task_0", "tool": "schedule_transaction", "outcome": "held:unbound",
         "args": {"recipient": att + "1212"}},
        {"pair": "M4|user_task_9|injection_task_0", "tool": "update_scheduled_transaction", "outcome": "held:unbound",
         "args": {"recipient": "SE3550000000054910000003"}},
        {"pair": "M4|user_task_13|injection_task_0", "tool": "update_user_info", "outcome": "held:always_escrow",
         "args": {"city": "x"}}]}}
    sig = [{"function": "send_money", "args": {"recipient": att}}]
    eps = {k: {"hijack_signatures": sig} for k in ("M4|user_task_2|injection_task_0", "M4|user_task_9|injection_task_0",
                                                    "M4|user_task_13|injection_task_0")}
    out = split_non_attacker(analysis, eps)
    assert out["held:unbound"]["recipient_like_attacker_iban"]["writes"] == 1
    assert out["held:unbound"]["other_recipient"]["by_user_task"] == {"user_task_9": 1}
    assert out["held:always_escrow"]["no_recipient_arg"]["writes"] == 1
