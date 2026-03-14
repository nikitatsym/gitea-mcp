"""Unit tests for public repo/org enforcement."""

import pytest

from gitea_mcp.config import set_allow_public
from gitea_mcp.prepare import _enforce_private, _enforce_visibility


@pytest.fixture(autouse=True)
def _forbid_public():
    set_allow_public(False)
    yield
    set_allow_public(False)


class TestEnforcePrivate:
    def test_block_public_repo(self):
        with pytest.raises(ValueError, match="Public repos not allowed"):
            _enforce_private(False)

    def test_block_when_not_specified(self):
        with pytest.raises(ValueError, match="Public repos not allowed"):
            _enforce_private(None)

    def test_explicit_private_passes(self):
        assert _enforce_private(True) is True


class TestEnforceVisibility:
    def test_block_public_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("public")

    def test_block_limited_org(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility("limited")

    def test_block_when_not_specified(self):
        with pytest.raises(ValueError, match="Public orgs not allowed"):
            _enforce_visibility(None)

    def test_explicit_private_passes(self):
        assert _enforce_visibility("private") == "private"


class TestAllowPublic:
    def test_public_repo_allowed(self):
        set_allow_public(True)
        assert _enforce_private(False) is False

    def test_none_allowed(self):
        set_allow_public(True)
        assert _enforce_private(None) is None

    def test_public_org_allowed(self):
        set_allow_public(True)
        assert _enforce_visibility("public") == "public"
