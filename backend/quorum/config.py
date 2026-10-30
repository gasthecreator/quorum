"""Runtime settings, read from environment variables."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]


def _env_bool(name: str, default: bool = False) -> bool:
    v = os.environ.get(name)
    return default if v is None else v.strip().lower() in {"1", "true", "yes", "on"}


@dataclass
class Settings:
    host: str = "127.0.0.1"
    port: int = 8787
    cors_origins: tuple[str, ...] = ("http://127.0.0.1:5173", "http://localhost:5173")
    home: Path = field(default_factory=lambda: Path.home() / ".quorum")
    pack_dirs: tuple[Path, ...] = (REPO_ROOT / "packs", REPO_ROOT / "backend" / "dev_packs")
    models_yaml: Path = REPO_ROOT / "config" / "models.yaml"
    ollama_url: str = "http://127.0.0.1:11434"
    enable_fake: bool = False
    # Local models share one GPU; Apple Silicon laptops should not run many at once.
    parallelism: int = 1
    call_timeout_s: float = 180.0
    call_retries: int = 2
    max_output_tokens: int = 700
    temperature: float = 0.2
    default_weight: float = 0.5
    vote_threshold: float = 0.5
    line_tolerance: int = 2
    cluster_by_category: bool = True
    run_token_budget: int = 60_000
    run_time_budget_s: float = 900.0
    min_change_reason_chars: int = 12
    escalation_min_confidence: float = 0.6

    @property
    def db_path(self) -> Path:
        return self.home / "quorum.db"

    @classmethod
    def from_env(cls) -> Settings:
        s = cls()
        if v := os.environ.get("QUORUM_HOME"):
            s.home = Path(v).expanduser()
        if v := os.environ.get("QUORUM_PACK_DIRS"):
            s.pack_dirs = tuple(Path(p).expanduser() for p in v.split(os.pathsep) if p)
        if v := os.environ.get("QUORUM_MODELS_YAML"):
            s.models_yaml = Path(v).expanduser()
        if v := os.environ.get("OLLAMA_HOST") or os.environ.get("QUORUM_OLLAMA_URL"):
            s.ollama_url = v if v.startswith("http") else f"http://{v}"
        s.enable_fake = _env_bool("QUORUM_ENABLE_FAKE", s.enable_fake)
        s.cluster_by_category = _env_bool("QUORUM_CLUSTER_BY_CATEGORY", s.cluster_by_category)
        s.parallelism = int(os.environ.get("QUORUM_PARALLELISM", s.parallelism))
        s.run_token_budget = int(os.environ.get("QUORUM_TOKEN_BUDGET", s.run_token_budget))
        s.run_time_budget_s = float(os.environ.get("QUORUM_TIME_BUDGET_S", s.run_time_budget_s))
        return s
