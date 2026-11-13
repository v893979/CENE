"""Runs all 12 use-case scenarios end to end (local mode) and fails on any unmet expectation."""
import copy

import pytest

from harness.runner import ScenarioRun, load_scenarios

SCENARIOS = load_scenarios()


@pytest.mark.parametrize("sc", SCENARIOS, ids=[s["id"] for s in SCENARIOS])
def test_scenario(sc):
    res = ScenarioRun(copy.deepcopy(sc)).run()
    failed = [f"{r['n']}. {r['name']}: {r['msg']}" for r in res["steps"] if not r["ok"]]
    assert not failed, "\n".join(failed)
