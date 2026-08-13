"""Semantic tests for the issue and issue-comment list filters.

tests/test_swagger_conformance.py guards the filter NAMES: Gitea silently
ignores query params it does not know, so a misspelled one still answers 200
with the filter never applied. This module guards the other half - that each
filter actually changes the result the way its documentation claims.

Every test is two-sided, like test_63/test_64 in test_integration.py: the
matching object is in the filtered result, a non-matching object is not, and
both are in the unfiltered baseline. A filter that drops everything, or
nothing, fails instead of passing on a one-sided positive check.

The fixture builds its own prefixed repo and org and deletes them again, and
every assertion names this module's own objects (issue numbers, titles,
comment ids) - never a global count, because other suites run against the
same instance at the same time.
"""

from __future__ import annotations

import time
from collections.abc import Iterator
from dataclasses import dataclass
from typing import Any

import pytest
from conftest import ADMIN_USER, AgentSimulator

pytestmark = pytest.mark.integration

PREFIX = "fsem-issues"
REPO = f"{PREFIX}-semantics"
ORG = f"{PREFIX}-org"
ORG_REPO = f"{PREFIX}-org-repo"
LABEL = f"{PREFIX}-label"
BRANCH = f"{PREFIX}/pr-source"

# MARKER is in every title this module creates, so one search returns them all
# and can serve as the unfiltered baseline; NEEDLE is in exactly one title.
# Neither appears in any body or comment, because Gitea's issue search also
# matches those and would otherwise widen a search behind our back.
MARKER = "fsemissuesmarker"
NEEDLE = "fsemissuesneedle"

OPEN_TITLE = f"{MARKER} {NEEDLE} open labeled issue"
CLOSED_TITLE = f"{MARKER} closed control issue"
PR_TITLE = f"{MARKER} pull request"
ORG_TITLE = f"{MARKER} org owned issue"

# Gitea stores comment timestamps with second granularity: two comments created
# inside the same second are indistinguishable to since/before.
COMMENT_GAP_SECONDS = 1.2

SEARCH_INDEX_TIMEOUT = 60.0
SEARCH_INDEX_INTERVAL = 0.5


@dataclass(frozen=True)
class World:
    """Identifiers of the objects the fixture created, for scoped assertions."""

    open_index: int
    closed_index: int
    pr_index: int
    early_comment_id: int
    early_updated_at: str
    late_comment_id: int
    late_updated_at: str


def _drop_leftovers(agent: AgentSimulator) -> None:
    """Delete what an aborted earlier run left behind, or creation would 409.

    Existence is read first instead of deleting and ignoring a 404, so no error
    is ever swallowed. Only this module's own prefixed names are touched.
    """
    own_repos = {
        repo["full_name"] for repo in agent.call("list_user_repos", username=ADMIN_USER)
    }
    if f"{ADMIN_USER}/{REPO}" in own_repos:
        agent.call("delete_repo", owner=ADMIN_USER, repo=REPO)

    if ORG not in {org["username"] for org in agent.call("list_orgs")}:
        return
    org_repos = {repo["full_name"] for repo in agent.call("list_org_repos", org=ORG)}
    if f"{ORG}/{ORG_REPO}" in org_repos:
        agent.call("delete_repo", owner=ORG, repo=ORG_REPO)
    agent.call("delete_org", org=ORG)


def _issue_numbers(agent: AgentSimulator, **params: Any) -> set[int]:
    """Issue numbers list_issues returns for our repo under these filters."""
    return {
        issue["number"]
        for issue in agent.call(
            "list_issues", owner=ADMIN_USER, repo=REPO, limit=50, **params
        )
    }


def _search_titles(agent: AgentSimulator, **params: Any) -> set[str]:
    """Titles search_issues returns under these filters.

    Search is cross-repo and issue numbers are per-repo indexes, so a number
    from another suite's repo could collide with ours. Titles carry this
    module's marker and identify our objects unambiguously.
    """
    return {
        issue["title"] for issue in agent.call("search_issues", limit=50, **params)
    }


