"""Central settings loaded from environment / .env."""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # Core
    database_url: str = "sqlite:///data/sachin-agent.db"
    log_level: str = "INFO"

    # Directories
    data_dir: Path = Path("data")
    snapshot_dir: Path = Path("snapshots")
    browser_profile_dir: Path = Path("browser_profile")
    logs_dir: Path = Path("logs")
    profile_data_dir: Path = Path("profile-data")

    # Placeholders for later phases (kept Optional so surface is stable)
    openai_api_key: str | None = None
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    # LLM: provider "openai" | "groq"; unset = whichever key is present.
    # Models unset = provider defaults (see shared/llm.py).
    groq_api_key: str | None = None
    llm_provider: str | None = None
    extraction_model: str | None = None
    scoring_model: str | None = None
    # Per-task model overrides as JSON, e.g. LLM_MODELS='{"resume": "openai/gpt-oss-20b"}'.
    # Tasks: extraction, scoring, keywords, resume, cover_letter, verify, form_answer.
    llm_models: dict[str, str] = {}

    # Orchestration (Phase 9)
    # Kill switch: KILL_SWITCH env var or a data_dir/KILL file (shared/killswitch.py).
    max_applications_per_day: int = 5
    # Unset: only jobs a human approved are applied to. Set: scored jobs at or
    # above this score are approved automatically (submission still pauses).
    auto_approve_min_score: float | None = None
    exclude_companies: list[str] = []
    exclude_title_keywords: list[str] = []
    score_llm_calls_per_run: int = 60
    tailor_per_run: int = 10

    # Snapshots (Phase 10)
    capture_snapshots: bool = True
    snapshot_retention_days: int = 30


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def reset_settings() -> None:
    """Testing hook — clears the cache so env changes take effect."""
    global _settings
    _settings = None