"""The five quality vectors used by the d-CQI, plus the zero-tolerance gatekeeper.

All functions return values in [0, 1] (1 = healthy) plus diagnostic detail.
Definitions match docs/04_Low_Level_Design.md section 5.
"""
from __future__ import annotations

import math
import re

import numpy as np
import pandas as pd
from scipy.stats import chi2, wasserstein_distance

LN2 = math.log(2.0)


# --------------------------------------------------------------------------- freshness
def freshness(age_seconds: float | None, sla_seconds: float) -> float:
    """1 inside the SLA, then halves every further SLA period. Unknown load time -> 0."""
    if age_seconds is None:
        return 0.0
    if age_seconds <= sla_seconds:
        return 1.0
    return math.exp(-LN2 * (age_seconds - sla_seconds) / sla_seconds)


def seconds_until_freshness_below(age_seconds: float, sla_seconds: float, threshold: float) -> float:
    """Time left before S_fresh drops below `threshold` (used to set token expiry, t_expiry)."""
    limit_age = sla_seconds * (1.0 + math.log(1.0 / threshold) / LN2)
    return limit_age - age_seconds


# --------------------------------------------------------------------------- lineage
def lineage(upstreams: list[str], health_of) -> tuple[float, dict]:
    """Product of upstream health scores; a missing upstream counts as 0 (untraceable)."""
    detail, s = {}, 1.0
    for node in upstreams:
        h = health_of(node)
        h = 0.0 if h is None else max(0.0, min(1.0, float(h)))
        detail[node] = h
        s *= h
    return s, detail


# --------------------------------------------------------------------------- completeness
def _null_mask(series: pd.Series) -> pd.Series:
    m = series.isna()
    if pd.api.types.is_object_dtype(series.dtype) or pd.api.types.is_string_dtype(series.dtype):
        m = m | (series.astype(str).str.strip() == "")
    return m


def completeness(df: pd.DataFrame, columns: dict) -> tuple[float, float, dict]:
    """Criticality-weighted completeness and the minimum completeness of critical columns."""
    rates, num, den, crit = {}, 0.0, 0.0, []
    for col, spec in columns.items():
        beta = float(spec.get("criticality", 1))
        rate = 1.0 if col not in df.columns or len(df) == 0 else float(_null_mask(df[col]).mean())
        rates[col] = rate
        num += beta * (1.0 - rate)
        den += beta
        if beta >= 3:
            crit.append(1.0 - rate)
    return (num / den if den else 1.0), (min(crit) if crit else 1.0), rates


# --------------------------------------------------------------------------- drift
def _standardise(cur: np.ndarray, ref: np.ndarray):
    mu, sd = ref.mean(axis=0), ref.std(axis=0)
    sd = np.where(sd < 1e-9, 1.0, sd)
    return (cur - mu) / sd, (ref - mu) / sd


def _mmd2_unbiased(x: np.ndarray, y: np.ndarray, bw: float) -> float:
    def k(a, b):
        d2 = ((a[:, None, :] - b[None, :, :]) ** 2).sum(-1)
        return np.exp(-d2 / (2 * bw * bw))

    m, n = len(x), len(y)
    kxx, kyy, kxy = k(x, x), k(y, y), k(x, y)
    np.fill_diagonal(kxx, 0.0)
    np.fill_diagonal(kyy, 0.0)
    return float(kxx.sum() / (m * (m - 1)) + kyy.sum() / (n * (n - 1)) - 2 * kxy.mean())