def _await_search_index(agent: AgentSimulator) -> None:
    """Block until every fixture title is searchable.

    Gitea's default issue indexer is bleve, fed by an async queue: an issue that
    list_issues already returns (a plain DB query) can still be missing from
    search_issues for a moment. Without this wait the search tests would fail on
    indexing latency rather than on filter semantics.
    """
    expected = {OPEN_TITLE, CLOSED_TITLE, PR_TITLE, ORG_TITLE}
    deadline = time.monotonic() + SEARCH_INDEX_TIMEOUT
    indexed: set[str] = set()
    while time.monotonic() < deadline:
        indexed = _search_titles(agent, query=MARKER, state="all")
        if expected <= indexed:
            return
        time.sleep(SEARCH_INDEX_INTERVAL)
    raise AssertionError(
        f"issue search index did not catch up within {SEARCH_INDEX_TIMEOUT}s; "
        f"still missing {sorted(expected - indexed)}"
    )


def _comment_ids(agent: AgentSimulator, **params: Any) -> set[int]:
    """Comment ids list_repo_issue_comments returns for our repo."""
    return {
        comment["id"]
        for comment in agent.call(
            "list_repo_issue_comments", owner=ADMIN_USER, repo=REPO, **params
        )
    }


def _assert_two_sided(
    what: str,
    baseline: set[Any],
    filtered: set[Any],
    kept: Any,
    dropped: Any,
) -> None:
    """Assert the filter keeps `kept` and drops `dropped`, both present unfiltered.

    The baseline half is what makes the check two-sided: without it a filter
    that returns everything, or nothing, would still satisfy one of the two
    membership assertions.
    """
    assert kept in baseline, f"{what}: {kept!r} missing from the unfiltered baseline"
    assert dropped in baseline, (
        f"{what}: {dropped!r} missing from the unfiltered baseline"
    )
    assert kept in filtered, f"{what}: filter dropped the matching {kept!r}"
    assert dropped not in filtered, f"{what}: filter kept the non-matching {dropped!r}"


