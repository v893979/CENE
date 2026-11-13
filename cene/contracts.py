"""Autonomous Data Contracts (ADC): one YAML document per data product.

The contract is the single source of truth for:
  * column semantics (type, criticality 1-3, pattern / range / allowed values / unit),
  * zero-tolerance rules (any violation blocks all write tools),
  * freshness SLA and upstream lineage,
  * quality weights, gate thresholds and tier cut-offs,
  * which tools the product unlocks, their argument bounds, row bindings and limits,
  * temporal (stateful) rules, taint handling and escrow settings,
  * alternative reference profiles ("modes", e.g. a signed storm/stress mode).

See docs/04_Low_Level_Design.md section 4 for the full schema.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

DEFAULT_QUALITY = {
    "weights": {"fresh": 0.2, "lin": 0.2, "comp": 0.2, "drift": 0.2, "poison": 0.2},
    "gate": {"lin": 0.6, "drift": 0.5, "poison": 0.6, "comp_crit": 0.9},
    "tiers": {"tier1": 0.85, "tier2": 0.70},
    "poison_tolerance": 0.05,
    "drift": {"w1_iqr_threshold": 0.35, "flatline_ratio": 0.05, "mmd_scale": 2.0},
}


class ContractError(ValueError):
    pass


@dataclass
class Contract:
    product: str
    owner: str
    columns: dict
    freshness_sla: float
    upstreams: list = field(default_factory=list)
    zero_tolerance: list = field(default_factory=list)
    quality: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)
    temporal: list = field(default_factory=list)
    taint: dict = field(default_factory=dict)
    escrow: dict = field(default_factory=dict)
    modes: dict = field(default_factory=dict)
    duplicate_keys: list = field(default_factory=list)
    key_column: str | None = None
    raw: dict = field(default_factory=dict)

    # ------------------------------------------------------------------
    @property
    def numeric_columns(self) -> list[str]:
        return [c for c, s in self.columns.items() if s.get("type") == "number"]

    @property
    def critical_columns(self) -> list[str]:
        return [c for c, s in self.columns.items() if int(s.get("criticality", 1)) >= 3]

    def read_tools(self) -> list[str]:
        return list(self.tools.get("read", []))

    def write_tools(self) -> dict:
        return dict(self.tools.get("write", {}))


def _merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in (override or {}).items():
        out[k] = _merge(base[k], v) if isinstance(v, dict) and isinstance(base.get(k), dict) else v
    return out


def from_dict(d: dict) -> Contract:
    for req in ("product", "columns", "freshness"):
        if req not in d:
            raise ContractError(f"contract missing '{req}'")
    q = _merge(DEFAULT_QUALITY, d.get("quality", {}))
    w = q["weights"]
    if abs(sum(w.values()) - 1.0) > 1e-6:
        raise ContractError(f"{d['product']}: quality weights must sum to 1")
    for c, spec in d["columns"].items():
        if spec.get("type") not in ("string", "number", "timestamp"):
            raise ContractError(f"{d['product']}.{c}: type must be string, number or timestamp")
    return Contract(
        product=d["product"],
        owner=d.get("owner", "unknown"),
        columns=d["columns"],
        freshness_sla=float(d["freshness"]["sla_seconds"]),
        upstreams=list(d.get("lineage", {}).get("upstreams", [])),
        zero_tolerance=list(d.get("zero_tolerance", [])),
        quality=q,
        tools=d.get("tools", {}),
        temporal=list(d.get("temporal", [])),
        taint=d.get("taint", {}),
        escrow=_merge({"approvers_required": 2}, d.get("escrow", {})),
        modes=d.get("modes", {}),
        duplicate_keys=list(d.get("duplicate_keys", [])),
        key_column=d.get("key_column"),
        raw=d,
    )


def load(path: str | Path) -> Contract:
    return from_dict(yaml.safe_load(Path(path).read_text()))


class ContractRegistry:
    def __init__(self, contracts: list[Contract] | None = None):
        self._c = {c.product: c for c in (contracts or [])}

    def add(self, c: Contract) -> None:
        self._c[c.product] = c

    def get(self, product: str) -> Contract:
        if product not in self._c:
            raise KeyError(f"unknown data product '{product}'")
        return self._c[product]

    def all(self) -> list[Contract]:
        return list(self._c.values())

    @classmethod
    def from_dir(cls, directory: str | Path) -> "ContractRegistry":
        return cls([load(p) for p in sorted(Path(directory).glob("*.yaml"))])
