"""Quota-week planning from measured timing (run setup r3).

Kaggle's GPU quota is weekly. Plan (user, 2026-11-08): week 1 = CHECK + smoke + M1-M4, plus M7/M8 only if both
fit inside the week-1 budget; week 2 (after the reset) = whatever is left: M7/M8 if not done, then M5, M6.
Nothing is dropped unless it does not fit in week 2 either; then M6 goes first, then M5 (M7/M8 are never
dropped before M5/M6). Within a week, sessions are at most `session_max_hours` (the runner's
--stop-after-hours enforces it), each paying `setup_hours` for install and model load.
"""
from __future__ import annotations

import math

EPISODES = {"M1": 16, "M2": 144, "M3": 16, "M4": 144, "M5": 16, "M6": 144, "M7": 16, "M8": 144}
CORE = ["M1", "M2", "M3", "M4"]
PAIR = ["M7", "M8"]
LAST = ["M5", "M6"]


def _week_hours(run_hours: float, setup: float, session_max: float) -> tuple[float, int]:
    if run_hours <= 0:
        return 0.0, 0
    n = max(1, math.ceil(run_hours / (session_max - setup)))
    return run_hours + n * setup, n


def plan_weeks(seconds_per_episode: float, *, week1_budget_hours: float, week2_budget_hours: float,
               week1_used_hours: float = 0.0, setup_hours: float = 0.5, session_max_hours: float = 11.0,
               smoke_episodes: int = 7, drop_order: tuple = ("M6", "M5")) -> dict:
    """week1_used_hours: quota already spent this week by CHECK. smoke_episodes: RUN's smoke (3 M3, 3 M2, 1 M7);
    the 3 M1 smoke episodes ran in CHECK."""
    h = lambda c: EPISODES[c] * seconds_per_episode / 3600  # noqa: E731
    smoke_h = smoke_episodes * seconds_per_episode / 3600
    avail1 = week1_budget_hours - week1_used_hours

    def cost(run_h):
        return _week_hours(run_h, setup_hours, session_max_hours)

    week1, week2 = [], []
    run1 = smoke_h
    for c in CORE:                                    # core first; whatever does not fit spills to week 2
        if cost(run1 + h(c))[0] <= avail1:
            week1.append(c)
            run1 += h(c)
        else:
            week2.append(c)
    if not week2 and cost(run1 + h("M7") + h("M8"))[0] <= avail1:
        week1 += PAIR
        run1 += h("M7") + h("M8")
    else:
        week2 += PAIR
    week2 += LAST
    dropped = []
    for c in drop_order:                              # only if week 2 cannot hold everything
        if cost(sum(h(x) for x in week2))[0] <= week2_budget_hours:
            break
        if c in week2:
            week2.remove(c)
            dropped.append(c)
    w1, n1 = cost(run1) if week1 else (0.0, 0)
    w2, n2 = cost(sum(h(x) for x in week2))
    return {
        "seconds_per_episode": round(seconds_per_episode, 1),
        "week1": {"configs": week1, "gpu_hours": round(w1, 2), "sessions": n1, "budget_left": round(avail1, 2)},
        "week2": {"configs": week2, "gpu_hours": round(w2, 2), "sessions": n2, "budget": week2_budget_hours},
        "dropped": dropped,
        "total_gpu_hours": round(week1_used_hours + w1 + w2, 2),
        "fits": cost(sum(h(x) for x in week2))[0] <= week2_budget_hours,
    }
