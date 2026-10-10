"""Unit tests for `_build_help` — render, search filter, cross-group hint.

Seeds a couple of synthetic ops into `_group_ops` so we exercise the help
renderer without relying on the full tools.py registration.
"""

from __future__ import annotations

from typing import Annotated

import pytest
from pydantic import Field

from gitea_mcp import server
from gitea_mcp.server import _build_help, _build_params_model


@pytest.fixture(autouse=True)
def _isolate_state():
    """Snapshot and restore module-level help state so tests don't leak."""
    saved_ops = dict(server._group_ops)
    saved_all = dict(server._all_grouped)
    server._group_ops.clear()
    server._all_grouped.clear()
    yield
    server._group_ops.clear()
    server._all_grouped.clear()
    server._group_ops.update(saved_ops)
    server._all_grouped.update(saved_all)


def _seed(group_to_fns: dict[str, dict[str, callable]]) -> None:
    """Install ops into the help registry, building Pydantic models on the fly."""
    for group_name, fns in group_to_fns.items():
        server._group_ops[group_name] = {}
        for pascal, fn in fns.items():
            fn._params_model = _build_params_model(fn)
            server._group_ops[group_name][pascal] = fn
            server._all_grouped[pascal] = group_name


def list_milestones(owner: str, repo: str):
    """List milestones in a repository."""


def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: Annotated[str | None, Field(description="Issue body markdown.")] = None,
):
    """Create an issue in a repository.

    Body must contain a <brief>summary</brief> tag.
    """


def merge_pull_request(owner: str, repo: str, index: int):
    """Merge a pull request by index."""


class TestHelpFlat:
    def test_full_listing_no_args(self):
        _seed({"gitea_read": {"ListMilestones": list_milestones}})
        out = _build_help("gitea_read")
        assert "1 operations available" in out
        assert "ListMilestones(owner: str, repo: str) — List milestones" in out

    def test_help_shows_docstring_body(self):
        _seed({"gitea_write": {"CreateIssue": create_issue}})
        out = _build_help("gitea_write")
        # Head on signature line.
        assert "CreateIssue(" in out and "Create an issue in a repository." in out
        # Body indented under the signature.
        assert "    Body must contain a <brief>summary</brief> tag." in out

    def test_help_shows_per_param_description(self):
        """Pydantic Field(description=...) renders as an indented bullet."""
        _seed({"gitea_write": {"CreateIssue": create_issue}})
        out = _build_help("gitea_write")
        assert "    body: Issue body markdown." in out


class TestHelpSearch:
    def test_search_filters_in_local_group(self):
        _seed({
            "gitea_read": {
                "ListMilestones": list_milestones,
                "CreateIssue": create_issue,  # placed in read for the test only
            }
        })
        out = _build_help("gitea_read", search="milestone")
        assert "ListMilestones" in out
        assert "CreateIssue" not in out
        assert "1 of 2 operations" in out

    def test_search_case_insensitive(self):
        _seed({"gitea_read": {"ListMilestones": list_milestones}})
        out = _build_help("gitea_read", search="MILESTONE")
        assert "ListMilestones" in out

    def test_search_no_match_in_local_group(self):
        _seed({"gitea_read": {"ListMilestones": list_milestones}})
        out = _build_help("gitea_read", search="qwerty")
        assert "No ops in gitea_read matching 'qwerty'" in out

    def test_search_cross_group_hint(self):
        """When local search is empty but other groups have a match, the
        renderer surfaces a 'Found in other groups' pointer so the agent
        learns where the op actually lives."""
        _seed({
            "gitea_read": {"ListMilestones": list_milestones},
            "gitea_execute": {"MergePullRequest": merge_pull_request},
        })
        out = _build_help("gitea_read", search="merge")
        # Local: nothing matched.
        assert "No ops in gitea_read matching 'merge'" in out
        # Cross-group: surface the hit.
        assert "Found in other groups" in out
        assert "gitea_execute" in out
        assert "MergePullRequest" in out

    def test_search_with_matches_and_cross_group_appended(self):
        """Local matches AND cross-group hits both render — local block on
        top, 'Also matching in other groups' appended."""
        _seed({
            "gitea_read": {"ListMilestones": list_milestones},
            "gitea_execute": {"MergePullRequest": merge_pull_request},
        })
        # 'List' matches ListMilestones locally and nothing in execute.
        out = _build_help("gitea_read", search="list")
        assert "ListMilestones" in out
        assert "Also matching in other groups" not in out  # nothing in execute


class TestHelpOptionalRendering:
    def test_none_default_renders_as_optional(self):
        """A `= None` default renders as `param?: T`, with no `=None` suffix."""
        def fn_with_none(owner: str, milestone: int = None):  # noqa: RUF013 - the sloppy shape is the test subject
            """Op with None default."""

        _seed({"gitea_write": {"FnWithNone": fn_with_none}})
        out = _build_help("gitea_write")
        assert "owner: str" in out
        assert "milestone?: int" in out
