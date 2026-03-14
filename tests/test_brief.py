"""Unit tests for <brief> validation."""

import importlib

import pytest

import gitea_mcp.prepare as _prepare_mod
from gitea_mcp.prepare import _validate_brief


@pytest.fixture(autouse=True)
def _require_brief_on(monkeypatch):
    """Ensure brief validation is on for all tests (unless overridden)."""
    monkeypatch.setenv("MCP_GITEA_BRIEF_MAX", "200")
    importlib.reload(_prepare_mod)
    yield
    monkeypatch.delenv("MCP_GITEA_BRIEF_MAX", raising=False)
    importlib.reload(_prepare_mod)


class TestValidateBrief:
    def test_valid_brief(self):
        _prepare_mod._validate_brief("<brief>Short summary</brief>\n\nFull body.")

    def test_missing_brief_raises(self):
        with pytest.raises(ValueError, match="must contain"):
            _prepare_mod._validate_brief("Body without brief tag")

    def test_none_body_raises(self):
        with pytest.raises(ValueError, match="must contain"):
            _prepare_mod._validate_brief(None)

    def test_empty_body_raises(self):
        with pytest.raises(ValueError, match="must contain"):
            _prepare_mod._validate_brief("")

    def test_brief_too_long(self):
        with pytest.raises(ValueError, match="too long"):
            _prepare_mod._validate_brief(f"<brief>{'x' * 201}</brief>")

    def test_brief_at_max_length(self):
        _prepare_mod._validate_brief(f"<brief>{'x' * 200}</brief>")

    def test_brief_multiline(self):
        _prepare_mod._validate_brief("<brief>Line one\nline two</brief>\n\nBody.")


class TestValidateBriefDisabled:
    def test_no_brief_passes_when_disabled(self, monkeypatch):
        monkeypatch.setenv("MCP_GITEA_BRIEF_MAX", "0")
        importlib.reload(_prepare_mod)
        _prepare_mod._validate_brief("No brief here, no problem")

    def test_none_passes_when_disabled(self, monkeypatch):
        monkeypatch.setenv("MCP_GITEA_BRIEF_MAX", "0")
        importlib.reload(_prepare_mod)
        _prepare_mod._validate_brief(None)


class TestCustomMaxLength:
    def test_custom_max_length(self, monkeypatch):
        monkeypatch.setenv("MCP_GITEA_BRIEF_MAX", "50")
        importlib.reload(_prepare_mod)
        _prepare_mod._validate_brief(f"<brief>{'x' * 50}</brief>")
        with pytest.raises(ValueError, match="too long"):
            _prepare_mod._validate_brief(f"<brief>{'x' * 51}</brief>")
