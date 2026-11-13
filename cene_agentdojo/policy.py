"""Policy v1 inputs: deterministic extraction from the user prompt and trusted environment data.

Plan: docs/09_AgentDojo_Plan.md sections 3 and 5. Sources:
  P  IBANs named in the user prompt                                     (b)
  Q  single-quoted strings in the user prompt                           (b)
  H  counterparty IBANs of past and scheduled transactions              (c)
  B  account balance                                                    (c)
  S  scheduled transactions as (id, current recipient)                  (c)
  F  file names in the environment                                      (c)
Only structured bank-account fields that are not AgentDojo injection vectors are read (the vectors are
file contents and the subject of a transaction). Nothing here reads file contents or transaction subjects.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd
import yaml

from cene.contracts import ContractRegistry, from_dict

HERE = Path(__file__).parent
CONTRACTS_FILE = HERE / "contracts" / "banking_v1.yaml"
POLICY_VERSION = "v1"

IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b")
QUOTED_RE = re.compile(r"'([^'\s][^']*)'")


@dataclass
class TaskFacts:
    prompt_ibans: list[str]                 # P
    quoted: list[str]                       # Q
    history_ibans: list[str]                # H
    balance: float                          # B
    scheduled: list[tuple[int, str]]        # S
    files: list[str]                        # F
    own_iban: str
    notes: list[str] = field(default_factory=list)

    @property
    def payees(self) -> list[str]:
        """P ∪ H, in first-seen order (P first)."""
        return list(dict.fromkeys(self.prompt_ibans + self.history_ibans))

    def to_dict(self) -> dict:
        return {"P": self.prompt_ibans, "Q": self.quoted, "H": self.history_ibans, "B": self.balance,
                "S": [list(s) for s in self.scheduled], "F": self.files, "own_iban": self.own_iban,
                "notes": self.notes}


def prompt_ibans(prompt: str) -> list[str]:
    return list(dict.fromkeys(IBAN_RE.findall(prompt)))


def quoted_strings(prompt: str) -> list[str]:
    """Single-quoted strings, excluding ones that name an existing file (those are file references)."""
    return list(dict.fromkeys(QUOTED_RE.findall(prompt)))


def extract(prompt: str, env) -> TaskFacts:
    acct = env.bank_account
    own = acct.iban
    notes = []
    hist = []
    for t in list(acct.transactions) + list(acct.scheduled_transactions):
        for v in (t.sender, t.recipient):
            if v in ("me", own):
                continue
            if not IBAN_RE.fullmatch(v):
                notes.append(f"non-IBAN counterparty ignored: {v!r}")
                continue
            hist.append(v)
    files = sorted(env.filesystem.files)
    quoted = [q for q in quoted_strings(prompt) if q not in files]
    return TaskFacts(
        prompt_ibans=[i for i in prompt_ibans(prompt) if i != own],
        quoted=quoted,
        history_ibans=list(dict.fromkeys(hist)),
        balance=float(acct.balance),
        scheduled=[(int(t.id), t.recipient) for t in acct.scheduled_transactions],
        files=files,
        own_iban=own,
        notes=notes,
    )


# ------------------------------------------------------------------ contracts and per-task products
def _fill(obj, values: dict):
    if isinstance(obj, dict):
        return {k: _fill(v, values) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_fill(v, values) for v in obj]
    if isinstance(obj, str) and obj in values:
        return copy.deepcopy(values[obj])
    return obj


def load_template() -> dict:
    return yaml.safe_load(CONTRACTS_FILE.read_text(encoding="utf-8"))


def contracts_for(facts: TaskFacts) -> ContractRegistry:
    tpl = load_template()
    values = {"$BALANCE": facts.balance, "$FILES": facts.files}
    return ContractRegistry([from_dict(_fill(c, values)) for c in tpl["contracts"]])


def tables_for(facts: TaskFacts) -> dict[str, pd.DataFrame]:
    """The rows each per-task product holds. A product with no rows is left out of the token."""
    payees = pd.DataFrame({"recipient": pd.Series(facts.payees, dtype=object),
                           "amount_cap": pd.Series([facts.balance] * len(facts.payees), dtype=float)})
    rows = []
    for sid, current in facts.scheduled:
        for r in [None, current] + [p for p in facts.prompt_ibans if p != current]:
            rows.append({"id": sid, "recipient": r})
    scheduled = pd.DataFrame(rows, columns=["id", "recipient"]).astype({"recipient": object})
    password = pd.DataFrame({"password": pd.Series(facts.quoted, dtype=object)})
    return {"ad_payees": payees, "ad_scheduled": scheduled, "ad_password": password}
