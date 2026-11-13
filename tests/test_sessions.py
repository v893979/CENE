"""Quota-week planning (cene_agentdojo/sessions.py, run setup r3)."""
from cene_agentdojo.sessions import plan_weeks

KW = dict(week1_budget_hours=13.5, week2_budget_hours=28.5, week1_used_hours=0.5, setup_hours=0.4)


def test_fast_everything_core_and_pair_in_week1():
    p = plan_weeks(60, **KW)          # M1-M4 = 320 eps = 5.3 h; + M7/M8 160 eps = 2.7 h -> fits 13 h
    assert p["week1"]["configs"] == ["M1", "M2", "M3", "M4", "M7", "M8"]
    assert p["week2"]["configs"] == ["M5", "M6"] and p["dropped"] == [] and p["fits"]


def test_pair_moves_to_week2_when_it_does_not_fit_whole():
    p = plan_weeks(120, **KW)         # core 10.7 h + setup fits; the pair (5.3 h) does not
    assert p["week1"]["configs"] == ["M1", "M2", "M3", "M4"]
    assert p["week2"]["configs"] == ["M7", "M8", "M5", "M6"] and p["dropped"] == []


def test_core_spills_and_drops_only_after_week2():
    p = plan_weeks(240, **KW)         # core alone ~21 h: spills; week 2 cannot hold everything
    assert p["week1"]["configs"][:2] == ["M1", "M2"] and "M4" in p["week2"]["configs"]
    assert p["dropped"][0] == "M6"
    assert "M7" in p["week2"]["configs"] and "M8" in p["week2"]["configs"]   # never dropped before M5/M6
    for w in ("week1", "week2"):
        assert p[w]["gpu_hours"] <= (KW["week1_budget_hours"] - KW["week1_used_hours"] if w == "week1"
                                     else KW["week2_budget_hours"])
