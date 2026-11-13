"""Quality engine: turns a data product into a score, a tier and a token expiry.

    d-CQI  = sum_i w_i * S_i                       over {fresh, lin, comp, drift, poison}
    Phi    = prod_k min(1, S_k / theta_k)           over the gated vectors (default: fresh, lin,
                                                    drift, poison, comp_crit)
    score  = Gamma * Phi * d-CQI                    Gamma = zero-tolerance gatekeeper (0 or 1)
    tier   = 1 if score >= tier1, 2 if score >= tier2, else 3

The classic linear CQI (fresh, lin, 1 - null ratio) is reported alongside for comparison only.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

from ..contracts import Contract
from . import metrics

DEFAULT_GATE_FRESH = 0.9


@dataclass
class QualityReport:
    product: str
    mode: str
    evaluated_at: float
    vectors: dict
    gamma: float
    gate: float
    dcqi: float
    linear_cqi: float
    score: float
    tier: int
    reasons: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)
    seconds_until_stale: float = 0.0
    flagged_rows: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class QualityEngine:
    def __init__(self, store, clock, modes=None):
        self.store = store
        self.clock = clock
        self.modes = modes  # ModeRegistry or None

    def active_mode(self, product: str) -> str:
        return self.modes.active(product) if self.modes else "baseline"

    def evaluate(self, c: Contract) -> QualityReport:
        now = self.clock.now()
        mode = self.active_mode(c.product)
        q = c.quality
        df = self.store.sample(c.product)
        ref = self.store.reference(c.product, mode)

        last = self.store.last_load_commit(c.product)
        age = None if last is None else max(0.0, now - last)
        s_fresh = metrics.freshness(age, c.freshness_sla)
        s_lin, lin_detail = metrics.lineage(c.upstreams, self.store.lineage_health)
        s_comp, s_comp_crit, null_rates = metrics.completeness(df, c.columns)
        s_drift, drift_detail = metrics.drift(df, ref, c.numeric_columns, c.critical_columns, q["drift"])
        s_poison, poison_detail, flagged = metrics.integrity(
            df, ref, c.columns, c.numeric_columns, c.duplicate_keys, float(q["poison_tolerance"]))
        gamma, broken = metrics.zero_tolerance(df, c.zero_tolerance)

        v = {"fresh": s_fresh, "lin": s_lin, "comp": s_comp, "drift": s_drift, "poison": s_poison,
             "comp_crit": s_comp_crit}
        w = q["weights"]
        dcqi = sum(w[k] * v[k] for k in ("fresh", "lin", "comp", "drift", "poison"))
        gate_cfg = dict(q["gate"])
        gate_cfg.setdefault("fresh", DEFAULT_GATE_FRESH)
        phi = 1.0
        for k, theta in gate_cfg.items():
            phi *= min(1.0, v[k] / float(theta)) if theta > 0 else 1.0
        score = gamma * phi * dcqi

        overall_null = sum(null_rates.values()) / max(1, len(null_rates))
        wl = w["fresh"] + w["lin"] + w["comp"]
        linear = (w["fresh"] * s_fresh + w["lin"] * s_lin + w["comp"] * (1 - overall_null)) / wl

        tiers = q["tiers"]
        tier = 1 if score >= tiers["tier1"] else (2 if score >= tiers["tier2"] else 3)

        reasons = []
        for k, theta in gate_cfg.items():
            if v[k] < theta:
                reasons.append(f"{k}={v[k]:.2f}<{theta}")
        if broken:
            reasons.append("zero_tolerance:" + ",".join(broken))
        if age is None:
            reasons.append("no_load_record")

        until = 0.0 if age is None else metrics.seconds_until_freshness_below(
            age, c.freshness_sla, gate_cfg["fresh"])
        return QualityReport(
            product=c.product, mode=mode, evaluated_at=now, vectors={k: round(x, 4) for k, x in v.items()},
            gamma=gamma, gate=round(phi, 4), dcqi=round(dcqi, 4), linear_cqi=round(linear, 4),
            score=round(score, 4), tier=tier, reasons=reasons,
            detail={"age_seconds": age, "lineage": lin_detail, "null_rates": null_rates,
                    "drift": drift_detail, "integrity": poison_detail},
            seconds_until_stale=max(0.0, until), flagged_rows=sorted(int(i) for i in flagged),
        )
