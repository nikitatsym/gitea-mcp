"""Unit tests for GITEA_FORCE_PRIVATE enforcement."""

import pytest

from gitea_mcp.config import _reset_settings
from gitea_mcp.server import _enforce_private, _enforce_visibility


@pytest.fixture(autouse=True)
def _force_private_on(monkeypatch):
    monkeypatch.setenv("GITEA_FORCE_PRIVATE", "true")
    _reset_settings()
    yield
    _reset_settings()


class TestEnforcePrivate:
    def test_block_public_repo(self):
        with pytest.raises(ValueError, match="Public repos not allowed"):
            _enforce_private(False, is_create=True)

    def test_block_public_repo_edit(self):
        with pytest.raises(ValueError, match="Public repos not allowed"):
            _enforce_private(False)

    def test_default_to_private_on_create(self):
        assert _enforce_private(None, is_create=True) is True

    def test_none_passthrough_on_edit(self):
        assert _enforce_private(None) is None

    def test_explicit_private_passes(self):
        assert _enforce_private(True, is_create=True) is True
        assert _enforce_private(True) is True


class TestEnforceVisibility:
    def test_block_public_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("public", is_create=True)

    def test_block_limited_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("limited")

    def test_default_to_private_on_create(self):
        assert _enforce_visibility(None, is_create=True) == "private"

    def test_none_passthrough_on_edit(self):
        assert _enforce_visibility(None) is None

    def test_explicit_private_passes(self):
        assert _enforce_visibility("private", is_create=True) == "private"
        assert _enforce_visibility("private") == "private"


class TestForcePrivateOff:
    def test_public_repo_allowed(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORCE_PRIVATE", "false")
        _reset_settings()
        assert _enforce_private(False, is_create=True) is False

    def test_none_stays_none(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORCE_PRIVATE", "false")
        _reset_settings()
        assert _enforce_private(None, is_create=True) is None

    def test_public_org_allowed(self, monkeypatch):
        monkeypatch.setenv("GITEA_FORCE_PRIVATE", "0")
        _reset_settings()
        assert _enforce_visibility("public", is_create=True) == "public"
