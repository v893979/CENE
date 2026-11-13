"""Signing back-ends.

Both back-ends use ECDSA P-256 with SHA-256 (JWS "ES256"), so tokens signed locally and
tokens signed by Cloud KMS / Cloud HSM are verified by exactly the same code.

* LocalSigner  - key held in memory or in a PEM file (development and tests only).
* KmsSigner    - key held in Cloud KMS; set the key's protection level to HSM to use Cloud HSM.
                 The private key never leaves KMS; only the digest is sent for signing.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
    encode_dss_signature,
)


def der_to_raw(sig_der: bytes) -> bytes:
    r, s = decode_dss_signature(sig_der)
    return r.to_bytes(32, "big") + s.to_bytes(32, "big")


def raw_to_der(sig_raw: bytes) -> bytes:
    if len(sig_raw) != 64:
        raise ValueError("ES256 signature must be 64 bytes")
    return encode_dss_signature(int.from_bytes(sig_raw[:32], "big"), int.from_bytes(sig_raw[32:], "big"))


class LocalSigner:
    alg = "ES256"

    def __init__(self, key_id: str = "local-dev-1", pem_path: str | None = None):
        self.key_id = key_id
        if pem_path and Path(pem_path).exists():
            self._key = serialization.load_pem_private_key(Path(pem_path).read_bytes(), password=None)
        else:
            self._key = ec.generate_private_key(ec.SECP256R1())
            if pem_path:
                Path(pem_path).write_bytes(
                    self._key.private_bytes(
                        serialization.Encoding.PEM,
                        serialization.PrivateFormat.PKCS8,
                        serialization.NoEncryption(),
                    )
                )

    def sign(self, data: bytes) -> bytes:
        """Return a raw (r||s) 64-byte ES256 signature."""
        return der_to_raw(self._key.sign(data, ec.ECDSA(hashes.SHA256())))

    def public_pem(self) -> bytes:
        return self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        )


class KmsSigner:
    """Cloud KMS asymmetric signing key (purpose ASYMMETRIC_SIGN, algorithm EC_SIGN_P256_SHA256).

    key_version_name looks like:
    projects/P/locations/L/keyRings/R/cryptoKeys/K/cryptoKeyVersions/1
    """

    alg = "ES256"

    def __init__(self, key_version_name: str, key_id: str | None = None):
        from google.cloud import kms  # imported lazily so local mode needs no GCP libraries

        self._client = kms.KeyManagementServiceClient()
        self._name = key_version_name
        self.key_id = key_id or key_version_name.rsplit("/cryptoKeys/", 1)[-1].replace("/cryptoKeyVersions/", "-v")
        self._pem: bytes | None = None

    def sign(self, data: bytes) -> bytes:
        digest = hashlib.sha256(data).digest()
        resp = self._client.asymmetric_sign(request={"name": self._name, "digest": {"sha256": digest}})
        return der_to_raw(resp.signature)

    def public_pem(self) -> bytes:
        if self._pem is None:
            self._pem = self._client.get_public_key(request={"name": self._name}).pem.encode()
        return self._pem


def verify_es256(public_pem: bytes, data: bytes, sig_raw: bytes) -> bool:
    key = serialization.load_pem_public_key(public_pem)
    try:
        key.verify(raw_to_der(sig_raw), data, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError):
        return False
