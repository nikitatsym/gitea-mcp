"""Unit tests for `_render_group_doc` — group docs cannot drift from ops.

Group docs are hand-written; operation names are derived from the tool
function names. Rendering the names from the registry keeps an advertised
example from naming an operation the group does not expose.
"""

from __future__ import annotations

import inspect

import pytest

from gitea_mcp import server, tools
from gitea_mcp.registry import Group


def _real_groups() -> dict[str, Group]:
    """Registered groups by name. `gitea_create`/`gitea_update` alias
    `gitea_write`, so key by name to collapse them."""
    return {
        obj.name: obj
        for _, obj in inspect.getmembers(tools, lambda o: isinstance(o, Group))
        if obj.name in server._group_ops
    }


def test_real_group_docs_render_without_leftover_placeholders():
    groups = _real_groups()
    assert set(groups) == set(server._group_ops)
    for name, group in groups.items():
        rendered = server._render_group_doc(name, group.doc, server._group_ops[name])
        assert "$" not in rendered, f"{name} doc left a placeholder unrendered"


def test_render_group_doc_rejects_unknown_placeholder():
    with pytest.raises(RuntimeError, match="NoSuchOp"):
        server._render_group_doc(
            "gitea_read",
            "Example: gitea_read(operation='$NoSuchOp', params={})",
            {"GetRepo": None},
        )


def test_render_group_doc_rejects_hardcoded_operation():
    with pytest.raises(RuntimeError, match="hardcodes"):
        server._render_group_doc(
            "gitea_read",
            "Example: gitea_read(operation='GetRepo', params={})",
            {"GetRepo": None},
        )

    with pytest.raises(RuntimeError, match="hardcodes"):
        server._render_group_doc(
            "gitea_read",
            "Example: gitea_read(operation = 'GetRepo', params={})",
            {"GetRepo": None},
        )


def test_render_group_doc_resolves_meta_and_keeps_generic_form():
    rendered = server._render_group_doc(
        "gitea_read", "operation='$help' or operation='$schema' or operation='<OpName>'", {}
    )
    assert rendered == "operation='help' or operation='schema' or operation='<OpName>'"
