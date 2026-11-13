"""Wires all components together.

build_platform(settings) is used by the HTTP services, the scenario harness and the tests.
MODE=local  -> in-memory data store, in-memory state, local keys, audit in memory/file.
MODE=gcp    -> BigQuery, Memorystore (Redis), Cloud KMS keys, audit in Cloud Storage.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from .audit import AuditLog, GcsSink, LocalFileSink, MemorySink
from .contracts import ContractRegistry
from .control_plane import ControlPlane
from .datastore import BigQueryDataStore, LocalDataStore
from .escrow import EscrowService
from .gateway import Gateway
from .modes import ModeRegistry
from .quality.engine import QualityEngine
from .screening import make_screener
from .signer import KmsSigner, LocalSigner
from .stores import make_store
from .tokens import Keyring
from .tools import ToolService
from .util import Clock


@dataclass
class Settings:
    mode: str = "local"
    contracts_dir: str = "harness/contracts"
    project: str = ""
    dataset: str = "cene"
    redis_url: str | None = None
    token_key: str | None = None   # KMS key version for Context Tokens
    oracle_key: str | None = None  # KMS key version for signed mode statements
    audit_key: str | None = None   # KMS key version for audit checkpoints
    audit_bucket: str | None = None
    audit_file: str | None = None
    token_ttl: float = 900
    screener: str = "none"            # none | marker (tests) | model_armor
    model_armor_template: str | None = None

    @classmethod
    def from_env(cls) -> "Settings":
        e = os.environ.get
        return cls(mode=e("CENE_MODE", "local"), contracts_dir=e("CENE_CONTRACTS_DIR", "harness/contracts"),
                   project=e("GOOGLE_CLOUD_PROJECT", ""), dataset=e("CENE_DATASET", "cene"),
                   redis_url=e("CENE_REDIS_URL") or None, token_key=e("CENE_TOKEN_KEY") or None,
                   oracle_key=e("CENE_ORACLE_KEY") or None, audit_key=e("CENE_AUDIT_KEY") or None,
                   audit_bucket=e("CENE_AUDIT_BUCKET") or None, audit_file=e("CENE_AUDIT_FILE") or None,
                   token_ttl=float(e("CENE_TOKEN_TTL", "900")), screener=e("CENE_SCREENER", "none"),
                   model_armor_template=e("CENE_MODEL_ARMOR_TEMPLATE") or None)


@dataclass
class Platform:
    settings: Settings
    clock: Clock
    contracts: ContractRegistry
    data: object
    state: object
    token_signer: object
    oracle_signer: object
    audit_signer: object
    audit: AuditLog
    audit_sink: object
    modes: ModeRegistry
    quality: QualityEngine
    control: ControlPlane
    tools: ToolService
    escrow: EscrowService
    gateway: Gateway
    keyring: Keyring


@dataclass
class ToolPlatform:
    """What the tools service needs: data and contracts only. It holds no keys, so it needs no KMS access."""
    settings: Settings
    tools: ToolService


def build_tool_service(settings: Settings | None = None) -> ToolPlatform:
    s = settings or Settings.from_env()
    contracts = ContractRegistry.from_dir(s.contracts_dir)
    data = BigQueryDataStore(s.project, s.dataset) if s.mode == "gcp" else LocalDataStore()
    return ToolPlatform(s, ToolService(contracts, data, Clock()))


def build_platform(settings: Settings | None = None, contracts: ContractRegistry | None = None,
                   clock: Clock | None = None, documents: dict | None = None) -> Platform:
    s = settings or Settings.from_env()
    clock = clock or Clock()
    contracts = contracts or ContractRegistry.from_dir(s.contracts_dir)
    if s.mode == "gcp":
        data = BigQueryDataStore(s.project, s.dataset)
        token_signer = KmsSigner(s.token_key)
        oracle_signer = KmsSigner(s.oracle_key) if s.oracle_key else None
        audit_signer = KmsSigner(s.audit_key or s.token_key)
        sink = GcsSink(s.audit_bucket) if s.audit_bucket else MemorySink()
    else:
        data = LocalDataStore()
        token_signer = LocalSigner("local-token-1")
        oracle_signer = LocalSigner("local-oracle-1")
        audit_signer = LocalSigner("local-audit-1")
        sink = LocalFileSink(s.audit_file) if s.audit_file else MemorySink()
    state = make_store(s.redis_url)
    audit = AuditLog(sink, audit_signer, clock)
    oracle_ring = Keyring({oracle_signer.key_id: oracle_signer.public_pem()} if oracle_signer else {})
    modes = ModeRegistry(oracle_ring, clock, contracts, audit)
    quality = QualityEngine(data, clock, modes)
    control = ControlPlane(contracts, quality, data, token_signer, clock, audit, s.token_ttl)
    tools = ToolService(contracts, data, clock, documents)
    escrow = EscrowService(audit, clock)
    keyring = Keyring({token_signer.key_id: token_signer.public_pem()})
    gateway = Gateway(keyring, state, tools, escrow, audit, clock,
                      make_screener(s.screener, s.model_armor_template))
    return Platform(s, clock, contracts, data, state, token_signer, oracle_signer, audit_signer, audit, sink,
                    modes, quality, control, tools, escrow, gateway, keyring)
