"""False-alarm calibration: how often is *healthy* data scored below Tier 1?

For each scenario's data products, generate `n` fresh healthy batches (new random seeds, same
generator as the reference) and evaluate them. Run this on your own data before relying on the
thresholds: lower `w1_iqr_threshold` / `mmd_scale` make drift stricter, higher make it more tolerant.

    python -m harness.calibrate            # 20 batches per product
    python -m harness.calibrate -n 50 uc07
"""
from __future__ import annotations

import argparse

from cene.contracts import ContractRegistry, from_dict
from cene.platform import Settings, build_platform
from cene.util import Clock

from .datagen import generate
from .runner import load_scenarios


def calibrate(sc: dict, n: int) -> dict:
    out = {}
    for prod, spec in sc["data"].items():
        tiers = []
        for i in range(n):
            clock = Clock(start=1_800_000_000.0)
            p = build_platform(Settings(mode="local"), ContractRegistry([from_dict(c) for c in sc["contracts"]]), clock)
            seed = int(spec.get("seed", 1))
            p.data.put_reference(prod, "baseline", generate(spec["columns"], int(spec.get("reference_rows", 1000)),
                                                            seed + 1000, start=500000))
            p.data.put_table(prod, generate(spec["columns"], int(spec["rows"]), 10_000 + i),
                             clock.now() - float(spec.get("load_age_seconds", 60)))
            for node, h in (sc.get("lineage") or {}).items():
                p.data.set_lineage_health(node, h)
            tiers.append(p.quality.evaluate(p.contracts.get(prod)).tier)
        out[prod] = {"batches": n, "tier1": tiers.count(1), "false_alarm_rate": 1 - tiers.count(1) / n}
    return out


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("ids", nargs="*")
    ap.add_argument("-n", type=int, default=20)
    a = ap.parse_args(argv)
    print(f"{'scenario':10} {'product':22} {'healthy batches':>16} {'false alarms':>13}")
    for sc in load_scenarios(a.ids):
        for prod, r in calibrate(sc, a.n).items():
            print(f"{sc['id']:10} {prod:22} {r['batches']:>16} {r['false_alarm_rate']:>12.0%}")


if __name__ == "__main__":
    main()
