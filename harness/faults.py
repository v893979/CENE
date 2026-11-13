"""Data faults that replicate the failure in each use case. Each works on the LocalDataStore;
docs/05_Test_Scenarios.md gives the equivalent BigQuery SQL for running them on Google Cloud.

  stale             load finished `age_seconds` ago (pipeline stalled, data still served)
  reload            a fresh load just committed
  null_spike        `fraction` of a column becomes null
  scale_column      unit change: multiply `fraction` of a column by `factor`, optionally set other columns
  shift             distribution shift: add/multiply a column for `fraction` of rows
  flatline          frozen sensor / replayed feed: every value equals the first value
  set_value         set columns on rows matching `where` (or `fraction` of rows)
  duplicate_cluster `count` near-identical rows copied from one template row with new ids
  regenerate        replace the table with freshly generated rows from a named profile
  lineage           set an upstream node's health (0 = broken/unapproved, 1 = healthy)
  drop_column       column disappears (schema change)
"""
from __future__ import annotations

import numpy as np

from .datagen import generate


def apply(fault: dict, store, scenario: dict, clock, rng) -> str:
    kind = fault["type"]
    p = fault.get("product")
    if kind == "lineage":
        store.set_lineage_health(fault["node"], float(fault["health"]))
        return f"lineage {fault['node']} health={fault['health']}"
    df = store.tables[p].copy()
    now = clock.now()
    commit = store.load_commits.get(p, now)
    if kind == "stale":
        store.load_commits[p] = now - float(fault["age_seconds"])
        return f"{p}: last load {fault['age_seconds']}s ago"
    if kind == "reload":
        store.load_commits[p] = now - float(fault.get("age_seconds", 1))
        return f"{p}: fresh load"
    if kind == "regenerate":
        prof = scenario["data"][p]
        prof_name = fault.get("profile", "baseline")
        over = (prof.get("profiles", {}).get(prof_name) or {}).get("overrides") if prof_name != "baseline" else None
        df = generate(prof["columns"], int(fault.get("rows", prof["rows"])), int(fault.get("seed", 99)),
                      start=int(fault.get("start", 0)), overrides=over)
        store.put_table(p, df, now - float(fault.get("age_seconds", 1)))
        return f"{p}: regenerated with profile '{prof_name}'"
    n = len(df)
    pick = rng.random(n) < float(fault.get("fraction", 1.0))
    col = fault.get("column")
    if kind == "null_spike":
        df[col] = df[col].astype(object)
        df.loc[pick, col] = None
    elif kind == "scale_column":
        df.loc[pick, col] = df.loc[pick, col].astype(float) * float(fault["factor"])
        for c, v in (fault.get("set") or {}).items():
            df.loc[pick, c] = v
    elif kind == "shift":
        x = df.loc[pick, col].astype(float)
        df.loc[pick, col] = x * float(fault.get("mult", 1.0)) + float(fault.get("add", 0.0))
    elif kind == "flatline":
        df[col] = df[col].iloc[0]
    elif kind == "set_value":
        mask = pick
        for c, v in (fault.get("where") or {}).items():
            mask = mask & (df[c] == v)
        for c, v in fault["set"].items():
            if v is None or isinstance(v, str):
                df[c] = df[c].astype(object)
            df.loc[mask, c] = v
    elif kind == "duplicate_cluster":
        tmpl = df.iloc[[int(fault.get("template_row", 0))]]
        rows = tmpl.loc[tmpl.index.repeat(int(fault["count"]))].reset_index(drop=True)
        idc, prefix, digits = fault["id_column"], fault["id_prefix"], int(fault.get("id_digits", 6))
        start = int(fault.get("id_start", 900000))
        rows[idc] = [f"{prefix}{i:0{digits}d}" for i in range(start, start + len(rows))]
        for c in fault.get("vary", []):
            rows[c] = [f"{v}-{i}" for i, v in enumerate(rows[c])]
        import pandas as pd

        df = pd.concat([df, rows], ignore_index=True)
    elif kind == "drop_column":
        df = df.drop(columns=[col])
    else:
        raise ValueError(f"unknown fault {kind}")
    store.put_table(p, df, commit)
    return f"{p}: {kind} {col or ''}".strip()


def make_rng(seed: int = 11):
    return np.random.default_rng(seed)
