"""Test omission and nullable-clear semantics through payloads and params models."""

from __future__ import annotations

import json

from gitea_mcp.prepare import build_payload
from gitea_mcp.registry import UNSET, Unset
from gitea_mcp.server import _build_params_model, _coerce_call


class TestUnsetSingleton:
    def test_singleton_identity(self):
        assert Unset() is UNSET
        assert Unset() is Unset()


class TestBodyDropping:
    """Default behaviour: `build_payload` drops `UNSET` always, drops `None` unless
    the field name is listed in `keep_null=`."""

    def test_drops_unset(self):
        locals_dict = {"a": 1, "b": UNSET, "c": 3}
        assert build_payload(locals_dict) == {"a": 1, "c": 3}

    def test_drops_none_by_default(self):
        """None-valued optionals stay off the wire without keep_null."""
        locals_dict = {"a": 1, "b": None, "c": 3}
        assert build_payload(locals_dict) == {"a": 1, "c": 3}

    def test_keeps_none_for_listed_fields(self):
        """nullable-clear opt-in: when the caller passes None for a field
        listed in keep_null, it surfaces as JSON null on the wire."""
        locals_dict = {"a": 1, "assignee": None, "milestone": None}
        out = build_payload(locals_dict, keep_null=("assignee",))
        assert out == {"a": 1, "assignee": None}

    def test_keep_null_still_drops_unset(self):
        """keep_null only changes None handling -- `UNSET` is still dropped
        because it means 'caller omitted'."""
        locals_dict = {"assignee": UNSET, "milestone": None}
        out = build_payload(locals_dict, keep_null=("assignee", "milestone"))
        assert out == {"milestone": None}

    def test_exclude_still_works(self):
        locals_dict = {"owner": "alice", "repo": "x", "title": "y"}
        out = build_payload(locals_dict, exclude=("owner", "repo"))
        assert out == {"title": "y"}

    def test_rename(self):
        locals_dict = {"status_types": ["open"], "page": 1}
        out = build_payload(locals_dict, rename={"status_types": "status-types"})
        assert out == {"status-types": ["open"], "page": 1}

    def test_keep_null_combined_with_rename(self):
        """rename and keep_null must compose — the listed name is the
        Python name (pre-rename), so callers don't have to know the wire
        spelling to opt a field in."""
        locals_dict = {"status_types": None, "page": 1}
        out = build_payload(
            locals_dict,
            rename={"status_types": "status-types"},
            keep_null=("status_types",),
        )
        assert out == {"status-types": None, "page": 1}


class TestUnsetThroughParamsModel:
    """End-to-end: a Pydantic model built from a `default = UNSET` signature
    preserves omission through `model_dump(exclude_unset=True)`."""

    def _wired(self, fn):
        """Attach the params model the same way `_register_tools` does, so
        `_coerce_call` can find it without going through full registration."""
        fn._params_model = _build_params_model(fn)
        return fn

    def test_omitted_means_unset_at_call_site(self):
        def fn(owner: str, milestone: int = UNSET):
            """Test."""
            return milestone

        self._wired(fn)
        assert _coerce_call(fn, {"owner": "alice"}, "Test") is UNSET

    def test_explicit_value_passes_through(self):
        def fn(owner: str, milestone: int = UNSET):
            """Test."""
            return milestone

        self._wired(fn)
        assert _coerce_call(fn, {"owner": "alice", "milestone": 42}, "Test") == 42

    def test_help_does_not_leak_pydantic_undefined(self):
        """Closed-loop check: build the model, dump JSON Schema, ensure the
        `UNSET`-defaulted field doesn't carry `default: 'PydanticUndefined'`
        through to the schema."""
        def fn(owner: str, milestone: int = UNSET):
            """Test."""

        model = _build_params_model(fn)
        schema = model.model_json_schema()
        # milestone is optional → not in `required`
        assert "milestone" not in schema.get("required", [])
        # And — critically — no PydanticUndefined sentinel anywhere.
        dumped = json.dumps(schema, default=str)
        assert "PydanticUndefined" not in dumped