@pytest.fixture(scope="class")
def world(agent: AgentSimulator) -> Iterator[World]:
    """Build the repo, org, issues, label, PR and timed comments; drop them after.

    Everything is created through the agent under this module's own prefix, so
    parallel suites on the same instance never see it, and the teardown leaves
    the instance as it found it.
    """
    _drop_leftovers(agent)
    agent.call(
        "create_repo",
        name=REPO,
        description="Fixture repo for issue filter semantics",
        private=True,
        auto_init=True,
        default_branch="main",
    )
    label = agent.call(
        "create_repo_label", owner=ADMIN_USER, repo=REPO, name=LABEL, color="#00ff00"
    )

    open_issue = agent.call(
        "create_issue",
        owner=ADMIN_USER,
        repo=REPO,
        title=OPEN_TITLE,
        body="<brief>Open issue carrying the label</brief>\nMatches every filter.",
    )
    agent.call(
        "add_issue_labels",
        owner=ADMIN_USER,
        repo=REPO,
        index=open_issue["number"],
        labels=[label["id"]],
    )
    closed_issue = agent.call(
        "create_issue",
        owner=ADMIN_USER,
        repo=REPO,
        title=CLOSED_TITLE,
        body="<brief>Closed issue with no label</brief>\nThe control object.",
    )
    agent.call(
        "edit_issue",
        owner=ADMIN_USER,
        repo=REPO,
        index=closed_issue["number"],
        state="closed",
    )

    agent.call(
        "create_branch",
        owner=ADMIN_USER,
        repo=REPO,
        new_branch_name=BRANCH,
        old_branch_name="main",
    )
    agent.call(
        "create_file",
        owner=ADMIN_USER,
        repo=REPO,
        filepath="fsem-issues.txt",
        content="filter semantics fixture\n",
        message="Add a file so the fixture PR has a diff",
        branch=BRANCH,
    )
    pull = agent.call(
        "create_pull_request",
        owner=ADMIN_USER,
        repo=REPO,
        title=PR_TITLE,
        head=BRANCH,
        base="main",
        body="<brief>Fixture PR for the issues/pulls type filter</brief>",
    )

    early = agent.call(
        "create_issue_comment",
        owner=ADMIN_USER,
        repo=REPO,
        index=open_issue["number"],
        body="Earlier fixture comment.",
    )
    time.sleep(COMMENT_GAP_SECONDS)
    late = agent.call(
        "create_issue_comment",
        owner=ADMIN_USER,
        repo=REPO,
        index=closed_issue["number"],
        body="Later fixture comment.",
    )
    # Timestamps read back from Gitea, not guessed; equal ones would make the
    # since/before tests vacuous.
    assert early["updated_at"] != late["updated_at"], (
        "fixture comments share an update timestamp "
        f"({early['updated_at']!r}); since/before cannot separate them"
    )

    agent.call(
        "create_org",
        username=ORG,
        description="Fixture org for the search owner filter",
        visibility="private",
    )
    agent.call(
        "create_org_repo",
        org=ORG,
        name=ORG_REPO,
        description="Fixture org repo for the search owner filter",
        private=True,
        auto_init=True,
        default_branch="main",
    )
    agent.call(
        "create_issue",
        owner=ORG,
        repo=ORG_REPO,
        title=ORG_TITLE,
        body="<brief>Issue owned by the fixture org</brief>\nOff-owner control.",
    )
    _await_search_index(agent)

    yield World(
        open_index=open_issue["number"],
        closed_index=closed_issue["number"],
        pr_index=pull["number"],
        early_comment_id=early["id"],
        early_updated_at=early["updated_at"],
        late_comment_id=late["id"],
        late_updated_at=late["updated_at"],
    )

    agent.call("delete_repo", owner=ADMIN_USER, repo=REPO)
    agent.call("delete_repo", owner=ORG, repo=ORG_REPO)
    agent.call("delete_org", org=ORG)


