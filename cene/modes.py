"""Signed operating modes ("oracle-signed stress manifold switching").

A data product normally uses its 'baseline' reference profile. In a declared emergency (e.g. a
cyclone), legitimate data looks very different from normal and the drift check would block it.
An authorised officer signs a mode statement with a separate *oracle* key held in Cloud KMS/HSM:

    {"aud": "cene-quality", "product": "...", "mode": "stress", "iat": ..., "exp": ..., "reason": "..."}

The quality engine then compares data with the product's 'stress' reference instead, until the
statement expires. Unsigned, wrongly signed, expired or undeclared modes are rejected.
"""
from __future__ import annotations

from .tokens import Keyring, TokenError, verify

MODE_TYP = "CENE-MODE"
MODE_AUD = "cene-quality"


class ModeRegistry:
    def __init__(self, oracle_keyring: Keyring, clock, contracts, audit=None):
        self.keyring = oracle_keyring
        self.clock = clock
        self.contracts = contracts
        self.audit = audit
        self._active: dict[str, tuple[str, float]] = {}

    def switch(self, statement: str) -> dict:
        now = self.clock.now()
        try:
            claims = verify(statement, self.keyring, now, MODE_AUD, typ=MODE_TYP)
        except TokenError as e:
            self._log("mode_rejected", {"reason": e.code})
            raise
        product, mode = claims["product"], claims["mode"]
        c = self.contracts.get(product)
        if mode != "baseline" and mode not in c.modes:
            self._log("mode_rejected", {"product": product, "mode": mode, "reason": "MODE_NOT_DECLARED"})
            raise TokenError("MODE_NOT_DECLARED")
        self._active[product] = (mode, float(claims["exp"]))
        self._log("mode_switched", {"product": product, "mode": mode, "exp": claims["exp"],
                                    "reason": claims.get("reason", "")})
        return {"product": product, "mode": mode, "expires": claims["exp"]}

    def active(self, product: str) -> str:
        mode, exp = self._active.get(product, ("baseline", float("inf")))
        if self.clock.now() > exp:
            self._active.pop(product, None)
            return "baseline"
        return mode

    def _log(self, kind, data):
        if self.audit:
            self.audit.append({"type": kind, **data})
