# region MODULE_CONTRACT [DOMAIN(9): JobAutomation, Configuration; CONCEPT(9): SafeDefaults, Allowlist; TECH(8): TOML, Dataclasses]
## @file config.py
## @brief Validated local configuration without credentials.
## @modulecontract
## @purpose Centralize conservative autonomy, bridge, search and LLM settings while rejecting unsafe provider configuration.
## @scope Local TOML parsing and immutable runtime settings.
## @input config.toml and JOB_FINDER_ROOT/JOB_FINDER_CONFIG paths.
## @output Config value object.
## @invariants No secret is read from TOML; bridge is fixed to 127.0.0.1:8766; HTTP LLM uses safe transport; Codex CLI uses an absolute executable and allowlisted settings.
## @changes LAST_CHANGE: [v0.3.0 — Added validated HTTP/Codex CLI provider selection and bounded provider-specific timeouts.]
## @modulemap
## CLASS 10[Complete validated runtime settings] => Config
## FUNC 9[Validates explicit LLM provider selection] => _validate_llm_provider
## FUNC 9[Validates absolute Codex CLI executable] => _validate_cli_executable
## FUNC 8[Validates Codex CLI model token] => _validate_cli_model
## FUNC 9[Validates optional provider reasoning capability] => _validate_reasoning_effort
## FUNC 9[Loads and validates local TOML] => load_config
def _module_contract() -> None:
    pass
# endregion MODULE_CONTRACT
# GREP_SUMMARY: config, TOML, bridge, autonomy, LLM provider, Codex CLI, safety limits, allowlist
# STRUCTURE: TOML -> typed sections -> safety clamps -> provider-specific validation -> immutable Config

from dataclasses import dataclass, field
from pathlib import Path
import os
import tomllib
from urllib.parse import urlparse


@dataclass(frozen=True)
class ProfileConfig:
    knowledge_paths: tuple[Path, ...]
    cover_letter_rules_path: Path


@dataclass(frozen=True)
class SearchConfig:
    area: str
    days: int
    per_query: int
    queries: tuple[str, ...]


@dataclass(frozen=True)
class SafetyConfig:
    daily_application_limit: int = 5
    daily_message_limit: int = 20
    minimum_read_interval_seconds: float = 1.0
    minimum_write_interval_seconds: float = 30.0
    confirmation_ttl_minutes: int = 15


@dataclass(frozen=True)
class HHConfig:
    base_url: str = "https://hh.ru"
    user_agent: str = "amir-job-finder/0.2 (local browser bridge)"
    chat_base_url: str = "https://chatik.hh.ru"


@dataclass(frozen=True)
class BridgeConfig:
    host: str = "127.0.0.1"
    port: int = 8766
    command_timeout_seconds: float = 45.0
    max_request_bytes: int = 1_100_000
    max_response_bytes: int = 1_000_000


@dataclass(frozen=True)
class LLMConfig:
    endpoint: str = "http://127.0.0.1:11434/v1/chat/completions"
    model: str = "local-model"
    api_key_account: str = "llm_api_key"
    timeout_seconds: float = 60.0
    minimum_confidence: float = 0.75
    reasoning_effort: str | None = None
    provider: str = "http"
    codex_executable: Path | None = None


@dataclass(frozen=True)
class AutonomyConfig:
    relevance_threshold: int = 75
    max_vacancies_per_cycle: int = 12
    chat_history_limit: int = 30
    cycle_interval_seconds: int = 300


@dataclass(frozen=True)
class Config:
    root: Path
    profile: ProfileConfig
    search: SearchConfig
    safety: SafetyConfig
    hh: HHConfig
    bridge: BridgeConfig = field(default_factory=BridgeConfig)
    llm: LLMConfig = field(default_factory=LLMConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)

    @property
    def state_dir(self) -> Path:
        return self.root / "state"

    @property
    def database_path(self) -> Path:
        return self.state_dir / "job-finder.sqlite3"


def project_root() -> Path:
    configured = os.environ.get("JOB_FINDER_ROOT")
    if configured:
        return Path(configured).expanduser().resolve()
    return Path(__file__).resolve().parents[2]