@pytest.mark.usefixtures("world")
class TestIssueFilterSemantics:
    """Every filter here must change the result, not merely be accepted.

    One class, so the class-scoped fixture builds its repo, org, issues, PR and
    comments exactly once for the whole file.
    """

    # ── list_issues ───────────────────────────────────────────

    def test_list_issues_state(self, agent: AgentSimulator, world: World) -> None:
        """state=open keeps the open issue and drops the closed one, and back."""
        baseline = _issue_numbers(agent, state="all")
        _assert_two_sided(
            "list_issues state=open",
            baseline,
            _issue_numbers(agent, state="open"),
            world.open_index,
            world.closed_index,
        )
        _assert_two_sided(
            "list_issues state=closed",
            baseline,
            _issue_numbers(agent, state="closed"),
            world.closed_index,
            world.open_index,
        )

    def test_list_issues_labels(self, agent: AgentSimulator, world: World) -> None:
        """labels takes label NAMES: the unlabeled issue must drop out."""
        _assert_two_sided(
            f"list_issues labels={LABEL}",
            _issue_numbers(agent, state="all"),
            _issue_numbers(agent, state="all", labels=LABEL),
            world.open_index,
            world.closed_index,
        )

    def test_list_issues_type(self, agent: AgentSimulator, world: World) -> None:
        """type splits issues from PRs; unfiltered the endpoint returns both."""
        baseline = _issue_numbers(agent, state="all")
        _assert_two_sided(
            "list_issues type=issues",
            baseline,
            _issue_numbers(agent, state="all", type="issues"),
            world.open_index,
            world.pr_index,
        )
        _assert_two_sided(
            "list_issues type=pulls",
            baseline,
            _issue_numbers(agent, state="all", type="pulls"),
            world.pr_index,
            world.open_index,
        )

    # ── search_issues (asserted on titles: search is cross-repo) ──

    def test_search_issues_query(self, agent: AgentSimulator) -> None:
        """q matches title text.

        q is required, so the baseline is the marker every fixture title shares;
        the needle query then keeps only the one title that carries it.
        """
        _assert_two_sided(
            f"search_issues q={NEEDLE}",
            _search_titles(agent, query=MARKER, state="all"),
            _search_titles(agent, query=NEEDLE, state="all"),
            OPEN_TITLE,
            CLOSED_TITLE,
        )

    def test_search_issues_owner(self, agent: AgentSimulator) -> None:
        """owner scopes the cross-repo search to one repo owner.

        Two-sided across two owners rather than one: the user's issue and the
        fixture org's issue both match the shared marker, and each owner filter
        must keep its own and drop the other's. Both owners are this module's
        own objects, so nothing outside the prefix is created or asserted on.
        """
        baseline = _search_titles(agent, query=MARKER, state="all")
        _assert_two_sided(
            f"search_issues owner={ADMIN_USER}",
            baseline,
            _search_titles(agent, query=MARKER, state="all", owner=ADMIN_USER),
            OPEN_TITLE,
            ORG_TITLE,
        )
        _assert_two_sided(
            f"search_issues owner={ORG}",
            baseline,
            _search_titles(agent, query=MARKER, state="all", owner=ORG),
            ORG_TITLE,
            OPEN_TITLE,
        )

    def test_search_issues_state(self, agent: AgentSimulator) -> None:
        """state=open keeps the open issue and drops the closed one, and back."""
        baseline = _search_titles(agent, query=MARKER, state="all")
        _assert_two_sided(
            "search_issues state=open",
            baseline,
            _search_titles(agent, query=MARKER, state="open"),
            OPEN_TITLE,
            CLOSED_TITLE,
        )
        _assert_two_sided(
            "search_issues state=closed",
            baseline,
            _search_titles(agent, query=MARKER, state="closed"),
            CLOSED_TITLE,
            OPEN_TITLE,
        )

    def test_search_issues_labels(self, agent: AgentSimulator) -> None:
        """labels takes label NAMES here too: the unlabeled issue must drop out."""
        _assert_two_sided(
            f"search_issues labels={LABEL}",
            _search_titles(agent, query=MARKER, state="all"),
            _search_titles(agent, query=MARKER, state="all", labels=LABEL),
            OPEN_TITLE,
            CLOSED_TITLE,
        )

    def test_search_issues_type(self, agent: AgentSimulator) -> None:
        """type splits issues from PRs; unfiltered the search returns both."""
        baseline = _search_titles(agent, query=MARKER, state="all")
        _assert_two_sided(
            "search_issues type=issues",
            baseline,
            _search_titles(agent, query=MARKER, state="all", type="issues"),
            OPEN_TITLE,
            PR_TITLE,
        )
        _assert_two_sided(
            "search_issues type=pulls",
            baseline,
            _search_titles(agent, query=MARKER, state="all", type="pulls"),
            PR_TITLE,
            OPEN_TITLE,
        )

    # ── list_repo_issue_comments ──────────────────────────────

    def test_repo_issue_comments_since(
        self, agent: AgentSimulator, world: World
    ) -> None:
        """since is inclusive at its own timestamp: the earlier comment drops out."""
        _assert_two_sided(
            "list_repo_issue_comments since",
            _comment_ids(agent),
            _comment_ids(agent, since=world.late_updated_at),
            world.late_comment_id,
            world.early_comment_id,
        )

    def test_repo_issue_comments_before(
        self, agent: AgentSimulator, world: World
    ) -> None:
        """before is inclusive at its own timestamp: the later comment drops out."""
        _assert_two_sided(
            "list_repo_issue_comments before",
            _comment_ids(agent),
            _comment_ids(agent, before=world.early_updated_at),
            world.early_comment_id,
            world.late_comment_id,
        )
