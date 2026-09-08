# region MODULE_CONTRACT [DOMAIN(9): LocalApplication; CONCEPT(9): Facade, Observability; TECH(8): Python]
## @file service.py
## @brief CLI/MCP facade over the bridge and autonomous worker.
## @modulecontract
## @purpose Expose safe status, dry-run discovery and cycle controls without exposing credentials or transport payloads.
## @scope Local process lifecycle and public application methods.
## @input Validated config and composed runtime components.
## @output JSON-serializable status and reports.
## @invariants Starting a bridge does not enable autonomy; status never reveals secret values.
## @changes LAST_CHANGE: [v0.2.0 — Replaced two-step official API facade with autonomous bridge facade.]
## @modulemap
## CLASS 9[Local control facade] => JobFinderService
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: service facade, status, bridge lifecycle, autonomous cycle, no secrets
# STRUCTURE: CLI/MCP -> service -> bridge lifecycle + worker -> serializable result

from typing import Any

from .autonomy import AutonomousWorker
from .bridge_server import BrowserBridgeServer
from .config import Config
from .knowledge import KnowledgeBase
from .storage import Storage
from .web_client import HHWebClient


class JobFinderService:
    def __init__(self, config: Config, bridge: BrowserBridgeServer, client: HHWebClient, worker: AutonomousWorker, storage: Storage, knowledge: KnowledgeBase) -> None:
        self.config, self.bridge, self.client = config, bridge, client
        self.worker, self.storage, self.knowledge = worker, storage, knowledge

    def status(self) -> dict[str, Any]:
        return {
            "version": "0.2.0",
            "mode": "local-browser-session-bridge",
            "ui_automation": False,
            "official_applicant_api": False,
            "bridge": {"paired": self.bridge.paired, "connected": self.bridge.connected, "listening": self.bridge._server is not None},
            "autonomy": {"enabled": self.storage.autonomy_enabled(), "dry_run_until_enabled": not self.storage.autonomy_enabled()},
            "knowledge": self.knowledge.health(),
            "safety": {
                "hard_stop": self.storage.hard_stop(),
                "applications_sent_today": self.storage.successful_today("application"),
                "application_daily_limit": self.config.safety.daily_application_limit,
                "messages_sent_today": self.storage.successful_today("chat_message"),
                "message_daily_limit": self.config.safety.daily_message_limit,
                "minimum_write_interval_seconds": self.config.safety.minimum_write_interval_seconds,
            },
        }

    def start_bridge(self) -> None:
        self.bridge.start()

    def stop_bridge(self) -> None:
        self.bridge.stop()

    def run_cycle(self, *, dry_run: bool | None = None) -> dict[str, Any]:
        self.start_bridge()
        return self.worker.run_cycle(dry_run=dry_run).to_dict()

    def recent_audit(self, *, limit: int = 20) -> dict[str, Any]:
        return {"items": self.storage.recent_audit(limit)}