# region FUNC_validate_llm_endpoint [DOMAIN(8): Security; CONCEPT(9): NetworkBoundary; TECH(8): URLParsing]
## @purpose Prevent plaintext remote AI traffic and credential-bearing endpoint URLs.
## @io str -> str
## @complexity 4
def _validate_llm_endpoint(value: str) -> str:
    parsed = urlparse(value)
    if parsed.username or parsed.password or not parsed.hostname:
        raise RuntimeError("Некорректный LLM endpoint")
    loopback = parsed.hostname in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme != "https" and not (loopback and parsed.scheme == "http"):
        raise RuntimeError("Удалённый LLM endpoint обязан использовать HTTPS; HTTP разрешён только на loopback")
    return value.rstrip("/")
# endregion FUNC_validate_llm_endpoint


LLM_PROVIDERS = frozenset({"http", "codex_cli"})
REASONING_EFFORTS = frozenset({"none", "low", "medium", "high", "xhigh", "max"})
CODEX_MODEL_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-._")


# region FUNC_validate_llm_provider [DOMAIN(8): Configuration; CONCEPT(10): ExplicitTransport; TECH(8): Allowlist]
## @purpose Prevent an unrecognized transport from inheriting HTTP or subprocess privileges.
## @io Any -> http|codex_cli
## @complexity 2
def _validate_llm_provider(value: object) -> str:
    provider = str(value).strip().lower()
    if provider not in LLM_PROVIDERS:
        raise RuntimeError("LLM provider должен быть http или codex_cli")
    return provider
# endregion FUNC_validate_llm_provider


# region FUNC_validate_reasoning_effort [DOMAIN(8): Configuration; CONCEPT(9): ProviderCapability; TECH(8): Allowlist]
## @purpose Accept only known Chat Completions reasoning effort values while preserving omission for local providers.
## @io Any|None -> str|None
## @complexity 3
def _validate_reasoning_effort(value: object) -> str | None:
    if value is None:
        return None
    effort = str(value).strip().lower()
    if effort not in REASONING_EFFORTS:
        allowed = ", ".join(sorted(REASONING_EFFORTS))
        raise RuntimeError(f"Некорректный reasoning_effort; разрешены: {allowed}")
    return effort
# endregion FUNC_validate_reasoning_effort


# region FUNC_validate_cli_executable [DOMAIN(9): Security; CONCEPT(10): FixedExecutable; TECH(8): FilesystemValidation]
## @purpose Ensure Codex CLI is selected by one explicit executable path rather than PATH lookup or shell parsing.
## @io provider, Any|None -> Path|None
## @complexity 4
def _validate_cli_executable(provider: str, value: object | None) -> Path | None:
    if provider != "codex_cli":
        return None
    if value is None:
        raise RuntimeError("Для provider=codex_cli требуется codex_executable")
    executable = Path(str(value))
    if not executable.is_absolute() or not executable.is_file() or not os.access(executable, os.X_OK):
        raise RuntimeError("codex_executable должен быть абсолютным путём к исполняемому файлу")
    return executable
# endregion FUNC_validate_cli_executable


# region FUNC_validate_cli_model [DOMAIN(8): Security; CONCEPT(9): FixedArgvValue; TECH(8): CharacterAllowlist]
## @purpose Keep the model argv value bounded and free of option/control characters in Codex CLI mode.
## @io provider, Any -> str
## @complexity 3
def _validate_cli_model(provider: str, value: object) -> str:
    model = str(value).strip()
    if provider == "codex_cli" and (not model or len(model) > 128 or any(char not in CODEX_MODEL_CHARS for char in model)):
        raise RuntimeError("Некорректная модель для provider=codex_cli")
    return model
# endregion FUNC_validate_cli_model


