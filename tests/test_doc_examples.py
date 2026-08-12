"""Unit tests for `_validate_doc_examples` — group docs cannot drift from ops.

Group docs are hand-written; operation names are derived from the tool
function names. Only this check keeps an advertised example from naming an
operation the group does not expose.
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


def test_real_group_docs_pass_validation():
    groups = _real_groups()
    assert set(groups) == set(server._group_ops)
    for name, group in groups.items():
        server._validate_doc_examples(name, group.doc, server._group_ops[name])


def test_doc_example_validation_rejects_unknown_operation():
    with pytest.raises(RuntimeError, match="NoSuchOp"):
        server._validate_doc_examples(
            "gitea_read",
            "Example: gitea_read(operation='NoSuchOp', params={})",
            {"GetRepo": None},
        )

    server._validate_doc_examples("gitea_read", "operation='help'", {})
    server._validate_doc_examples("gitea_read", "operation='schema'", {})
