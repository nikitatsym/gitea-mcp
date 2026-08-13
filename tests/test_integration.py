"""Integration tests — an agent works with Gitea entirely through MCP tools.

The test simulates a realistic agent workflow:
1. Check connection → 2. Create repo → 3. Work with files →
4. Create branches → 5. Work with issues (full lifecycle) →
6. Create and merge PR → 7. Tags and releases →
8. Wiki → 9. Actions/CI → 10. Org and teams →
11. Admin operations → 12. Cleanup
"""

from contextlib import contextmanager

import pytest
from conftest import (
    upload_generic_package,
    wait_for_pr_mergeable,
    wait_for_workflow_run,
)

from gitea_mcp.client import GiteaError

pytestmark = pytest.mark.integration

ADMIN_USER = "testadmin"


@contextmanager
def gitea_error(status: int, message: str):
    """Assert the wrapped call fails with exactly this Gitea status and body.

    Used where the op cannot succeed in the test environment (no runner, no
    signing key, nothing orphaned to adopt): the contract gets pinned instead
    of the failure being waved through.
    """
    with pytest.raises(GiteaError) as exc:
        yield
    assert exc.value.status == status, f"expected {status}, got: {exc.value}"
    assert message in str(exc.value.body), f"body missing {message!r}: {exc.value}"


