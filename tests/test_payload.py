"""Unit tests for payload building and the omitted-parameter dispatch path."""

from __future__ import annotations

from gitea_mcp.prepare import build_payload
from gitea_mcp.server import _build_params_model, _coerce_call


class TestBuildPayload:
    def test_drops_none(self):
        locals_dict = {"a": 1, "b": None, "c": 3}
        assert build_payload(locals_dict) == {"a": 1, "c": 3}

    def test_exclude(self):
        locals_dict = {"owner": "alice", "repo": "x", "title": "y"}
        out = build_payload(locals_dict, exclude=("owner", "repo"))
        assert out == {"title": "y"}

    def test_rename(self):
        locals_dict = {"status_types": ["open"], "page": 1}
        out = build_payload(locals_dict, rename={"status_types": "status-types"})
        assert out == {"status-types": ["open"], "page": 1}


class TestOmittedParams:
    """An omitted parameter is not passed to the tool function, so the
    function's own signature default applies."""

    def _wired(self, fn):
        """Attach the params model the same way `_register_tools` does, so
        `_coerce_call` can find it without going through full registration."""
        fn._params_model = _build_params_model(fn)
        return fn

    def test_omitted_uses_function_default(self):
        def fn(owner: str, milestone: int = 7):
            """Test."""
            return milestone

        self._wired(fn)
        assert _coerce_call(fn, {"owner": "alice"}, "Test") == 7

    def test_explicit_value_passes_through(self):
        def fn(owner: str, milestone: int = 7):
            """Test."""
            return milestone

        self._wired(fn)
        assert _coerce_call(fn, {"owner": "alice", "milestone": 42}, "Test") == 42
