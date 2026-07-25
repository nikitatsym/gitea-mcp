"""Unit tests for `_build_params_model` — Pydantic params model construction.

Focused tests for the validator path. The server-side dispatch and live
fixtures live in `test_integration.py`; these run with no docker, no env.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from gitea_mcp.server import _build_params_model


class TestStringBoolCoercion:
    """Spec requirement: MCP clients sometimes pass `"true"`/`"false"` as
    JSON strings rather than booleans. `_build_params_model` coerces those
    via a field validator so callers don't have to know Pydantic's strict
    bool rules."""

    def _model(self, fn):
        return _build_params_model(fn)

    def test_string_true_coerced(self):
        def fn(private: bool):
            """Test."""
            return private

        model = self._model(fn)
        assert model.model_validate({"private": "true"}).private is True
        assert model.model_validate({"private": "True"}).private is True
        assert model.model_validate({"private": "yes"}).private is True
        assert model.model_validate({"private": "1"}).private is True

    def test_string_false_coerced(self):
        def fn(private: bool):
            """Test."""
            return private

        model = self._model(fn)
        assert model.model_validate({"private": "false"}).private is False
        assert model.model_validate({"private": "False"}).private is False
        assert model.model_validate({"private": "no"}).private is False
        assert model.model_validate({"private": "0"}).private is False

    def test_bool_passthrough(self):
        """Real booleans still work unchanged."""
        def fn(private: bool):
            """Test."""
            return private

        model = self._model(fn)
        assert model.model_validate({"private": True}).private is True
        assert model.model_validate({"private": False}).private is False

    def test_string_in_non_bool_field_not_coerced(self):
        """Coercion targets bool-typed fields only; a `"true"` string passed
        to a `str`-typed param survives intact."""
        def fn(label: str):
            """Test."""
            return label

        model = self._model(fn)
        assert model.model_validate({"label": "true"}).label == "true"

    def test_optional_bool_coerced(self):
        """Optional[bool] (i.e. `bool | None`) is still picked up — the
        validator walks the Union args."""
        def fn(private: bool | None = None):
            """Test."""
            return private

        model = self._model(fn)
        assert model.model_validate({"private": "true"}).private is True
        assert model.model_validate({"private": "false"}).private is False
        # None still allowed.
        assert model.model_validate({"private": None}).private is None
        # Omitted altogether.
        assert model.model_validate({}).private is None

    def test_unknown_string_left_alone(self):
        """Strings that don't match the recognised vocabulary aren't coerced,
        so Pydantic's downstream validation can surface the type error
        with a clean field-level message."""
        def fn(private: bool):
            """Test."""
            return private

        model = self._model(fn)
        with pytest.raises(ValidationError):
            model.model_validate({"private": "maybe"})


class TestExtraForbid:
    """Sanity check: existing behaviour preserved — unknown keys still fail."""

    def test_unknown_key_rejected(self):
        def fn(a: int):
            """Test."""
            return a

        model = _build_params_model(fn)
        with pytest.raises(ValidationError):
            model.model_validate({"a": 1, "z": 99})
