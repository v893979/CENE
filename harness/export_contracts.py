"""Write every scenario's data contracts to harness/contracts/*.yaml (used by the services in
Google Cloud mode, CENE_CONTRACTS_DIR). Run after editing a scenario:  python -m harness.export_contracts
"""
from pathlib import Path

import yaml

from .runner import load_scenarios

OUT = Path(__file__).parent / "contracts"


def main():
    OUT.mkdir(exist_ok=True)
    for sc in load_scenarios():
        for c in sc["contracts"]:
            (OUT / f"{c['product']}.yaml").write_text(yaml.safe_dump(c, sort_keys=False))
            print("wrote", OUT / f"{c['product']}.yaml")


if __name__ == "__main__":
    main()
