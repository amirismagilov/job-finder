from __future__ import annotations

from pathlib import Path

from .config import Config, load_config
from .hh_client import HHClient, SecretStore
from .keychain import MacOSKeychain
from .knowledge import KnowledgeBase
from .service import JobFinderService
from .storage import Storage


def create_service(*, config: Config | None = None, secrets_store: SecretStore | None = None, database_path: Path | None = None) -> JobFinderService:
    config = config or load_config()
    secrets_store = secrets_store or MacOSKeychain()
    storage = Storage(database_path or config.database_path)
    return JobFinderService(config, HHClient(config, secrets_store), storage, KnowledgeBase(config.profile))
