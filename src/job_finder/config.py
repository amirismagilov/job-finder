from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import tomllib


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
    daily_application_limit: int
    daily_message_limit: int
    minimum_read_interval_seconds: float
    minimum_write_interval_seconds: float
    confirmation_ttl_minutes: int


@dataclass(frozen=True)
class HHConfig:
    base_url: str
    user_agent: str


@dataclass(frozen=True)
class Config:
    root: Path
    profile: ProfileConfig
    search: SearchConfig
    safety: SafetyConfig
    hh: HHConfig

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


def load_config(path: Path | None = None) -> Config:
    root = project_root()
    config_path = path or Path(os.environ.get("JOB_FINDER_CONFIG", root / "config.toml"))
    if not config_path.exists():
        raise RuntimeError(
            f"Не найден локальный конфиг {config_path}. "
            "Скопируйте config.example.toml в config.toml и укажите пути к базе знаний."
        )
    with config_path.open("rb") as fh:
        raw = tomllib.load(fh)

    profile = raw.get("profile", {})
    search = raw.get("search", {})
    safety = raw.get("safety", {})
    hh = raw.get("hh", {})
    base_url = str(hh.get("base_url", "https://api.hh.ru")).rstrip("/")
    if base_url != "https://api.hh.ru":
        raise RuntimeError("В целях безопасности разрешён только официальный API https://api.hh.ru")

    cfg = Config(
        root=root,
        profile=ProfileConfig(
            knowledge_paths=tuple(Path(p).expanduser() for p in profile.get("knowledge_paths", [])),
            cover_letter_rules_path=Path(profile.get("cover_letter_rules_path", "")).expanduser(),
        ),
        search=SearchConfig(
            area=str(search.get("area", "1")),
            days=max(1, min(int(search.get("days", 14)), 30)),
            per_query=max(1, min(int(search.get("per_query", 10)), 20)),
            queries=tuple(str(q).strip() for q in search.get("queries", []) if str(q).strip()),
        ),
        safety=SafetyConfig(
            daily_application_limit=max(1, int(safety.get("daily_application_limit", 10))),
            daily_message_limit=max(1, int(safety.get("daily_message_limit", 30))),
            minimum_read_interval_seconds=max(0.5, float(safety.get("minimum_read_interval_seconds", 0.8))),
            minimum_write_interval_seconds=max(5.0, float(safety.get("minimum_write_interval_seconds", 10.0))),
            confirmation_ttl_minutes=max(5, min(int(safety.get("confirmation_ttl_minutes", 15)), 60)),
        ),
        hh=HHConfig(
            base_url=base_url,
            user_agent=str(hh.get("user_agent", "amir-job-finder/0.1 (personal local assistant)")),
        ),
    )
    if not cfg.search.queries:
        raise RuntimeError("В config.toml должен быть хотя бы один поисковый запрос")
    return cfg
