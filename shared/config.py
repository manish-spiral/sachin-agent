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

    # Matcher (Phase 5)
    scoring_model: str = "gpt-4o-mini"


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