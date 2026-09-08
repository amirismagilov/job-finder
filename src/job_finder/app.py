# region MODULE_CONTRACT [DOMAIN(9): RuntimeComposition; CONCEPT(9): DependencyAssembly; TECH(8): Python]
## @file app.py
## @brief Composition root for the local autonomous agent.
## @modulecontract
## @purpose Assemble storage, Keychain, browser bridge, web adapter, LLM and worker in one auditable location.
## @scope Production runtime construction with optional test substitutions.
## @input Config, SecretStore and database path overrides.
## @output JobFinderService.
## @invariants Official applicant OAuth client is not part of the runtime graph.
## @changes LAST_CHANGE: [v0.2.1 — Wired the configured shared read throttler into the HH adapter.]
## @modulemap
## FUNC 10[Creates production service graph] => create_service
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: composition root, bridge, web client, LLM, autonomous worker, Keychain
# STRUCTURE: config + Keychain + SQLite -> bridge -> web/LLM -> worker -> service

from pathlib import Path
from typing import Any

from .autonomy import AutonomousWorker
from .bridge_server import BrowserBridgeServer
from .config import Config, load_config
from .keychain import MacOSKeychain
from .knowledge import KnowledgeBase
from .llm import LLMProvider
from .service import JobFinderService
from .storage import Storage
from .web_client import HHWebClient, ReadThrottler


def create_service(*, config: Config | None = None, secrets_store: Any | None = None, database_path: Path | None = None) -> JobFinderService:
    config = config or load_config()
    secrets_store = secrets_store or MacOSKeychain()
    storage = Storage(database_path or config.database_path)
    knowledge = KnowledgeBase(config.profile)
    bridge = BrowserBridgeServer(config.bridge, secrets_store, storage=storage)
    client = HHWebClient(bridge, read_throttler=ReadThrottler(config.safety.minimum_read_interval_seconds))
    llm = LLMProvider(config.llm, secrets_store)
    worker = AutonomousWorker(config, client, llm, storage, knowledge)
    return JobFinderService(config, bridge, client, worker, storage, knowledge)