@pytest.mark.usefixtures("configure_env")
class TestAgentWorkflow:
    """Sequential test simulating a full agent workflow."""

    # Shared state between tests
    repo_name = "agent-test-repo"
    owner = ADMIN_USER

    # Will be populated by tests
    label_id = None
    milestone_id = None
    issue_index = None
    issue_comment_id = None
    pr_index = None
    tag_name = None
    release_id = None
    webhook_id = None
    org_name = None
    team_id = None
    second_issue_index = None
    filter_issue_index = None
    workflow_run_id = None

    def _issue(self, agent, index=None):
        """Read one issue back, to assert the effect of the op just called."""
        return agent.call("get_issue",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index if index is None else index,
        )

    # ── 1. Connection & General ───────────────────────────────

    def test_01_version(self, agent):
        """Agent checks Gitea version."""
        result = agent.call("gitea_version")
        assert "service" in result

    def test_02_current_user(self, agent):
        """Agent verifies its identity."""
        result = agent.call("get_current_user")
        assert result["login"] == ADMIN_USER

    def test_03_user_settings(self, agent):
        """Agent reads and updates user settings."""
        settings = agent.call("get_user_settings")
        # Values depend on earlier runs of test_313; the keys are the contract.
        assert {"language", "theme", "full_name"} <= set(settings)

    def test_04_search_users(self, agent):
        """Agent searches for users."""
        result = agent.call("search_users", query=ADMIN_USER)
        # Result format: {"data": [...], "ok": true} or just a list
        users = result.get("data", result) if isinstance(result, dict) else result
        assert any(u["login"] == ADMIN_USER for u in (users if isinstance(users, list) else [users]))

    def test_05_get_user(self, agent):
        """Agent gets user profile."""
        result = agent.call("get_user", username=ADMIN_USER)
        assert result["login"] == ADMIN_USER

    # ── 2. Repository ─────────────────────────────────────────

    def test_10_create_repo(self, agent):
        """Agent creates a test repository."""
        result = agent.call("create_repo",
            name=self.repo_name,
            description="Test repo for agent workflow",
            private=False,
            auto_init=True,
            default_branch="main",
        )
        assert result["name"] == self.repo_name
        assert result["owner"]["login"] == ADMIN_USER

    def test_11_get_repo(self, agent):
        """Agent verifies the repo exists."""
        result = agent.call("get_repo", owner=self.owner, repo=self.repo_name)
        assert result["name"] == self.repo_name

    def test_12_edit_repo(self, agent):
        """Agent updates repo description."""
        result = agent.call("edit_repo",
            owner=self.owner,
            repo=self.repo_name,
            description="Updated by agent",
            has_issues=True,
            has_wiki=True,
        )
        assert result["description"] == "Updated by agent"

    def test_13_search_repos(self, agent):
        """Agent searches for repos."""
        result = agent.call("search_repos", query="agent-test")
        # search_repos returns {"data": [...], "ok": true}
        data = result.get("data", result) if isinstance(result, dict) else result
        assert len(data) >= 1

    def test_14_repo_topics(self, agent):
        """Agent sets and reads topics."""
        agent.call("set_repo_topics",
            owner=self.owner,
            repo=self.repo_name,
            topics=["test", "mcp", "automation"],
        )
        result = agent.call("list_repo_topics", owner=self.owner, repo=self.repo_name)
        topics = result.get("topics", result) if isinstance(result, dict) else result
        assert "test" in topics

    def test_15_star_unstar(self, agent):
        """Agent stars and unstars the repo."""
        agent.call("star_repo", owner=self.owner, repo=self.repo_name)
        agent.call("unstar_repo", owner=self.owner, repo=self.repo_name)

    # ── 3. Files ──────────────────────────────────────────────

    def test_20_create_file(self, agent):
        """Agent creates a file in the repo."""
        result = agent.call("create_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath="src/hello.py",
            content='print("Hello from agent!")\n',
            message="Add hello.py via agent",
        )
        assert result["content"]["name"] == "hello.py"

    def test_21_get_file_content(self, agent):
        """Agent reads the file back."""
        result = agent.call("get_file_content",
            owner=self.owner,
            repo=self.repo_name,
            filepath="src/hello.py",
        )
        assert result["name"] == "hello.py"
        assert result.get("content") is not None  # base64 content
        TestAgentWorkflow._file_sha = result["sha"]

    def test_22_update_file(self, agent):
        """Agent updates the file."""
        result = agent.call("update_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath="src/hello.py",
            content='print("Updated by agent!")\n',
            message="Update hello.py via agent",
            sha=TestAgentWorkflow._file_sha,
        )
        assert result["content"]["name"] == "hello.py"

    def test_23_get_directory(self, agent):
        """Agent lists directory contents."""
        result = agent.call("get_directory_content",
            owner=self.owner,
            repo=self.repo_name,
            dirpath="src",
        )
        assert isinstance(result, list)
        assert any(f["name"] == "hello.py" for f in result)

    def test_24_get_raw_file(self, agent):
        """Agent reads raw file content."""
        result = agent.call_raw("get_raw_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath="src/hello.py",
        )
        assert "Updated by agent" in result

    def test_25_create_more_files(self, agent):
        """Agent creates additional files for later use."""
        agent.call("create_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath="docs/README.md",
            content="# Documentation\n\nThis is the docs folder.\n",
            message="Add docs/README.md",
        )
        agent.call("create_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath=".gitea/workflows/test.yml",
            content="""name: Test
on:
  workflow_dispatch:
    inputs:
      greeting:
        description: 'Greeting message'
        required: false
        default: 'hello'
jobs:
  test:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: echo "Agent test workflow - ${{ inputs.greeting }}"
""",
            message="Add test workflow",
        )

    # ── 4. Branches ───────────────────────────────────────────

    def test_30_list_branches(self, agent):
        """Agent lists branches."""
        result = agent.call("list_branches", owner=self.owner, repo=self.repo_name)
        assert isinstance(result, list)
        assert any(b["name"] == "main" for b in result)

    def test_31_create_branch(self, agent):
        """Agent creates a feature branch."""
        result = agent.call("create_branch",
            owner=self.owner,
            repo=self.repo_name,
            new_branch_name="feature/agent-changes",
            old_branch_name="main",
        )
        assert result["name"] == "feature/agent-changes"

    def test_32_get_branch(self, agent):
        """Agent gets branch info."""
        result = agent.call("get_branch",
            owner=self.owner,
            repo=self.repo_name,
            branch="feature/agent-changes",
        )
        assert result["name"] == "feature/agent-changes"

    def test_33_create_file_in_branch(self, agent):
        """Agent creates a file in the feature branch."""
        agent.call("create_file",
            owner=self.owner,
            repo=self.repo_name,
            filepath="src/feature.py",
            content='def new_feature():\n    return "implemented by agent"\n',
            message="Add feature.py in feature branch",
            branch="feature/agent-changes",
        )

    # ── 5. Commits ────────────────────────────────────────────

    def test_35_list_commits(self, agent):
        """Agent lists commits."""
        result = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_36_compare_commits(self, agent):
        """Agent compares branches."""
        result = agent.call("compare_commits",
            owner=self.owner,
            repo=self.repo_name,
            base="main",
            head="feature/agent-changes",
        )
        assert "commits" in result

    # ── 6. Labels & Milestones ────────────────────────────────

    def test_40_create_label(self, agent):
        """Agent creates a label."""
        result = agent.call("create_repo_label",
            owner=self.owner,
            repo=self.repo_name,
            name="bug",
            color="#d73a4a",
            description="Something isn't working",
        )
        TestAgentWorkflow.label_id = result["id"]
        assert result["name"] == "bug"

    def test_41_list_labels(self, agent):
        """Agent lists labels."""
        result = agent.call("list_repo_labels", owner=self.owner, repo=self.repo_name)
        assert any(label["name"] == "bug" for label in result)

    def test_42_create_milestone(self, agent):
        """Agent creates a milestone."""
        result = agent.call("create_milestone",
            owner=self.owner,
            repo=self.repo_name,
            title="v1.0",
            description="First release milestone",
        )
        TestAgentWorkflow.milestone_id = result["id"]
        assert result["title"] == "v1.0"

    def test_43_list_milestones(self, agent):
        """Agent lists milestones."""
        result = agent.call("list_milestones", owner=self.owner, repo=self.repo_name)
        assert any(m["title"] == "v1.0" for m in result)

    # ── 7. Issues (full lifecycle) ────────────────────────────

    def test_50_create_issue(self, agent):
        """Agent creates an issue with labels and milestone."""
        result = agent.call("create_issue",
            owner=self.owner,
            repo=self.repo_name,
            title="Fix the bug in hello.py",
            body="<brief>Fix greeting message in hello.py</brief>\nThe greeting message needs to be updated.\n\n- [ ] Update message\n- [ ] Add tests",
            labels=[TestAgentWorkflow.label_id],
            milestone_id=TestAgentWorkflow.milestone_id,
        )
        TestAgentWorkflow.issue_index = result["number"]
        assert result["title"] == "Fix the bug in hello.py"

    def test_51_get_issue(self, agent):
        """Agent reads the issue back."""
        result = agent.call("get_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert result["number"] == self.issue_index

    def test_52_edit_issue(self, agent):
        """Agent updates the issue."""
        result = agent.call("edit_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            title="Fix the bug in hello.py [Updated]",
            assignees=[ADMIN_USER],
        )
        assert "Updated" in result["title"]

    def test_53_issue_comment(self, agent):
        """Agent adds a comment."""
        result = agent.call("create_issue_comment",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            body="I'm working on this. The fix is in the feature branch.",
        )
        TestAgentWorkflow.issue_comment_id = result["id"]
        assert "working on this" in result["body"]

    def test_54_edit_comment(self, agent):
        """Agent edits the comment."""
        result = agent.call("edit_issue_comment",
            owner=self.owner,
            repo=self.repo_name,
            comment_id=self.issue_comment_id,
            body="I'm working on this. The fix is ready in feature/agent-changes.",
        )
        assert "ready" in result["body"]

    def test_55_issue_labels(self, agent):
        """Agent manages issue labels."""
        labels = agent.call("list_issue_labels",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert len(labels) >= 1

    def test_56_set_deadline(self, agent):
        """Agent sets a deadline."""
        result = agent.call("set_issue_deadline",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            due_date="2030-12-31T23:59:59Z",
        )
        assert result["due_date"].startswith("2030-12-31")

    def test_57_issue_reactions(self, agent):
        """Agent adds and lists reactions."""
        agent.call("add_issue_reaction",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            reaction="+1",
        )
        reactions = agent.call("list_issue_reactions",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert isinstance(reactions, list)
        assert len(reactions) >= 1

    def test_58_comment_reactions(self, agent):
        """Agent adds reaction to a comment."""
        agent.call("add_comment_reaction",
            owner=self.owner,
            repo=self.repo_name,
            comment_id=self.issue_comment_id,
            reaction="heart",
        )
        reactions = agent.call("list_comment_reactions",
            owner=self.owner,
            repo=self.repo_name,
            comment_id=self.issue_comment_id,
        )
        assert len(reactions) >= 1

    def test_59_time_tracking(self, agent):
        """Agent tracks time on the issue."""
        result = agent.call("add_tracked_time",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            time=3600,  # 1 hour
        )
        assert result["time"] == 3600

        times = agent.call("list_tracked_times",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert len(times) >= 1

    def test_60_issue_dependencies(self, agent):
        """Agent creates a second issue, sets and clears a dependency."""
        # Create a second issue
        result = agent.call("create_issue",
            owner=self.owner,
            repo=self.repo_name,
            title="Prerequisite task",
            body="<brief>Prerequisite task for dependency test</brief>\nThis must be done first",
        )
        TestAgentWorkflow.second_issue_index = result["number"]

        dep = {
            "owner": self.owner, "repo": self.repo_name,
            "index": self.issue_index, "depends_on_id": self.second_issue_index,
        }

        def listed():
            return agent.call("list_issue_dependencies",
                owner=self.owner, repo=self.repo_name, index=self.issue_index,
            )

        agent.call("add_issue_dependency", **dep)
        assert [d["number"] for d in listed()] == [self.second_issue_index]

        # Remove it again: an open dependency would block closing the issue
        # later in the workflow (Gitea returns 412 on close).
        agent.call("remove_issue_dependency", **dep)
        assert listed() == []

    def test_61_pin_lock_issue(self, agent):
        """Agent pins and locks an issue."""
        agent.call("pin_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        agent.call("lock_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        issue = self._issue(agent)
        assert issue["pin_order"] == 1  # stays pinned until test_223 unpins
        assert issue["is_locked"] is True

        # Unlock it so we can still work with it
        agent.call("unlock_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert self._issue(agent)["is_locked"] is False

    def test_62_search_issues(self, agent):
        """Agent searches issues."""
        result = agent.call("list_issues",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def _assert_filter_excludes(self, agent, **filters):
        """Assert the filter keeps the first issue and drops the control issue.

        Both must show up unfiltered, so a filter that excludes everything (or
        nothing) fails instead of passing on a one-sided positive check.
        """
        def numbers(**params):
            # Raised limit keeps this independent of how many issues earlier
            # tests create, up to Gitea's 50-item response cap (the default
            # limit 20 plus newest-first ordering would hide the oldest).
            return [i["number"] for i in agent.call("list_issues",
                owner=self.owner, repo=self.repo_name, limit=100, **params,
            )]

        unfiltered = numbers()
        assert self.issue_index in unfiltered
        assert self.filter_issue_index in unfiltered

        filtered = numbers(**filters)
        assert self.issue_index in filtered
        assert self.filter_issue_index not in filtered

    def test_63_list_issues_milestone_filter(self, agent):
        """Agent filters issues by milestone: issues without it must drop out."""
        created = agent.call("create_issue",
            owner=self.owner,
            repo=self.repo_name,
            title="Filter control task",
            body="<brief>Control issue with no milestone and no assignee</brief>\nExists to prove the list_issues filters exclude non-matching issues.",
        )
        TestAgentWorkflow.filter_issue_index = created["number"]
        self._assert_filter_excludes(agent, milestone="v1.0")

    def test_64_list_issues_assignee_filter(self, agent):
        """Agent filters issues by assignee: unassigned issues must drop out."""
        agent.call("edit_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            assignees=[ADMIN_USER],
        )
        self._assert_filter_excludes(agent, assignee=ADMIN_USER)

    # ── 8. Pull Requests ──────────────────────────────────────

    def test_70_create_pr(self, agent):
        """Agent creates a pull request."""
        result = agent.call("create_pull_request",
            owner=self.owner,
            repo=self.repo_name,
            title="Add new feature",
            head="feature/agent-changes",
            base="main",
            body=f"Closes #{self.issue_index}\n\nThis PR adds the new feature implemented by the agent.",
        )
        TestAgentWorkflow.pr_index = result["number"]
        assert result["title"] == "Add new feature"

    def test_71_get_pr(self, agent):
        """Agent reads the PR."""
        result = agent.call("get_pull_request",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert result["number"] == self.pr_index

    def test_72_list_prs(self, agent):
        """Agent lists PRs."""
        result = agent.call("list_pull_requests",
            owner=self.owner,
            repo=self.repo_name,
            state="open",
        )
        assert any(pr["number"] == self.pr_index for pr in result)

    def test_73_pr_files(self, agent):
        """Agent checks what files changed in the PR."""
        result = agent.call("get_pull_request_files",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_74_pr_diff(self, agent):
        """Agent reads the PR diff."""
        result = agent.call_raw("get_pull_request_diff",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert "diff" in result.lower() or "@@" in result

    def test_75_pr_commits(self, agent):
        """Agent lists PR commits."""
        result = agent.call("get_pull_request_commits",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

    def test_76_create_review(self, agent):
        """Agent comments on its own PR — Gitea allows COMMENT reviews there
        (only APPROVED / REQUEST_CHANGES are refused on your own PR)."""
        result = agent.call("create_pull_review",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
            body="LGTM! The feature looks good.",
            event="COMMENT",
        )
        TestAgentWorkflow._review_id = result["id"]
        assert result["state"] == "COMMENT"
        assert result["body"] == "LGTM! The feature looks good."

    def test_77_list_reviews(self, agent):
        """Agent lists reviews."""
        result = agent.call("list_pull_reviews",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert [r["id"] for r in result] == [TestAgentWorkflow._review_id]

    def test_78_merge_pr(self, agent):
        """Agent merges the PR."""
        # Gitea's `mergeable` flag is computed asynchronously after PR
        # creation. POSTing to /merge while it's still null returns a
        # misleading 405 with body `{"message": "Please try again later"}`.
        # Wait for the flag to converge before attempting the merge — and
        # raise loudly if it goes False or times out so the test failure
        # mode is "Gitea told us no" rather than "405 in the middle of the
        # test for unclear reasons."
        wait_for_pr_mergeable(agent, self.owner, self.repo_name, self.pr_index)

        agent.call("merge_pull_request",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
            merge_type="merge",
            delete_branch_after_merge=True,
        )
        # Verify PR is merged
        pr = agent.call("get_pull_request",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert pr["merged"] is True or pr["state"] == "closed"

    # ── 9. Tags & Releases ────────────────────────────────────

    def test_80_create_tag(self, agent):
        """Agent creates a tag."""
        result = agent.call("create_tag",
            owner=self.owner,
            repo=self.repo_name,
            tag_name="v1.0.0",
            message="First release",
        )
        TestAgentWorkflow.tag_name = "v1.0.0"
        assert result["name"] == "v1.0.0"

    def test_81_list_tags(self, agent):
        """Agent lists tags."""
        result = agent.call("list_tags", owner=self.owner, repo=self.repo_name)
        assert any(t["name"] == "v1.0.0" for t in result)

    def test_82_create_release(self, agent):
        """Agent creates a release."""
        result = agent.call("create_release",
            owner=self.owner,
            repo=self.repo_name,
            tag_name="v1.0.0",
            name="Release v1.0.0",
            body="## Changes\n\n- Added new feature\n- Fixed bugs",
        )
        TestAgentWorkflow.release_id = result["id"]
        assert result["name"] == "Release v1.0.0"

    def test_83_get_release(self, agent):
        """Agent reads the release."""
        result = agent.call("get_release",
            owner=self.owner,
            repo=self.repo_name,
            release_id=self.release_id,
        )
        assert result["name"] == "Release v1.0.0"

    def test_84_edit_release(self, agent):
        """Agent updates the release."""
        result = agent.call("edit_release",
            owner=self.owner,
            repo=self.repo_name,
            release_id=self.release_id,
            body="## Changes\n\n- Added new feature\n- Fixed bugs\n- Updated by agent",
        )
        assert "Updated by agent" in result["body"]

    # ── 10. Wiki ──────────────────────────────────────────────

    def test_90_create_wiki(self, agent):
        """Agent creates a wiki page."""
        result = agent.call("create_wiki_page",
            owner=self.owner,
            repo=self.repo_name,
            title="Home",
            content="# Welcome\n\nThis wiki was created by the agent.\n",
            message="Create wiki home page",
        )
        assert result["title"] == "Home"

    def test_91_get_wiki(self, agent):
        """Agent reads the wiki page."""
        result = agent.call("get_wiki_page",
            owner=self.owner,
            repo=self.repo_name,
            page_name="Home",
        )
        assert result["title"] == "Home"

    def test_92_edit_wiki(self, agent):
        """Agent edits the wiki page."""
        agent.call("edit_wiki_page",
            owner=self.owner,
            repo=self.repo_name,
            page_name="Home",
            content="# Welcome\n\nThis wiki was updated by the agent.\n",
            message="Update wiki home page",
        )

    def test_93_list_wiki(self, agent):
        """Agent lists wiki pages."""
        result = agent.call("list_wiki_pages", owner=self.owner, repo=self.repo_name)
        assert isinstance(result, list)
        assert len(result) >= 1

    # ── 11. Webhooks ──────────────────────────────────────────

    def test_95_create_webhook(self, agent):
        """Agent creates a webhook."""
        result = agent.call("create_repo_webhook",
            owner=self.owner,
            repo=self.repo_name,
            config={"url": "https://httpbin.org/post", "content_type": "json"},
            events=["push", "pull_request"],
        )
        TestAgentWorkflow.webhook_id = result["id"]
        assert result["active"] is True

    def test_96_list_webhooks(self, agent):
        """Agent lists webhooks."""
        result = agent.call("list_repo_webhooks",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert any(h["id"] == self.webhook_id for h in result)

    def test_97_delete_webhook(self, agent):
        """Agent deletes the webhook."""
        agent.call("delete_repo_webhook",
            owner=self.owner,
            repo=self.repo_name,
            hook_id=self.webhook_id,
        )

    # ── 12. Commit Statuses ───────────────────────────────────

    def test_100_create_commit_status(self, agent):
        """Agent creates a commit status."""
        # Get latest commit SHA
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]

        result = agent.call("create_commit_status",
            owner=self.owner,
            repo=self.repo_name,
            sha=sha,
            state="success",
            description="All tests passed",
            context="ci/agent-test",
            target_url="https://example.com/builds/1",
        )
        assert result["status"] == "success"

    def test_101_get_combined_status(self, agent):
        """Agent checks combined commit status."""
        result = agent.call("get_combined_commit_status",
            owner=self.owner,
            repo=self.repo_name,
            ref="main",
        )
        assert result["state"] == "success"

    # ── 13. Actions / CI ──────────────────────────────────────

    def test_110_action_variables(self, agent):
        """Agent manages Action variables."""
        agent.call("create_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
            value="hello_from_agent",
        )
        var = agent.call("get_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
        )
        assert var["data"] == "hello_from_agent"

        agent.call("update_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
            value="updated_by_agent",
        )
        assert agent.call("get_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
        )["data"] == "updated_by_agent"

        variables = agent.call("list_action_variables",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert [v["name"] for v in variables] == ["TEST_VAR"]

        agent.call("delete_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
        )
        assert agent.call("list_action_variables",
            owner=self.owner,
            repo=self.repo_name,
        ) == []

    def test_111_action_secrets(self, agent):
        """Agent manages Action secrets."""
        agent.call("create_action_secret",
            owner=self.owner,
            repo=self.repo_name,
            secret_name="TEST_SECRET",
            data="super_secret_value",
        )

        secrets = agent.call("list_action_secrets",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert [sec["name"] for sec in secrets] == ["TEST_SECRET"]

        agent.call("delete_action_secret",
            owner=self.owner,
            repo=self.repo_name,
            secret_name="TEST_SECRET",
        )
        assert agent.call("list_action_secrets",
            owner=self.owner,
            repo=self.repo_name,
        ) == []

    def test_112_list_workflows(self, agent):
        """Agent lists workflows."""
        result = agent.call("list_workflows", owner=self.owner, repo=self.repo_name)
        assert [w["path"] for w in result["workflows"]] == [
            ".gitea/workflows/test.yml"
        ]

    def test_113_dispatch_workflow(self, agent):
        """Agent dispatches a workflow and reads back the queued run."""
        agent.call("dispatch_workflow",
            owner=self.owner,
            repo=self.repo_name,
            workflow_id="test.yml",
            ref="main",
            inputs={"greeting": "hello from agent test"},
        )
        run = wait_for_workflow_run(agent, self.owner, self.repo_name)
        TestAgentWorkflow.workflow_run_id = run["id"]
        assert run["event"] == "workflow_dispatch"
        assert run["path"] == "test.yml@refs/heads/main"
        # No act_runner is registered, so the run never leaves the queue.
        assert run["status"] == "queued"

    # ── 14. Organization & Teams ──────────────────────────────

    def test_120_create_org(self, agent):
        """Agent creates an organization."""
        TestAgentWorkflow.org_name = "test-org-agent"
        result = agent.call("create_org",
            username=self.org_name,
            full_name="Test Organization",
            description="Created by agent for testing",
            visibility="public",
        )
        assert result["name"] == self.org_name or result["username"] == self.org_name

    def test_121_get_org(self, agent):
        """Agent reads the org."""
        result = agent.call("get_org", org=self.org_name)
        assert result["name"] == self.org_name or result["username"] == self.org_name

    def test_122_edit_org(self, agent):
        """Agent updates the org."""
        result = agent.call("edit_org",
            org=self.org_name,
            description="Updated by agent",
        )
        assert result["description"] == "Updated by agent"

    def test_123_list_orgs(self, agent):
        """Agent lists orgs."""
        result = agent.call("list_orgs")
        assert isinstance(result, list)

    def test_124_create_team(self, agent):
        """Agent creates a team."""
        result = agent.call("create_team",
            org=self.org_name,
            name="developers",
            description="Dev team",
            permission="write",
            units=["repo.code", "repo.issues", "repo.pulls"],
        )
        TestAgentWorkflow.team_id = result["id"]
        assert result["name"] == "developers"

    def test_125_get_team(self, agent):
        """Agent reads the team."""
        result = agent.call("get_team", team_id=self.team_id)
        assert result["name"] == "developers"

    def test_126_list_teams(self, agent):
        """Agent lists org teams."""
        result = agent.call("list_org_teams", org=self.org_name)
        assert sorted(t["name"] for t in result) == ["Owners", "developers"]

    def test_127_team_members(self, agent):
        """Agent adds and lists team members."""
        agent.call("add_team_member",
            team_id=self.team_id,
            username=ADMIN_USER,
        )
        members = agent.call("list_team_members", team_id=self.team_id)
        assert [m["login"] for m in members] == [ADMIN_USER]

    def test_128_org_labels(self, agent):
        """Agent manages org labels."""
        result = agent.call("create_org_label",
            org=self.org_name,
            name="priority:high",
            color="#ff0000",
            description="High priority",
        )
        assert result["name"] == "priority:high"

        labels = agent.call("list_org_labels", org=self.org_name)
        assert any(label["name"] == "priority:high" for label in labels)

    # ── 15. Notifications ─────────────────────────────────────

    def test_130_notifications(self, agent):
        """Agent checks notifications."""
        result = agent.call("list_notifications")
        assert isinstance(result, list)

    # ── 16. Admin ─────────────────────────────────────────────

    def test_140_admin_list_users(self, agent):
        """Agent lists all users (admin)."""
        result = agent.call("admin_list_users")
        assert isinstance(result, list)
        assert any(u["login"] == ADMIN_USER for u in result)

    def test_141_admin_create_user(self, agent):
        """Agent creates a user (admin)."""
        result = agent.call("admin_create_user",
            username="testuser2",
            email="user2@test.local",
            password="testuser1234",
            must_change_password=False,
        )
        assert result["login"] == "testuser2"

    def test_142_admin_edit_user(self, agent):
        """Agent edits a user (admin)."""
        result = agent.call("admin_edit_user",
            username="testuser2",
            login_name="testuser2",
            active=True,
        )
        assert result["login"] == "testuser2"
        assert result["login_name"] == "testuser2"

    # ── 17. Misc ──────────────────────────────────────────────

    def test_150_render_markdown(self, agent):
        """Agent renders markdown."""
        result = agent.call_raw("render_markdown", text="# Hello\n\n**Bold** text")
        assert "<strong>Bold</strong>" in result
        assert ">Hello</h1>" in result

    def test_151_search_topics(self, agent):
        """Agent searches topics."""
        result = agent.call("search_topics", query="test")
        # 'test' is one of the topics set on the repo in test_14.
        assert "test" in [t["topic_name"] for t in result["topics"]]

    def test_152_gitignore_templates(self, agent):
        """Agent lists gitignore templates."""
        result = agent.call("list_gitignore_templates")
        assert isinstance(result, list)
        assert len(result) > 0

    def test_153_license_templates(self, agent):
        """Agent lists license templates."""
        result = agent.call("list_license_templates")
        assert isinstance(result, list)
        assert len(result) > 0

    # ── 18. New tools: Topics/Stars/Watchers ────────────────

    def test_160_add_delete_topic(self, agent):
        """Agent adds and removes individual topics."""
        agent.call("add_repo_topic",
            owner=self.owner, repo=self.repo_name, topic="agent-topic",
        )
        topics = agent.call("list_repo_topics", owner=self.owner, repo=self.repo_name)
        topic_list = topics.get("topics", topics) if isinstance(topics, dict) else topics
        assert "agent-topic" in topic_list
        agent.call("delete_repo_topic",
            owner=self.owner, repo=self.repo_name, topic="agent-topic",
        )

    def test_161_star_list_watchers(self, agent):
        """Agent stars, watches, and lists watchers."""
        agent.call("star_repo", owner=self.owner, repo=self.repo_name)
        starred = agent.call("list_my_starred_repos")
        assert isinstance(starred, list)
        agent.call("unstar_repo", owner=self.owner, repo=self.repo_name)

        agent.call("watch_repo", owner=self.owner, repo=self.repo_name)
        subs = agent.call("list_my_subscriptions")
        assert isinstance(subs, list)
        watchers = agent.call("list_repo_watchers", owner=self.owner, repo=self.repo_name)
        assert isinstance(watchers, list)
        agent.call("unwatch_repo", owner=self.owner, repo=self.repo_name)

    # ── 19. New tools: Branch/Tag Protection ─────────────────

    def test_162_branch_protection_crud(self, agent):
        """Agent manages branch protection rules."""
        agent.call("create_branch_protection",
            owner=self.owner, repo=self.repo_name,
            branch_name="main",
        )
        bp = agent.call("get_branch_protection",
            owner=self.owner, repo=self.repo_name, name="main",
        )
        assert bp["branch_name"] == "main"
        assert bp["enable_push"] is False

        edited = agent.call("edit_branch_protection",
            owner=self.owner, repo=self.repo_name, name="main",
            enable_push=True,
        )
        assert edited["enable_push"] is True

        bps = agent.call("list_branch_protections",
            owner=self.owner, repo=self.repo_name,
        )
        assert [b["branch_name"] for b in bps] == ["main"]

        agent.call("delete_branch_protection",
            owner=self.owner, repo=self.repo_name, name="main",
        )
        assert agent.call("list_branch_protections",
            owner=self.owner, repo=self.repo_name,
        ) == []

    def test_163_tag_protection_crud(self, agent):
        """Agent manages tag protection rules."""
        # Gitea rejects a rule with both whitelists empty (400).
        result = agent.call("create_tag_protection",
            owner=self.owner, repo=self.repo_name,
            name_pattern="v*",
            whitelist_usernames=[ADMIN_USER],
        )
        tp_id = result["id"]
        assert result["name_pattern"] == "v*"
        assert result["whitelist_usernames"] == [ADMIN_USER]

        tp = agent.call("get_tag_protection",
            owner=self.owner, repo=self.repo_name,
            tag_protection_id=tp_id,
        )
        assert tp["id"] == tp_id

        tps = agent.call("list_tag_protections",
            owner=self.owner, repo=self.repo_name,
        )
        assert [t["id"] for t in tps] == [tp_id]

        agent.call("delete_tag_protection",
            owner=self.owner, repo=self.repo_name,
            tag_protection_id=tp_id,
        )
        assert agent.call("list_tag_protections",
            owner=self.owner, repo=self.repo_name,
        ) == []

    # ── 20. New tools: Issue extras ──────────────────────────

    def test_164_issue_timeline(self, agent):
        """Agent gets issue timeline."""
        timeline = agent.call("get_issue_timeline",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert isinstance(timeline, list)

    def test_165_delete_issue_deadline(self, agent):
        """Agent removes the deadline set in test_56."""
        agent.call("delete_issue_deadline",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert self._issue(agent)["due_date"] is None

    def test_166_repo_issue_comments(self, agent):
        """Agent lists all issue comments in repo."""
        comments = agent.call("list_repo_issue_comments",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(comments, list)

    # ── 21. New tools: Org Webhooks ──────────────────────────

    def test_167_org_webhooks(self, agent):
        """Agent manages org webhooks."""
        result = agent.call("create_org_webhook",
            org=self.org_name,
            config={"url": "https://httpbin.org/post", "content_type": "json"},
            events=["push"],
        )
        hook_id = result["id"]

        hooks = agent.call("list_org_webhooks", org=self.org_name)
        assert isinstance(hooks, list)

        agent.call("delete_org_webhook", org=self.org_name, hook_id=hook_id)

    # ── 22. New tools: Org Actions secrets/variables ─────────

    def test_168_org_action_variables(self, agent):
        """Agent manages org action variables."""
        agent.call("create_org_action_variable",
            org=self.org_name,
            variable_name="ORG_TEST_VAR",
            value="org_value",
        )
        var = agent.call("get_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
        )
        assert var["data"] == "org_value"

        agent.call("update_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
            value="updated_org_value",
        )
        assert agent.call("get_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
        )["data"] == "updated_org_value"

        variables = agent.call("list_org_action_variables", org=self.org_name)
        assert [v["name"] for v in variables] == ["ORG_TEST_VAR"]

        agent.call("delete_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
        )
        assert agent.call("list_org_action_variables", org=self.org_name) == []

    def test_169_org_action_secrets(self, agent):
        """Agent manages org action secrets."""
        agent.call("create_org_action_secret",
            org=self.org_name,
            secret_name="ORG_SECRET",
            data="secret_data",
        )

        secrets = agent.call("list_org_action_secrets", org=self.org_name)
        assert [sec["name"] for sec in secrets] == ["ORG_SECRET"]

        agent.call("delete_org_action_secret",
            org=self.org_name, secret_name="ORG_SECRET",
        )
        assert agent.call("list_org_action_secrets", org=self.org_name) == []

    # ── 23. New tools: Org members ───────────────────────────

    def test_170_org_membership(self, agent):
        """Agent checks and manages org membership."""
        members = agent.call("list_org_members", org=self.org_name)
        assert [m["login"] for m in members] == [ADMIN_USER]

        agent.call("check_org_membership",
            org=self.org_name, username=ADMIN_USER,
        )

        public = agent.call("list_org_public_members", org=self.org_name)
        assert [m["login"] for m in public] == [ADMIN_USER]

    # ── 24. New tools: User emails/OAuth2/blocks ─────────────

    def test_171_user_emails(self, agent):
        """Agent manages user emails."""
        emails = agent.call("list_user_emails")
        assert isinstance(emails, list)

    def test_172_user_teams(self, agent):
        """Agent lists user teams."""
        teams = agent.call("list_user_teams")
        assert isinstance(teams, list)

    def test_173_oauth2_apps(self, agent):
        """Agent manages OAuth2 applications."""
        app = agent.call("create_oauth2_app",
            name="test-oauth-app",
            redirect_uris=["https://example.com/callback"],
        )
        app_id = app["id"]

        fetched = agent.call("get_oauth2_app", app_id=app_id)
        assert fetched["name"] == "test-oauth-app"

        agent.call("edit_oauth2_app",
            app_id=app_id,
            name="test-oauth-app-updated",
            redirect_uris=["https://example.com/callback"],
        )

        apps = agent.call("list_oauth2_apps")
        assert isinstance(apps, list)

        agent.call("delete_oauth2_app", app_id=app_id)

    def test_174_blocked_users(self, agent):
        """Agent manages blocked users."""
        blocked = agent.call("list_blocked_users")
        assert isinstance(blocked, list)

    def test_175_check_following(self, agent):
        """Agent checks a following relationship it hasn't established yet."""
        # test_310 covers the positive side after actually following.
        with gitea_error(404, "not found"):
            agent.call("check_user_following",
                username=ADMIN_USER, target="testuser2",
            )

    # ── 25. New tools: Notifications expansion ───────────────

    def test_176_notification_count(self, agent):
        """Agent checks notification count."""
        assert agent.call("get_new_notification_count") == {"new": 0}

    def test_177_repo_notifications(self, agent):
        """Agent lists repo notifications."""
        assert agent.call("list_repo_notifications",
            owner=self.owner, repo=self.repo_name,
        ) == []

    # ── 26. New tools: Repo extras ───────────────────────────

    def test_178_repo_languages(self, agent):
        """Agent gets repo languages."""
        result = agent.call("get_repo_languages",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, dict)

    def test_179_repo_assignees_reviewers(self, agent):
        """Agent lists assignees and reviewers."""
        assignees = agent.call("list_repo_assignees",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(assignees, list)

    def test_180_collaborator_permission(self, agent):
        """Agent checks collaborator permission."""
        result = agent.call("get_repo_collaborator_permission",
            owner=self.owner, repo=self.repo_name,
            collaborator=ADMIN_USER,
        )
        assert result["permission"] == "owner"
        assert result["user"]["login"] == ADMIN_USER

    def test_181_repo_refs(self, agent):
        """Agent lists git refs."""
        result = agent.call("list_repo_refs",
            owner=self.owner, repo=self.repo_name,
        )
        assert "refs/heads/main" in [r["ref"] for r in result]

    def test_182_git_tree(self, agent):
        """Agent gets git tree."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        result = agent.call("get_git_tree",
            owner=self.owner, repo=self.repo_name, sha=sha, recursive=True,
        )
        assert "src/hello.py" in [entry["path"] for entry in result["tree"]]

    def test_183_repo_teams(self, agent):
        """Agent lists teams of a user-owned repo — Gitea serves this only for
        org repos (405). The org-repo path is covered in test_330."""
        with gitea_error(405, "repo is not owned by an organization"):
            agent.call("list_repo_teams",
                owner=self.owner, repo=self.repo_name,
            )

    # ── 27. New tools: Admin expansion ───────────────────────

    def test_184_admin_list_repos(self, agent):
        """Agent lists all repos (admin) via search."""
        result = agent.call("admin_list_repos")
        assert f"{self.owner}/{self.repo_name}" in [r["full_name"] for r in result]

    def test_185_admin_list_emails(self, agent):
        """Agent lists all emails (admin)."""
        result = agent.call("admin_list_emails")
        assert isinstance(result, list)

    def test_186_admin_cron_jobs(self, agent):
        """Agent lists cron jobs (admin)."""
        result = agent.call("admin_list_cron_jobs")
        assert isinstance(result, list)

    # ── 28. New tools: Misc expansion ────────────────────────

    def test_187_gitignore_template_detail(self, agent):
        """Agent gets a specific gitignore template."""
        result = agent.call("get_gitignore_template", name="Python")
        assert result["name"] == "Python"
        assert "__pycache__/" in result["source"]

    def test_188_license_template_detail(self, agent):
        """Agent gets a specific license template."""
        result = agent.call("get_license_template", name="MIT")
        assert result["key"] == "MIT"
        assert "MIT License" in result["body"]

    # ── 29. Close issue ──────────────────────────────────────

    def test_190_close_issue(self, agent):
        """Agent closes the issue."""
        result = agent.call("edit_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
            state="closed",
        )
        assert result["state"] == "closed"

    # ── 30. Repo extended ops ────────────────────────────────

    def test_191_list_user_repos(self, agent):
        """Agent lists repos for a user."""
        result = agent.call("list_user_repos", username=ADMIN_USER)
        assert isinstance(result, list)

    def test_192_list_repo_collaborators(self, agent):
        """Agent lists repo collaborators."""
        result = agent.call("list_repo_collaborators",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, list)

    def test_193_add_check_remove_collaborator(self, agent):
        """Agent adds, checks, and removes a collaborator."""
        agent.call("add_repo_collaborator",
            owner=self.owner, repo=self.repo_name,
            collaborator="testuser2", permission="write",
        )
        agent.call("check_repo_collaborator",
            owner=self.owner, repo=self.repo_name,
            collaborator="testuser2",
        )
        agent.call("remove_repo_collaborator",
            owner=self.owner, repo=self.repo_name,
            collaborator="testuser2",
        )

    def test_194_fork_and_list_forks(self, agent):
        """Agent forks the repo and lists forks."""
        # Forking as testuser2 needs that user's token; fork to self instead.
        result = agent.call("fork_repo",
            owner=self.owner, repo=self.repo_name,
            name="agent-test-repo-fork",
        )
        assert result["name"] == "agent-test-repo-fork"
        forks = agent.call("list_forks",
            owner=self.owner, repo=self.repo_name,
        )
        assert [f["full_name"] for f in forks] == [
            f"{self.owner}/agent-test-repo-fork"
        ]
        agent.call("delete_repo", owner=self.owner, repo="agent-test-repo-fork")

    def test_195_list_repo_activities(self, agent):
        """Agent lists repo activity feed."""
        result = agent.call("list_repo_activities",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, list)

    def test_196_get_signing_key(self, agent):
        """Agent asks for the instance signing key — the test instance has no
        [repository.signing] key configured, so Gitea says so outright."""
        with gitea_error(404, "no signing key"):
            agent.call_raw("get_signing_key")

    def test_197_get_nodeinfo(self, agent):
        """Agent gets nodeinfo (route needs GITEA__federation__ENABLED).

        1.26 gutted federation: the route now maps to activitypub.NotImplemented,
        so the only contract left to pin is its 501."""
        with gitea_error(501, "Not implemented"):
            agent.call("get_nodeinfo")

    def test_198_list_repo_reviewers(self, agent):
        """Agent lists repo reviewers."""
        result = agent.call("list_repo_reviewers",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, list)

    def test_199_get_repo_archive(self, agent):
        """Agent downloads a repo archive."""
        result = agent.call_raw("get_repo_archive",
            owner=self.owner, repo=self.repo_name,
            archive="main.tar.gz",
        )
        assert len(result) > 0

    def test_200_get_repo_git_notes(self, agent):
        """Agent asks for a git note on a commit that has none."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        # Gitea reports a missing note as a missing commit; pushing a real note
        # needs git over SSH/HTTP, which is outside the MCP surface.
        with gitea_error(404, "commit doesn't exist"):
            agent.call("get_repo_git_notes",
                owner=self.owner, repo=self.repo_name, sha=sha,
            )

    # ── 31. Commits extended ──────────────────────────────────

    def test_201_get_commit(self, agent):
        """Agent gets a single commit."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        result = agent.call("get_commit",
            owner=self.owner, repo=self.repo_name, sha=sha,
        )
        assert result["sha"].startswith(sha[:12])

    def test_202_get_commit_diff(self, agent):
        """Agent gets a commit diff."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        result = agent.call_raw("get_commit_diff",
            owner=self.owner, repo=self.repo_name, sha=sha,
        )
        assert result.startswith("diff --git")

    def test_203_list_commit_statuses(self, agent):
        """Agent lists commit statuses."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        result = agent.call("list_commit_statuses",
            owner=self.owner, repo=self.repo_name, sha=sha,
        )
        assert isinstance(result, list)

    # ── 32. Issues extended ───────────────────────────────────

    def test_210_search_issues(self, agent):
        """Agent searches issues globally.

        The first issue is closed by test_190 and Gitea's default search state
        is 'open', so the query targets the still-open second issue."""
        result = agent.call("search_issues", query="Prerequisite")
        assert "Prerequisite task" in [i["title"] for i in result]

    def test_211_list_issue_comments(self, agent):
        """Agent lists comments on a specific issue."""
        result = agent.call("list_issue_comments",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert isinstance(result, list)

    def test_212_add_issue_labels(self, agent):
        """Agent adds labels to an issue."""
        result = agent.call("add_issue_labels",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            labels=[self.label_id],
        )
        assert isinstance(result, list)

    def test_213_replace_issue_labels(self, agent):
        """Agent replaces all labels on an issue."""
        result = agent.call("replace_issue_labels",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            labels=[self.label_id],
        )
        assert isinstance(result, list)

    def test_214_remove_issue_label(self, agent):
        """Agent removes a label from an issue."""
        agent.call("remove_issue_label",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            label_id=self.label_id,
        )

    def test_215_clear_issue_labels(self, agent):
        """Agent clears all labels from an issue."""
        # Re-add a label first
        agent.call("add_issue_labels",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            labels=[self.label_id],
        )
        agent.call("clear_issue_labels",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )

    def test_216_list_issue_dependencies(self, agent):
        """Agent lists issue dependencies (cleared in test_60)."""
        result = agent.call("list_issue_dependencies",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert result == []

    def test_218_issue_subscriptions(self, agent):
        """Agent manages issue subscriptions."""
        agent.call("subscribe_to_issue",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            user=ADMIN_USER,
        )
        subs = agent.call("list_issue_subscriptions",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )
        assert isinstance(subs, list)
        agent.call("unsubscribe_from_issue",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            user=ADMIN_USER,
        )

    def test_219_stopwatch_ops(self, agent):
        """Agent starts, stops, and deletes a stopwatch."""
        agent.call("start_stopwatch",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )
        agent.call("stop_stopwatch",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )
        # A second start is only possible because the first one was stopped.
        agent.call("start_stopwatch",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )
        agent.call("delete_stopwatch",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
        )

    def test_220_delete_tracked_time(self, agent):
        """Agent deletes a tracked time entry."""
        times = agent.call("list_tracked_times",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert len(times) == 1  # the hour logged in test_59
        agent.call("delete_tracked_time",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
            time_id=times[0]["id"],
        )
        assert agent.call("list_tracked_times",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        ) == []

    def test_221_remove_issue_reaction(self, agent):
        """Agent removes the reaction added in test_57."""
        agent.call("remove_issue_reaction",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
            reaction="+1",
        )
        assert agent.call("list_issue_reactions",
            owner=self.owner, repo=self.repo_name, index=self.issue_index,
        ) == []

    def test_222_remove_comment_reaction(self, agent):
        """Agent removes the comment reaction added in test_58."""
        agent.call("remove_comment_reaction",
            owner=self.owner, repo=self.repo_name,
            comment_id=self.issue_comment_id,
            reaction="heart",
        )
        assert agent.call("list_comment_reactions",
            owner=self.owner, repo=self.repo_name,
            comment_id=self.issue_comment_id,
        ) == []

    def test_223_unpin_issue(self, agent):
        """Agent unpins the issue pinned in test_61."""
        agent.call("unpin_issue",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert self._issue(agent)["pin_order"] == 0

    def test_224_delete_issue_comment(self, agent):
        """Agent deletes an issue comment."""
        # Create a new comment to delete
        result = agent.call("create_issue_comment",
            owner=self.owner, repo=self.repo_name,
            index=self.second_issue_index,
            body="Comment to delete",
        )
        agent.call("delete_issue_comment",
            owner=self.owner, repo=self.repo_name,
            comment_id=result["id"],
        )

    # ── 33. Labels & Milestones extended ──────────────────────

    def test_230_edit_repo_label(self, agent):
        """Agent edits a repo label."""
        result = agent.call("edit_repo_label",
            owner=self.owner, repo=self.repo_name,
            label_id=self.label_id,
            name="bug-updated",
            color="#e11d48",
        )
        assert result["name"] == "bug-updated"

    def test_231_get_milestone(self, agent):
        """Agent gets a milestone."""
        result = agent.call("get_milestone",
            owner=self.owner, repo=self.repo_name,
            milestone_id=self.milestone_id,
        )
        assert result["title"] == "v1.0"

    def test_232_edit_milestone(self, agent):
        """Agent edits a milestone."""
        result = agent.call("edit_milestone",
            owner=self.owner, repo=self.repo_name,
            milestone_id=self.milestone_id,
            description="Updated milestone description",
        )
        assert "Updated" in result["description"]

    def test_233_edit_org_label(self, agent):
        """Agent edits the label created in test_128."""
        labels = agent.call("list_org_labels", org=self.org_name)
        assert [label["name"] for label in labels] == ["priority:high"]
        label_id = labels[0]["id"]
        result = agent.call("edit_org_label",
            org=self.org_name,
            label_id=label_id,
            name="priority:critical",
            color="#dc2626",
        )
        assert result["name"] == "priority:critical"
        TestAgentWorkflow._org_label_id = label_id

    def test_234_delete_org_label(self, agent):
        """Agent deletes an org label."""
        agent.call("delete_org_label",
            org=self.org_name,
            label_id=TestAgentWorkflow._org_label_id,
        )
        assert agent.call("list_org_labels", org=self.org_name) == []

    # ── 34. PR extended ───────────────────────────────────────

    def test_240_pr_extended_ops(self, agent):
        """Agent tests PR edit, reviewers, review comments, and update branch."""
        # Create a new branch and PR for extended testing
        agent.call("create_branch",
            owner=self.owner, repo=self.repo_name,
            new_branch_name="feature/pr-test-2",
            old_branch_name="main",
        )
        agent.call("create_file",
            owner=self.owner, repo=self.repo_name,
            filepath="src/pr_test2.py",
            content='print("PR test 2")\n',
            message="Add pr_test2.py",
            branch="feature/pr-test-2",
        )
        pr = agent.call("create_pull_request",
            owner=self.owner, repo=self.repo_name,
            title="PR for extended testing",
            head="feature/pr-test-2",
            base="main",
            body="Testing PR extended ops",
        )
        pr_idx = pr["number"]
        TestAgentWorkflow._pr2_index = pr_idx

        # edit_pull_request
        edited = agent.call("edit_pull_request",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            title="PR for extended testing [edited]",
        )
        assert "edited" in edited["title"]

        requested = agent.call("request_pull_reviewers",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            reviewers=["testuser2"],
        )
        assert [r["user"]["login"] for r in requested] == ["testuser2"]

        agent.call("remove_pull_reviewers",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            reviewers=["testuser2"],
        )

        # Gitea answers 500 "HeadBranch is up to date" when there is nothing to
        # pull in, so move base ahead first.
        agent.call("create_file",
            owner=self.owner, repo=self.repo_name,
            filepath="src/base_moved.py",
            content='print("base moved")\n',
            message="Move main ahead of the PR branch",
        )
        head_before = agent.call("get_branch",
            owner=self.owner, repo=self.repo_name, branch="feature/pr-test-2",
        )["commit"]["id"]
        agent.call("update_pull_request_branch",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
        )
        head_after = agent.call("get_branch",
            owner=self.owner, repo=self.repo_name, branch="feature/pr-test-2",
        )["commit"]["id"]
        assert head_after != head_before

    def test_241_pr_review_extended(self, agent):
        """Agent tests submit/dismiss/delete review and review comments."""
        pr_idx = TestAgentWorkflow._pr2_index

        # Omitting `event` leaves the review PENDING, which is the only state
        # submit_pull_review accepts (it 422s on anything already submitted).
        review = agent.call("create_pull_review",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            body="Review for testing",
        )
        review_id = review["id"]
        assert review["state"] == "PENDING"

        comments = agent.call("get_pull_review_comments",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            review_id=review_id,
        )
        assert comments == []

        submitted = agent.call("submit_pull_review",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            review_id=review_id,
            body="Submitted",
            event="COMMENT",
        )
        assert submitted["state"] == "COMMENT"

        # Only APPROVED / REQUEST_CHANGES reviews are dismissible, and Gitea
        # refuses both on your own PR — so 403 is the reachable contract here.
        with gitea_error(403, "not need to dismiss this review"):
            agent.call("dismiss_pull_review",
                owner=self.owner, repo=self.repo_name,
                index=pr_idx,
                review_id=review_id,
                message="Dismissing for test",
            )

        agent.call("delete_pull_review",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            review_id=review_id,
        )
        assert review_id not in [
            r["id"] for r in agent.call("list_pull_reviews",
                owner=self.owner, repo=self.repo_name, index=pr_idx,
            )
        ]

    def test_242_cleanup_pr2(self, agent):
        """Clean up PR2 by merging it."""
        pr_idx = TestAgentWorkflow._pr2_index
        wait_for_pr_mergeable(agent, self.owner, self.repo_name, pr_idx)
        agent.call("merge_pull_request",
            owner=self.owner, repo=self.repo_name,
            index=pr_idx,
            merge_type="merge",
            delete_branch_after_merge=True,
        )
        pr = agent.call("get_pull_request",
            owner=self.owner, repo=self.repo_name, index=pr_idx,
        )
        assert pr["merged"] is True

    # ── 35. Releases & Tags extended ──────────────────────────

    def test_250_list_releases(self, agent):
        """Agent lists releases."""
        result = agent.call("list_releases",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, list)

    def test_251_delete_release(self, agent):
        """Agent deletes a release."""
        agent.call("delete_release",
            owner=self.owner, repo=self.repo_name,
            release_id=self.release_id,
        )
        with gitea_error(404, "not found"):
            agent.call("get_release",
                owner=self.owner, repo=self.repo_name,
                release_id=self.release_id,
            )

    def test_252_delete_tag(self, agent):
        """Agent deletes a tag."""
        agent.call("delete_tag",
            owner=self.owner, repo=self.repo_name,
            tag=self.tag_name,
        )
        assert self.tag_name not in [
            t["name"] for t in agent.call("list_tags",
                owner=self.owner, repo=self.repo_name,
            )
        ]

    # ── 36. Edit tag protection ───────────────────────────────

    def test_253_edit_tag_protection(self, agent):
        """Agent edits tag protection."""
        tp = agent.call("create_tag_protection",
            owner=self.owner, repo=self.repo_name,
            name_pattern="release-*",
            whitelist_usernames=[ADMIN_USER],
        )
        tp_id = tp["id"]
        edited = agent.call("edit_tag_protection",
            owner=self.owner, repo=self.repo_name,
            tag_protection_id=tp_id,
            name_pattern="release-v*",
        )
        assert edited["name_pattern"] == "release-v*"
        agent.call("delete_tag_protection",
            owner=self.owner, repo=self.repo_name,
            tag_protection_id=tp_id,
        )

    # ── 37. Webhooks extended ─────────────────────────────────

    def test_260_edit_repo_webhook(self, agent):
        """Agent edits a repo webhook."""
        hook = agent.call("create_repo_webhook",
            owner=self.owner, repo=self.repo_name,
            config={"url": "https://httpbin.org/post", "content_type": "json"},
            events=["push"],
        )
        hook_id = hook["id"]
        result = agent.call("edit_repo_webhook",
            owner=self.owner, repo=self.repo_name,
            hook_id=hook_id,
            events=["push", "issues"],
        )
        # Gitea expands 'issues' into its issue_* sub-events.
        assert {"push", "issues"} <= set(result["events"])

        # Only queues the delivery, so an unreachable target URL is still 'ok'.
        agent.call("test_repo_webhook",
            owner=self.owner, repo=self.repo_name,
            hook_id=hook_id,
        )

        agent.call("delete_repo_webhook",
            owner=self.owner, repo=self.repo_name,
            hook_id=hook_id,
        )

    def test_261_edit_org_webhook(self, agent):
        """Agent edits an org webhook."""
        hook = agent.call("create_org_webhook",
            org=self.org_name,
            config={"url": "https://httpbin.org/post", "content_type": "json"},
            events=["push"],
        )
        hook_id = hook["id"]
        result = agent.call("edit_org_webhook",
            org=self.org_name,
            hook_id=hook_id,
            events=["push", "repository"],
        )
        assert sorted(result["events"]) == ["push", "repository"]
        agent.call("delete_org_webhook", org=self.org_name, hook_id=hook_id)

    # ── 38. Deploy keys ───────────────────────────────────────

    def test_270_deploy_keys(self, agent, ssh_pubkey):
        """Agent manages deploy keys."""
        pub_key = ssh_pubkey

        result = agent.call("create_deploy_key",
            owner=self.owner, repo=self.repo_name,
            title="test-deploy-key",
            key=pub_key,
            read_only=True,
        )
        key_id = result["id"]

        fetched = agent.call("get_deploy_key",
            owner=self.owner, repo=self.repo_name,
            key_id=key_id,
        )
        assert fetched["title"] == "test-deploy-key"

        keys = agent.call("list_deploy_keys",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(keys, list)

        agent.call("delete_deploy_key",
            owner=self.owner, repo=self.repo_name,
            key_id=key_id,
        )

    # ── 39. SSH keys ──────────────────────────────────────────

    def test_271_ssh_keys(self, agent, ssh_pubkey):
        """Agent manages user SSH keys."""
        pub_key = ssh_pubkey

        result = agent.call("create_ssh_key",
            title="test-ssh-key",
            key=pub_key,
        )
        key_id = result["id"]

        keys = agent.call("list_ssh_keys")
        assert isinstance(keys, list)

        agent.call("delete_ssh_key", key_id=key_id)

    # ── 40. GPG keys ─────────────────────────────────────────

    def test_272_gpg_keys(self, agent):
        """Agent lists GPG keys and gets rejected on a malformed one."""
        assert agent.call("list_gpg_keys") == []

        with gitea_error(422, "failed to parse gpg key"):
            agent.call("create_gpg_key",
                armored_public_key="not-a-real-key",
            )

        # Gitea's GPG delete is idempotent — unknown ids answer 204, not 404.
        agent.call("delete_gpg_key", key_id=999999)

    # ── 41. Wiki extended ─────────────────────────────────────

    def test_280_delete_wiki_page(self, agent):
        """Agent deletes a wiki page."""
        # Create a page to delete
        agent.call("create_wiki_page",
            owner=self.owner, repo=self.repo_name,
            title="PageToDelete",
            content="This page will be deleted.\n",
            message="Create page to delete",
        )
        agent.call("delete_wiki_page",
            owner=self.owner, repo=self.repo_name,
            page_name="PageToDelete",
        )

    # ── 42. File ops extended ─────────────────────────────────

    def test_290_delete_file(self, agent):
        """Agent deletes a file."""
        # Create a file to delete
        agent.call("create_file",
            owner=self.owner, repo=self.repo_name,
            filepath="temp_delete_me.txt",
            content="Delete me\n",
            message="Add temp file",
        )
        file_info = agent.call("get_file_content",
            owner=self.owner, repo=self.repo_name,
            filepath="temp_delete_me.txt",
        )
        agent.call("delete_file",
            owner=self.owner, repo=self.repo_name,
            filepath="temp_delete_me.txt",
            message="Delete temp file",
            sha=file_info["sha"],
        )

    def test_291_delete_branch(self, agent):
        """Agent deletes a branch."""
        agent.call("create_branch",
            owner=self.owner, repo=self.repo_name,
            new_branch_name="branch-to-delete",
            old_branch_name="main",
        )
        agent.call("delete_branch",
            owner=self.owner, repo=self.repo_name,
            branch="branch-to-delete",
        )

    # ── 43. Notifications extended ────────────────────────────

    def test_300_mark_notifications_read(self, agent):
        """Agent marks notifications as read — nothing to mark, so no threads
        come back (Gitea never notifies the user who acted)."""
        assert agent.call("mark_notifications_read") == []

    def test_301_mark_repo_notifications_read(self, agent):
        """Agent marks repo notifications as read."""
        assert agent.call("mark_repo_notifications_read",
            owner=self.owner, repo=self.repo_name,
        ) == []

    def test_302_notification_thread(self, agent):
        """Agent reads notification threads."""
        # Single-actor instance: the only actor is the one who caused every
        # event, and Gitea skips the doer, so the inbox stays empty.
        assert agent.call("list_notifications") == []

        with gitea_error(404, "notification does not exist"):
            agent.call("get_notification_thread", thread_id=999999)
        with gitea_error(404, "notification does not exist"):
            agent.call("mark_notification_read", thread_id=999999)

    # ── 44. User mgmt extended ────────────────────────────────

    def test_310_follow_unfollow(self, agent):
        """Agent follows and unfollows a user."""
        agent.call("follow_user", username="testuser2")
        agent.call("check_user_following",
            username=ADMIN_USER, target="testuser2",
        )
        agent.call("unfollow_user", username="testuser2")
        with gitea_error(404, "not found"):
            agent.call("check_user_following",
                username=ADMIN_USER, target="testuser2",
            )

    def test_311_block_unblock(self, agent):
        """Agent blocks and unblocks a user."""
        agent.call("block_user", username="testuser2")
        agent.call("unblock_user", username="testuser2")

    def test_312_list_user_heatmap(self, agent):
        """Agent lists user heatmap."""
        result = agent.call("list_user_heatmap", username=ADMIN_USER)
        assert isinstance(result, list)

    def test_313_update_user_settings(self, agent):
        """Agent updates user settings."""
        result = agent.call("update_user_settings",
            full_name="Test Admin Updated",
            language="en-US",
        )
        assert result["full_name"] == "Test Admin Updated"
        assert result["language"] == "en-US"

    def test_314_add_delete_user_email(self, agent):
        """Agent adds and deletes a user email."""
        added = agent.call("add_user_email",
            emails=["extra@test.local"],
        )
        assert "extra@test.local" in [e["email"] for e in added]

        agent.call("delete_user_email",
            emails=["extra@test.local"],
        )
        assert "extra@test.local" not in [
            e["email"] for e in agent.call("list_user_emails")
        ]

    def test_315_list_followers_following(self, agent):
        """Agent lists followers and following."""
        followers = agent.call("list_followers", username=ADMIN_USER)
        assert isinstance(followers, list)
        following = agent.call("list_following", username=ADMIN_USER)
        assert isinstance(following, list)

    def test_316_list_user_orgs(self, agent):
        """Agent lists user's organizations."""
        result = agent.call("list_user_orgs", username=ADMIN_USER)
        assert isinstance(result, list)

    # ── 45. User actions secrets/variables ────────────────────

    def test_320_user_action_variables(self, agent):
        """Agent manages user-level action variables."""
        agent.call("create_user_action_variable",
            variable_name="USER_TEST_VAR",
            value="user_value",
        )
        var = agent.call("get_user_action_variable",
            variable_name="USER_TEST_VAR",
        )
        assert var["data"] == "user_value"

        agent.call("update_user_action_variable",
            variable_name="USER_TEST_VAR",
            value="updated_user_value",
        )
        assert agent.call("get_user_action_variable",
            variable_name="USER_TEST_VAR",
        )["data"] == "updated_user_value"

        variables = agent.call("list_user_action_variables")
        assert [v["name"] for v in variables] == ["USER_TEST_VAR"]

        agent.call("delete_user_action_variable",
            variable_name="USER_TEST_VAR",
        )
        assert agent.call("list_user_action_variables") == []

    def test_321_user_action_secrets(self, agent):
        """Agent manages user-level action secrets.

        Create/delete only — Gitea exposes a list endpoint for org and repo
        secrets but none for user secrets, so there is no op to call."""
        agent.call("create_user_action_secret",
            secret_name="USER_SECRET",
            data="secret_data",
        )
        agent.call("delete_user_action_secret",
            secret_name="USER_SECRET",
        )

    # ── 46. Org extended ──────────────────────────────────────

    def test_330_org_repos(self, agent):
        """Agent creates and lists org repos."""
        result = agent.call("create_org_repo",
            org=self.org_name,
            name="org-test-repo",
            description="Org test repo",
            auto_init=True,
        )
        assert result["name"] == "org-test-repo"

        repos = agent.call("list_org_repos", org=self.org_name)
        assert isinstance(repos, list)
        assert any("org-test-repo" in r.get("full_name", r.get("name", "")) for r in repos)

        # Same op that 405s on a user-owned repo in test_183.
        teams = agent.call("list_repo_teams",
            owner=self.org_name, repo="org-test-repo",
        )
        assert [t["name"] for t in teams] == ["Owners"]

    def test_331_org_public_member_ops(self, agent):
        """Agent manages org public membership."""
        agent.call("set_org_public_member",
            org=self.org_name, username=ADMIN_USER,
        )
        agent.call("check_org_public_member",
            org=self.org_name, username=ADMIN_USER,
        )
        agent.call("remove_org_public_member",
            org=self.org_name, username=ADMIN_USER,
        )
        with gitea_error(404, "not found"):
            agent.call("check_org_public_member",
                org=self.org_name, username=ADMIN_USER,
            )

    # ── 47. Teams extended ────────────────────────────────────

    def test_340_edit_team(self, agent):
        """Agent edits a team."""
        result = agent.call("edit_team",
            team_id=self.team_id,
            description="Updated team description",
        )
        assert result["description"] == "Updated team description"

    def test_341_team_repos(self, agent):
        """Agent manages team repos."""
        agent.call("add_team_repo",
            team_id=self.team_id,
            org=self.org_name,
            repo="org-test-repo",
        )
        repos = agent.call("list_team_repos", team_id=self.team_id)
        assert [r["full_name"] for r in repos] == [f"{self.org_name}/org-test-repo"]

        checked = agent.call("check_team_repo",
            team_id=self.team_id,
            org=self.org_name,
            repo="org-test-repo",
        )
        assert checked["full_name"] == f"{self.org_name}/org-test-repo"

        agent.call("remove_team_repo",
            team_id=self.team_id,
            org=self.org_name,
            repo="org-test-repo",
        )
        assert agent.call("list_team_repos", team_id=self.team_id) == []

    def test_342_remove_team_member(self, agent):
        """Agent removes a team member."""
        agent.call("remove_team_member",
            team_id=self.team_id,
            username=ADMIN_USER,
        )
        assert agent.call("list_team_members", team_id=self.team_id) == []

    # ── 48. Admin extended ────────────────────────────────────

    def test_350_admin_list_orgs(self, agent):
        """Agent lists all orgs (admin)."""
        result = agent.call("admin_list_orgs")
        assert isinstance(result, list)

    def test_351_admin_create_org(self, agent):
        """Agent creates an org via admin API.

        `username` is the new org's login, `owner_name` the existing user that
        will own it — the path segment is the owner, not the org."""
        result = agent.call("admin_create_org",
            username="admin-created-org",
            owner_name=ADMIN_USER,
            full_name="Admin Created Org",
            visibility="public",
        )
        assert result["username"] == "admin-created-org"
        agent.call("delete_org", org="admin-created-org")

    def test_352_admin_create_repo_for_user(self, agent):
        """Agent creates a repo for another user (admin)."""
        result = agent.call("admin_create_repo_for_user",
            username="testuser2",
            name="admin-created-repo",
            description="Created by admin",
            auto_init=True,
        )
        assert result["name"] == "admin-created-repo"
        assert result["owner"]["login"] == "testuser2"
        agent.call("delete_repo", owner="testuser2", repo="admin-created-repo")

    def test_353_admin_rename_user(self, agent):
        """Agent renames a user (admin)."""
        agent.call("admin_create_user",
            username="temprename",
            email="temprename@test.local",
            password="testuser1234",
            must_change_password=False,
        )
        agent.call("admin_rename_user",
            username="temprename",
            new_username="temprenamed",
        )
        assert agent.call("get_user", username="temprenamed")["login"] == "temprenamed"
        agent.call("admin_delete_user", username="temprenamed", purge=True)

    def test_354_admin_user_public_keys(self, agent, ssh_pubkey):
        """Agent manages user public keys via admin API."""
        result = agent.call("admin_create_user_public_key",
            username="testuser2",
            title="admin-test-key",
            key=ssh_pubkey,
        )
        assert result["title"] == "admin-test-key"
        agent.call("admin_delete_user_public_key",
            username="testuser2",
            key_id=result["id"],
        )

    def test_355_admin_unadopted_repos(self, agent):
        """Agent lists unadopted repos (admin).

        Nothing is orphaned: every repo on disk has a DB row, and planting a
        bare repo would need filesystem access inside the container. So adopt
        and delete-unadopted only have their 404 contract to assert."""
        assert agent.call("admin_list_unadopted_repos") == []

        with gitea_error(404, "not found"):
            agent.call("admin_adopt_repo",
                owner=ADMIN_USER, repo="nonexistent-repo",
            )
        with gitea_error(404, "not found"):
            agent.call("admin_delete_unadopted_repo",
                owner=ADMIN_USER, repo="nonexistent-repo",
            )

    def test_356_admin_run_cron_job(self, agent):
        """Agent runs a cron job (admin)."""
        crons = agent.call("admin_list_cron_jobs")
        assert "update_mirrors" in [c["name"] for c in crons]
        agent.call("admin_run_cron_job", task_name=crons[0]["name"])

    def test_357_admin_search_emails(self, agent):
        """Agent searches emails (admin)."""
        result = agent.call("admin_search_emails", query="test")
        assert isinstance(result, list)

    # ── 49. Milestones cleanup (delete) ───────────────────────

    def test_360_delete_milestone(self, agent):
        """Agent deletes a milestone."""
        agent.call("delete_milestone",
            owner=self.owner, repo=self.repo_name,
            milestone_id=self.milestone_id,
        )
        assert agent.call("list_milestones",
            owner=self.owner, repo=self.repo_name, state="all",
        ) == []

    def test_361_delete_repo_label(self, agent):
        """Agent deletes a repo label."""
        agent.call("delete_repo_label",
            owner=self.owner, repo=self.repo_name,
            label_id=self.label_id,
        )
        assert agent.call("list_repo_labels",
            owner=self.owner, repo=self.repo_name,
        ) == []

    # ── 50. Packages ──────────────────────────────────────────

    def test_370_packages(self, agent):
        """Agent reads and deletes a package.

        The upload API lives outside /api/v1 and has no MCP op, so the package
        is published over raw HTTP first (see conftest)."""
        upload_generic_package("agent-pkg", "1.0.0", "hello.txt", b"hello")

        packages = agent.call("list_packages", owner=self.owner)
        assert [p["name"] for p in packages] == ["agent-pkg"]

        pkg = agent.call("get_package",
            owner=self.owner, type="generic", name="agent-pkg", version="1.0.0",
        )
        assert pkg["version"] == "1.0.0"

        files = agent.call("list_package_files",
            owner=self.owner, type="generic", name="agent-pkg", version="1.0.0",
        )
        assert [f["name"] for f in files] == ["hello.txt"]

        versions = agent.call("list_package_versions",
            owner=self.owner, type="generic", name="agent-pkg",
        )
        assert [v["version"] for v in versions] == ["1.0.0"]

        agent.call("delete_package",
            owner=self.owner, type="generic", name="agent-pkg", version="1.0.0",
        )
        with gitea_error(404, "package does not exist"):
            agent.call("get_package",
                owner=self.owner, type="generic", name="agent-pkg", version="1.0.0",
            )

    # ── 51. Workflows/CI extended ─────────────────────────────

    def test_380_workflow_ops(self, agent):
        """Agent reads the workflow, the run dispatched in test_113, and its job."""
        workflow = agent.call("get_workflow",
            owner=self.owner, repo=self.repo_name,
            workflow_id="test.yml",
        )
        assert workflow["path"] == ".gitea/workflows/test.yml"
        assert workflow["state"] == "active"

        run_id = TestAgentWorkflow.workflow_run_id
        run = agent.call("get_workflow_run",
            owner=self.owner, repo=self.repo_name, run_id=run_id,
        )
        assert run["id"] == run_id

        jobs = agent.call("list_workflow_run_jobs",
            owner=self.owner, repo=self.repo_name, run_id=run_id,
        )
        assert [j["name"] for j in jobs] == ["test"]
        job_id = jobs[0]["id"]

        job = agent.call("get_workflow_job",
            owner=self.owner, repo=self.repo_name, job_id=job_id,
        )
        assert job["id"] == job_id
        assert job["run_id"] == run_id

        # Logs only exist once a runner picks the job up; none is registered.
        with gitea_error(404, "job not started"):
            agent.call("get_workflow_job_logs",
                owner=self.owner, repo=self.repo_name, job_id=job_id,
            )

    # ── 52. Runners ───────────────────────────────────────────

    # No act_runner is registered in the test instance, so every scope lists
    # zero runners and get/delete of a runner id can only answer 404.

    def test_390_repo_runners(self, agent):
        """Agent lists repo runners and creates registration token."""
        result = agent.call("list_repo_runners",
            owner=self.owner, repo=self.repo_name,
        )
        assert result == {"runners": [], "total_count": 0}

        token = agent.call("create_repo_runner_token",
            owner=self.owner, repo=self.repo_name,
        )
        assert token["token"]

        with gitea_error(404, "Runner not found"):
            agent.call("get_repo_runner",
                owner=self.owner, repo=self.repo_name, runner_id=99999,
            )
        with gitea_error(404, "Runner not found"):
            agent.call("delete_repo_runner",
                owner=self.owner, repo=self.repo_name, runner_id=99999,
            )

    def test_391_org_runners(self, agent):
        """Agent lists org runners and creates registration token."""
        assert agent.call("list_org_runners", org=self.org_name) == {
            "runners": [], "total_count": 0,
        }
        assert agent.call("create_org_runner_token", org=self.org_name)["token"]

        with gitea_error(404, "Runner not found"):
            agent.call("get_org_runner", org=self.org_name, runner_id=99999)
        with gitea_error(404, "Runner not found"):
            agent.call("delete_org_runner", org=self.org_name, runner_id=99999)

    def test_392_user_runners(self, agent):
        """Agent lists user runners and creates registration token."""
        assert agent.call("list_user_runners") == {"runners": [], "total_count": 0}
        assert agent.call("create_user_runner_token")["token"]

        with gitea_error(404, "Runner not found"):
            agent.call("get_user_runner", runner_id=99999)
        with gitea_error(404, "Runner not found"):
            agent.call("delete_user_runner", runner_id=99999)

    def test_393_admin_runners(self, agent):
        """Agent lists admin runners and creates registration token."""
        assert agent.call("list_admin_runners") == {"runners": [], "total_count": 0}
        assert agent.call("create_admin_runner_token")["token"]

        with gitea_error(404, "Runner not found"):
            agent.call("get_admin_runner", runner_id=99999)
        with gitea_error(404, "Runner not found"):
            agent.call("delete_admin_runner", runner_id=99999)

    # ── 53. Org member removal (before cleanup) ───────────────

    def test_394_remove_org_member(self, agent):
        """Agent removes an org member."""
        # Team membership is what makes testuser2 an org member.
        agent.call("add_team_member",
            team_id=self.team_id,
            username="testuser2",
        )
        assert "testuser2" in [
            m["login"] for m in agent.call("list_org_members", org=self.org_name)
        ]
        agent.call("remove_org_member",
            org=self.org_name,
            username="testuser2",
        )
        assert "testuser2" not in [
            m["login"] for m in agent.call("list_org_members", org=self.org_name)
        ]

    # ── 54. Template repo (create from template) ──────────────

    def test_395_create_repo_from_template(self, agent):
        """Agent creates a repo from a template."""
        assert agent.call("edit_repo",
            owner=self.owner, repo=self.repo_name,
            template=True,
        )["template"] is True

        # Gitea 422s with "must select at least one template item" unless at
        # least one of git_content/topics/labels is requested.
        result = agent.call("create_repo_from_template",
            template_owner=self.owner,
            template_repo=self.repo_name,
            name="from-template-repo",
            owner=self.owner,
            description="Created from template",
            git_content=True,
        )
        assert result["name"] == "from-template-repo"
        agent.call("delete_repo", owner=self.owner, repo="from-template-repo")

        assert agent.call("edit_repo",
            owner=self.owner, repo=self.repo_name,
            template=False,
        )["template"] is False

    # ── 55. Transfer repo ─────────────────────────────────────

    def test_396_transfer_repo(self, agent):
        """Agent transfers a repo."""
        agent.call("create_repo",
            name="repo-to-transfer",
            description="Will be transferred",
            auto_init=True,
        )
        transferred = agent.call("transfer_repo",
            owner=self.owner,
            repo="repo-to-transfer",
            new_owner=self.org_name,
        )
        assert transferred["owner"]["login"] == self.org_name
        agent.call("delete_repo", owner=self.org_name, repo="repo-to-transfer")

    # ── 56. Org repo cleanup ──────────────────────────────────

    def test_397_cleanup_org_repo(self, agent):
        """Clean up org-test-repo."""
        agent.call("delete_repo", owner=self.org_name, repo="org-test-repo")

    # ── 30. Cleanup ──────────────────────────────────────────

    def test_900_cleanup_org(self, agent):
        """Agent cleans up the organization."""
        agent.call("delete_team", team_id=self.team_id)
        agent.call("delete_org", org=self.org_name)
        with gitea_error(404, "does not exist"):
            agent.call("get_org", org=self.org_name)

    def test_901_cleanup_user(self, agent):
        """Agent cleans up the test user."""
        agent.call("admin_delete_user", username="testuser2", purge=True)
        with gitea_error(404, "does not exist"):
            agent.call("get_user", username="testuser2")

    def test_999_delete_repo(self, agent):
        """Agent deletes the test repo."""
        agent.call("delete_repo", owner=self.owner, repo=self.repo_name)
        with gitea_error(404, "not found"):
            agent.call("get_repo", owner=self.owner, repo=self.repo_name)

    def test_final_print_log(self, agent):
        """Print the full agent call log."""
        print(f"\n{'='*60}")
        print(f"Agent made {len(agent.call_log)} MCP tool calls total")
        print(f"{'='*60}")
        tools_used = {e["tool"] for e in agent.call_log}
        print(f"Unique tools used: {len(tools_used)}")
        for tool in sorted(tools_used):
            count = sum(1 for e in agent.call_log if e["tool"] == tool)
            print(f"  {tool}: {count}x")