def drift(df: pd.DataFrame, ref: pd.DataFrame | None, numeric_cols: list[str], critical: list[str],
          cfg: dict, seed: int = 7) -> tuple[float, dict]:
    """Distribution shift of numeric columns against the active reference profile.

    S_drift = min(S_mmd, S_w1, S_flat):
      S_mmd  - unbiased RBF-kernel MMD^2 (median-heuristic bandwidth), compared with the 95th
               percentile of MMD^2 between random halves of the reference (an empirical null);
      S_w1   - worst per-column 1-Wasserstein distance, in units of the reference IQR;
      S_flat - frozen/flat-lined critical columns (std collapses versus the reference).
    """
    if ref is None or ref.empty:
        return 0.6, {"reason": "no_reference_profile"}
    cols = [c for c in numeric_cols if c in df.columns and c in ref.columns]
    if not cols or len(df) < 10:
        return 1.0, {"reason": "not_enough_numeric_data"}
    rng = np.random.default_rng(seed)
    cur = df[cols].apply(pd.to_numeric, errors="coerce").dropna().to_numpy(float)
    refm = ref[cols].apply(pd.to_numeric, errors="coerce").dropna().to_numpy(float)
    if len(cur) < 10 or len(refm) < 20:
        return 1.0, {"reason": "not_enough_numeric_data"}
    n = min(300, len(cur), len(refm) // 2)
    cur_s = cur[rng.choice(len(cur), n, replace=False)]
    ref_s = refm[rng.choice(len(refm), min(2 * n, len(refm)), replace=False)]
    z_cur, z_ref = _standardise(cur_s, ref_s)
    d = np.sqrt(((z_ref[:200, None, :] - z_ref[None, :200, :]) ** 2).sum(-1))
    bw = float(np.median(d[d > 0])) if np.any(d > 0) else 1.0
    mmd = _mmd2_unbiased(z_cur, z_ref[:n], bw)
    null = []
    for _ in range(20):
        idx = rng.permutation(len(z_ref))
        null.append(_mmd2_unbiased(z_ref[idx[:n // 2]], z_ref[idx[n // 2:n]], bw))
    q95 = max(float(np.quantile(null, 0.95)), 1e-4)
    s_mmd = math.exp(-max(0.0, mmd / q95 - 1.0) / float(cfg.get("mmd_scale", 2.0)))

    thr = float(cfg.get("w1_iqr_threshold", 0.35))
    w1n = {}
    for j, c in enumerate(cols):
        q1, q3 = np.quantile(refm[:, j], [0.25, 0.75])
        iqr = (q3 - q1) or (refm[:, j].std() or 1.0)
        w1n[c] = float(wasserstein_distance(cur[:, j], refm[:, j]) / iqr)
    worst = max(w1n.values())
    s_w1 = math.exp(-max(0.0, worst - thr) / thr)

    ratio = float(cfg.get("flatline_ratio", 0.05))
    flat = []
    for j, c in enumerate(cols):
        if c in critical and refm[:, j].std() > 0 and cur[:, j].std() < ratio * refm[:, j].std():
            flat.append(c)
    s_flat = 0.1 if flat else 1.0
    return min(s_mmd, s_w1, s_flat), {"mmd2": mmd, "mmd2_null_q95": q95, "w1_iqr": w1n, "flatlined": flat}


# --------------------------------------------------------------------------- integrity / poisoning
def _rule_violations(df: pd.DataFrame, columns: dict) -> pd.Series:
    bad = pd.Series(False, index=df.index)
    for col, spec in columns.items():
        if col not in df.columns:
            continue
        s = df[col]
        present = ~_null_mask(s)
        if "pattern" in spec:
            rx = re.compile(spec["pattern"])
            bad |= present & ~s.astype(str).map(lambda v: bool(rx.fullmatch(v)))
        if "allowed" in spec:
            bad |= present & ~s.isin(spec["allowed"])
        if spec.get("type") == "number":
            num = pd.to_numeric(s, errors="coerce")
            bad |= present & num.isna()
            if "min" in spec:
                bad |= present & (num < float(spec["min"]))
            if "max" in spec:
                bad |= present & (num > float(spec["max"]))
    return bad


def integrity(df: pd.DataFrame, ref: pd.DataFrame | None, columns: dict, numeric_cols: list[str],
              duplicate_keys: list[str], tolerance: float) -> tuple[float, dict, set]:
    """Contract-rule violations + multivariate outliers + suspicious duplicate clusters.

    Returns (S_poison, detail, flagged_row_indices). Flagged rows are excluded from token bindings.
    """
    if df.empty:
        return 1.0, {"rows": 0}, set()
    viol = _rule_violations(df, columns)

    anom = pd.Series(False, index=df.index)
    cols = [c for c in numeric_cols if c in df.columns and ref is not None and c in ref.columns]
    if ref is not None and cols and len(ref) > len(cols) + 5:
        from sklearn.covariance import LedoitWolf

        refm = ref[cols].apply(pd.to_numeric, errors="coerce").dropna().to_numpy(float)
        curd = df[cols].apply(pd.to_numeric, errors="coerce")
        ok = curd.notna().all(axis=1)
        mu, sd = refm.mean(0), refm.std(0)
        sd = np.where(sd < 1e-9, 1.0, sd)
        lw = LedoitWolf().fit((refm - mu) / sd)
        z = (curd[ok].to_numpy(float) - mu) / sd
        d2 = np.einsum("ij,jk,ik->i", z, lw.precision_, z)
        anom.loc[ok[ok].index] = d2 > chi2.ppf(0.999, len(cols))

    dup = pd.Series(False, index=df.index)
    keys = [k for k in duplicate_keys if k in df.columns]
    if keys:
        sizes = df.groupby(keys, dropna=False)[keys[0]].transform("size")
        dup = sizes >= 3

    viol_frac = float(viol.mean())
    anom_frac = float(anom.mean())
    dup_frac = float(dup.mean())
    excess_anom = max(0.0, anom_frac - 0.01)  # ~0.1% expected by chance; allow 1%
    s = max(0.0, min(1.0, 1.0 - (viol_frac + excess_anom + dup_frac) / tolerance))
    flagged = set(df.index[viol | anom | dup].tolist())
    return s, {"rule_violation_rate": viol_frac, "outlier_rate": anom_frac,
               "duplicate_cluster_rate": dup_frac}, flagged


# --------------------------------------------------------------------------- zero tolerance
def zero_tolerance(df: pd.DataFrame, rules: list[dict]) -> tuple[float, list[str]]:
    """Gamma(D): 0 if any row breaks any zero-tolerance rule (or the column is missing), else 1."""
    broken = []
    for r in rules:
        col, kind = r["column"], r["rule"]
        if col not in df.columns:
            broken.append(f"{col}:missing_column")
            continue
        s = df[col]
        if kind == "not_null":
            hit = _null_mask(s).any()
        elif kind == "equals":
            hit = (s != r["value"]).any()
        elif kind == "in":
            hit = (~s.isin(r["values"])).any()
        elif kind == "min":
            hit = (pd.to_numeric(s, errors="coerce") < float(r["value"])).any()
        elif kind == "max":
            hit = (pd.to_numeric(s, errors="coerce") > float(r["value"])).any()
        elif kind == "not_in_source":
            hit = s.isin(r["values"]).any()
        else:
            raise ValueError(f"unknown zero-tolerance rule '{kind}'")
        if hit:
            broken.append(f"{col}:{kind}")
    return (0.0 if broken else 1.0), broken
