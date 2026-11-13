"""Context Tokens: compact JWS (ES256) carrying an agent's capabilities for one task.

Header : {"alg": "ES256", "kid": <key id>, "typ": "CENE-CT"}
Payload: see docs/04_Low_Level_Design.md section 3 for every claim.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from .signer import verify_es256
from .util import b64url, b64url_decode, canonical_json, sha256_hex

TOKEN_TYP = "CENE-CT"


class TokenError(Exception):
    def __init__(self, code: str, message: str = ""):
        super().__init__(message or code)
        self.code = code


def issue(claims: dict, signer, typ: str = TOKEN_TYP) -> str:
    header = {"alg": signer.alg, "kid": signer.key_id, "typ": typ}
    signing_input = b64url(canonical_json(header)) + "." + b64url(canonical_json(claims))
    sig = signer.sign(signing_input.encode("ascii"))
    return signing_input + "." + b64url(sig)


def token_hash(token: str) -> str:
    return sha256_hex(token.encode("ascii"))


@dataclass
class Keyring:
    """Public keys the gateway trusts, by key id. Rotation = add the new kid, later remove the old."""

    keys: dict[str, bytes]

    def get(self, kid: str) -> bytes | None:
        return self.keys.get(kid)


def verify(token: str, keyring: Keyring, now: float, audience: str, leeway: float = 2.0,
           typ: str = TOKEN_TYP) -> dict:
    """Verify signature, type, audience and validity window. Returns the claims or raises TokenError."""
    try:
        h_b64, p_b64, s_b64 = token.split(".")
        header = json.loads(b64url_decode(h_b64))
        claims = json.loads(b64url_decode(p_b64))
        sig = b64url_decode(s_b64)
    except Exception as exc:  # malformed in any way
        raise TokenError("TOKEN_MALFORMED", str(exc)) from exc
    if header.get("typ") != typ or header.get("alg") != "ES256":
        raise TokenError("TOKEN_MALFORMED", "unexpected header")
    pem = keyring.get(header.get("kid", ""))
    if pem is None:
        raise TokenError("TOKEN_UNKNOWN_KEY")
    if not verify_es256(pem, (h_b64 + "." + p_b64).encode("ascii"), sig):
        raise TokenError("TOKEN_BAD_SIGNATURE")
    if claims.get("aud") != audience:
        raise TokenError("TOKEN_WRONG_AUDIENCE")
    if now > float(claims.get("exp", 0)) + leeway:
        raise TokenError("TOKEN_EXPIRED")
    if now + leeway < float(claims.get("nbf", claims.get("iat", 0))):
        raise TokenError("TOKEN_NOT_YET_VALID")
    return claims
