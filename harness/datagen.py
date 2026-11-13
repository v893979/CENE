"""Synthetic data products from a small column spec (see any harness/scenarios/*.yaml, key `data`).

Generators:
  seq       prefix + zero-padded counter           {gen: seq, prefix: "INV-", digits: 6, start: 1}
  choice    pick from values (optional weights)    {gen: choice, values: [...], weights: [...]}
  map       derive from another column             {gen: map, from: vendor_id, values: {V-01: ACC-0001}}
  normal    gaussian, optional clip and rounding   {gen: normal, mean: 1, sd: 0.2, clip: [0, 5], round: 2}
  lognormal                                         {gen: lognormal, mean: 8, sigma: 0.6, clip: [10, 5e4]}
  uniform                                           {gen: uniform, low: 0, high: 1}
  int       rounded normal                          {gen: int, mean: 4, sd: 1, clip: [1, 12]}
  const                                             {gen: const, value: "AUD"}
  maybe     another generator, null with prob p    {gen: maybe, p_null: 0.1, of: {gen: const, value: x}}
"""
from __future__ import annotations

import numpy as np
import pandas as pd


def _gen(spec: dict, n: int, rng, df: pd.DataFrame, start: int):
    g = spec["gen"]
    if g == "seq":
        d = int(spec.get("digits", 6))
        s0 = int(spec.get("start", 1)) + start
        return [f"{spec.get('prefix', '')}{i:0{d}d}" for i in range(s0, s0 + n)]
    if g == "choice":
        w = spec.get("weights")
        p = None if w is None else np.array(w, float) / np.sum(w)
        return list(rng.choice(spec["values"], size=n, p=p))
    if g == "map":
        return [spec["values"][v] for v in df[spec["from"]]]
    if g in ("normal", "int"):
        x = rng.normal(spec["mean"], spec["sd"], n)
    elif g == "lognormal":
        x = rng.lognormal(spec["mean"], spec["sigma"], n)
    elif g == "uniform":
        x = rng.uniform(spec["low"], spec["high"], n)
    elif g == "const":
        return [spec["value"]] * n
    elif g == "maybe":
        base = _gen(spec["of"], n, rng, df, start)
        mask = rng.random(n) < float(spec["p_null"])
        return [None if m else v for v, m in zip(base, mask)]
    else:
        raise ValueError(f"unknown generator {g}")
    if "clip" in spec:
        x = np.clip(x, spec["clip"][0], spec["clip"][1])
    if g == "int":
        return np.round(x).astype(int).astype(float)
    return np.round(x, int(spec.get("round", 4)))


def generate(columns: dict, n: int, seed: int, start: int = 0, overrides: dict | None = None) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    df = pd.DataFrame(index=range(n))
    spec_all = {**columns, **(overrides or {})}
    for col, spec in spec_all.items():
        df[col] = _gen(spec, n, rng, df, start)
    return df
