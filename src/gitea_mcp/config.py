"""Centralized configuration via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gitea_url: str = ""
    gitea_token: str = ""
    gitea_compact: bool = False
    gitea_require_brief: bool = True
    gitea_brief_max_length: int = 200
    gitea_force_private: bool = False


_settings: Settings | None = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def _reset_settings() -> None:
    """Force re-read from env. Used by tests."""
    global _settings
    _settings = None