# region FUNC_load_config [DOMAIN(9): JobAutomation; CONCEPT(9): SafeDefaults; TECH(8): TOML]
## @purpose Build one validated configuration whose defaults cannot silently enable unsafe writes.
## @io Path|None -> Config
## @complexity 6
def load_config(path: Path | None = None) -> Config:
    root = project_root()
    config_path = path or Path(os.environ.get("JOB_FINDER_CONFIG", root / "config.toml"))
    if not config_path.exists():
        raise RuntimeError(f"Не найден локальный конфиг {config_path}. Скопируйте config.example.toml в config.toml.")
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)
    profile, search = raw.get("profile", {}), raw.get("search", {})
    safety, bridge = raw.get("safety", {}), raw.get("bridge", {})
    llm, autonomy, hh = raw.get("llm", {}), raw.get("autonomy", {}), raw.get("hh", {})
    llm_provider = _validate_llm_provider(llm.get("provider", "http"))
    reasoning_effort = _validate_reasoning_effort(llm.get("reasoning_effort"))
    if llm_provider == "codex_cli" and reasoning_effort is None:
        raise RuntimeError("Для provider=codex_cli требуется reasoning_effort")
    llm_timeout = float(llm.get("timeout_seconds", 300 if llm_provider == "codex_cli" else 60))
    bridge_host, bridge_port = str(bridge.get("host", "127.0.0.1")), int(bridge.get("port", 8766))
    if bridge_host != "127.0.0.1" or bridge_port != 8766:
        raise RuntimeError("Bridge разрешён только на 127.0.0.1:8766")
    hh_base = str(hh.get("base_url", "https://hh.ru")).rstrip("/")
    chat_base = str(hh.get("chat_base_url", "https://chatik.hh.ru")).rstrip("/")
    if hh_base != "https://hh.ru" or chat_base != "https://chatik.hh.ru":
        raise RuntimeError("Разрешены только фиксированные web-origin hh.ru и chatik.hh.ru")
    cfg = Config(
        root=root,
        profile=ProfileConfig(tuple(Path(p).expanduser() for p in profile.get("knowledge_paths", [])), Path(profile.get("cover_letter_rules_path", "")).expanduser()),
        search=SearchConfig(str(search.get("area", "1")), max(1, min(int(search.get("days", 14)), 30)), max(1, min(int(search.get("per_query", 10)), 20)), tuple(str(q).strip() for q in search.get("queries", []) if str(q).strip())),
        safety=SafetyConfig(max(1, min(int(safety.get("daily_application_limit", 5)), 20)), max(1, min(int(safety.get("daily_message_limit", 20)), 100)), max(0.5, float(safety.get("minimum_read_interval_seconds", 1.0))), max(30.0, float(safety.get("minimum_write_interval_seconds", 30.0)))),
        hh=HHConfig(hh_base, str(hh.get("user_agent", "amir-job-finder/0.2 (local browser bridge)")), chat_base),
        bridge=BridgeConfig(bridge_host, bridge_port, max(35.0, min(float(bridge.get("command_timeout_seconds", 45)), 120.0)), max(1024, min(int(bridge.get("max_request_bytes", 1_100_000)), 2_000_000)), max(1024, min(int(bridge.get("max_response_bytes", 1_000_000)), 1_000_000))),
        llm=LLMConfig(
            endpoint=_validate_llm_endpoint(str(llm.get("endpoint", "http://127.0.0.1:11434/v1/chat/completions"))) if llm_provider == "http" else "",
            model=_validate_cli_model(llm_provider, llm.get("model", "local-model")),
            api_key_account="llm_api_key",
            timeout_seconds=max(30.0, min(llm_timeout, 600.0)) if llm_provider == "codex_cli" else max(5.0, min(llm_timeout, 180.0)),
            minimum_confidence=max(0.5, min(float(llm.get("minimum_confidence", 0.75)), 1.0)),
            reasoning_effort=reasoning_effort,
            provider=llm_provider,
            codex_executable=_validate_cli_executable(llm_provider, llm.get("codex_executable")),
        ),
        autonomy=AutonomyConfig(max(0, min(int(autonomy.get("relevance_threshold", 75)), 100)), max(1, min(int(autonomy.get("max_vacancies_per_cycle", 12)), 50)), max(5, min(int(autonomy.get("chat_history_limit", 30)), 50)), max(60, int(autonomy.get("cycle_interval_seconds", 300)))),
    )
    if not cfg.search.queries:
        raise RuntimeError("В config.toml должен быть хотя бы один поисковый запрос")
    return cfg
# endregion FUNC_load_config
