"""Unit tests for GITEA_FORBID_PUBLIC enforcement."""

import pytest

from gitea_mcp.config import _reset_settings
from gitea_mcp.server import _enforce_private, _enforce_visibility


@pytest.fixture(autouse=True)
def _forbid_public_on(monkeypatch):
    monkeypatch.setenv("GITEA_FORBID_PUBLIC", "true")
    _reset_settings()
    yield
    _reset_settings()


class TestEnforcePrivate:
    def test_block_public_repo(self):
        with pytest.raises(ValueError, match="Public repos not allowed"):
            _enforce_private(False)

    def test_none_passthrough(self):
        assert _enforce_private(None) is None

    def test_explicit_private_passes(self):
        assert _enforce_private(True) is True


class TestEnforceVisibility:
    def test_block_public_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("public")

    def test_block_limited_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("limited")

    def test_none_passthrough(self):
        assert _enforce_visibility(None) is None

    def test_explicit_private_passes(self):
        assert _enforce_visibility("private") == "private"


class TestForbidPublicOff:
    def test_public_repo_allowed(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORBID_PUBLIC", "false")
        _reset_settings()
        assert _enforce_private(False) is False

    def test_none_stays_none(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORBID_PUBLIC", "false")
        _reset_settings()
        assert _enforce_private(None) is None

    def test_public_org_allowed(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORBID_PUBLIC", "0")
        _reset_settings()
        assert _enforce_visibility("public") == "public"
