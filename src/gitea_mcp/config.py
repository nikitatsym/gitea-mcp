"""Centralized configuration via pydantic-settings."""

from __future__ import annotations

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    gitea_url: str = ""
    gitea_token: str = ""
    gitea_compact: bool = False
    gitea_require_brief: bool = True
    gitea_brief_max_length: int = 200


_settings: Settings | None = None

# Not an env var — only controllable via --allow-public CLI flag.
_allow_public: bool = False


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings


def allow_public() -> bool:
    return _allow_public


def set_allow_public(value: bool) -> None:
    global _allow_public
    _allow_public = value


def _reset_settings() -> None:
    """Force re-read from env. Used by tests."""
    global _settings
    _settings = None
