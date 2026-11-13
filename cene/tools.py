"""Mock tool servers that stand in for the real systems agents act on (payments, ERP, SCADA, ...).

Handlers (chosen per tool in the data contract, key `handler`):
  record   - a state-changing action; appended to `executed` so tests can see what really happened
  lookup   - read one row of a data product by key column
  external - fetch an external document; the result is marked untrusted (taints the session)

On Google Cloud these run as one small Cloud Run service; replace a handler with a real API client
when you connect a real system.
"""
from __future__ import annotations


class ToolError(Exception):
    pass


def jsonable(row: dict | None) -> dict | None:
    """Convert a data row (pandas / BigQuery types) into plain JSON types."""
    if row is None:
        return None
    import datetime
    import decimal

    out = {}
    for k, v in row.items():
        try:
            import pandas as pd

            if v is None or (not isinstance(v, (list, dict, str)) and pd.isna(v)):
                out[k] = None
                continue
        except (TypeError, ValueError):
            pass
        if hasattr(v, "item") and not isinstance(v, (str, bytes)):
            v = v.item()  # numpy scalars
        if isinstance(v, (datetime.datetime, datetime.date)):
            v = v.isoformat()
        elif isinstance(v, decimal.Decimal):
            v = float(v)
        elif not isinstance(v, (str, int, float, bool, list, dict)):
            v = str(v)
        out[k] = v
    return out


class ToolService:
    def __init__(self, contracts, store, clock, documents: dict | None = None):
        self.contracts = contracts
        self.store = store
        self.clock = clock
        self.documents = documents or {}
        self.executed: list[dict] = []
        self._specs = {}
        for c in contracts.all():
            for kind in ("read", "write"):
                for name, spec in (c.tools.get(kind) or {}).items():
                    self._specs[name] = {"product": c.product, "kind": kind, **(spec or {})}

    def call(self, tool: str, args: dict, claims: dict | None = None) -> dict:
        spec = self._specs.get(tool)
        if spec is None:
            raise ToolError(f"unknown tool {tool}")
        handler = spec.get("handler", "record" if spec["kind"] == "write" else "lookup")
        if handler == "record":
            rec = {"tool": tool, "args": dict(args), "agent": (claims or {}).get("sub"), "ts": self.clock.now()}
            self.executed.append(rec)
            return {"status": "done", "tool": tool}
        if handler == "lookup":
            key = spec["key"]
            row = self.store.lookup(spec.get("from_product", spec["product"]), key, args.get(key))
            return {"status": "ok", "row": jsonable(row)}
        if handler == "external":
            doc = self.documents.get(args.get("document_id"), {"text": ""})
            return {"status": "ok", "document": doc, "untrusted": True}
        raise ToolError(f"unknown handler {handler}")

    def executed_count(self, tool: str | None = None) -> int:
        return sum(1 for e in self.executed if tool is None or e["tool"] == tool)
