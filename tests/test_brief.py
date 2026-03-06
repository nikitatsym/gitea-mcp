"""Unit tests for <brief> validation."""

import os
import pytest


@pytest.fixture(autouse=True)
def _require_brief_on(monkeypatch):
    """Ensure brief validation is on for all tests (unless overridden)."""
    monkeypatch.setenv("GITEA_REQUIRE_BRIEF", "true")
    monkeypatch.setenv("GITEA_BRIEF_MAX_LENGTH", "200")
    # Re-import to pick up env changes
    import importlib
    import gitea_mcp.server as srv
    importlib.reload(srv)
    yield srv


def _reload_srv(monkeypatch, **env):
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import importlib
    import gitea_mcp.server as srv
    importlib.reload(srv)
    return srv


class TestValidateBrief:
    def test_valid_brief(self, _require_brief_on):
        srv = _require_brief_on
        srv._validate_brief("<brief>Short summary</brief>\n\nFull body.")

    def test_missing_brief_raises(self, _require_brief_on):
        srv = _require_brief_on
        with pytest.raises(ValueError, match="must contain"):
            srv._validate_brief("Body without brief tag")

    def test_none_body_raises(self, _require_brief_on):
        srv = _require_brief_on
        with pytest.raises(ValueError, match="must contain"):
            srv._validate_brief(None)

    def test_empty_body_raises(self, _require_brief_on):
        srv = _require_brief_on
        with pytest.raises(ValueError, match="must contain"):
            srv._validate_brief("")

    def test_brief_too_long(self, _require_brief_on):
        srv = _require_brief_on
        with pytest.raises(ValueError, match="too long"):
            srv._validate_brief(f"<brief>{'x' * 201}</brief>")

    def test_brief_at_max_length(self, _require_brief_on):
        srv = _require_brief_on
        srv._validate_brief(f"<brief>{'x' * 200}</brief>")

    def test_brief_multiline(self, _require_brief_on):
        srv = _require_brief_on
        srv._validate_brief("<brief>Line one\nline two</brief>\n\nBody.")


class TestValidateBriefDisabled:
    def test_no_brief_passes_when_disabled(self, monkeypatch):
        srv = _reload_srv(monkeypatch, GITEA_REQUIRE_BRIEF="false")
        srv._validate_brief("No brief here, no problem")

    def test_none_passes_when_disabled(self, monkeypatch):
        srv = _reload_srv(monkeypatch, GITEA_REQUIRE_BRIEF="no")
        srv._validate_brief(None)


class TestCustomMaxLength:
    def test_custom_max_length(self, monkeypatch):
        srv = _reload_srv(monkeypatch, GITEA_REQUIRE_BRIEF="true", GITEA_BRIEF_MAX_LENGTH="50")
        srv._validate_brief(f"<brief>{'x' * 50}</brief>")
        with pytest.raises(ValueError, match="too long"):
            srv._validate_brief(f"<brief>{'x' * 51}</brief>")
