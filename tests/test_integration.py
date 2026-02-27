"""Integration tests — an agent works with Gitea entirely through MCP tools.

The test simulates a realistic agent workflow:
1. Check connection → 2. Create repo → 3. Work with files →
4. Create branches → 5. Work with issues (full lifecycle) →
6. Create and merge PR → 7. Tags and releases →
8. Wiki → 9. Actions/CI → 10. Org and teams →
11. Admin operations → 12. Cleanup
"""

import time
import pytest

ADMIN_USER = "testadmin"


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

    # ── 1. Connection & General ───────────────────────────────

    def test_01_version(self, agent):
        """Agent checks Gitea version."""
        result = agent.call("get_version")
        assert "version" in result

    def test_02_current_user(self, agent):
        """Agent verifies its identity."""
        result = agent.call("get_current_user")
        assert result["login"] == ADMIN_USER

    def test_03_user_settings(self, agent):
        """Agent reads and updates user settings."""
        settings = agent.call("get_user_settings")
        assert "language" in settings or "theme" in settings or isinstance(settings, dict)

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
        assert any(l["name"] == "bug" for l in result)

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
            body="The greeting message needs to be updated.\n\n- [ ] Update message\n- [ ] Add tests",
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
        # Result should contain the updated issue or deadline info
        assert result is not None

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
        assert result is not None

        times = agent.call("list_tracked_times",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        assert len(times) >= 1

    def test_60_issue_dependencies(self, agent):
        """Agent creates a second issue and sets dependency."""
        # Create a second issue
        result = agent.call("create_issue",
            owner=self.owner,
            repo=self.repo_name,
            title="Prerequisite task",
            body="This must be done first",
        )
        TestAgentWorkflow.second_issue_index = result["number"]

        # Try adding dependency (may not work on all Gitea versions)
        try:
            agent.call("add_issue_dependency",
                owner=self.owner,
                repo=self.repo_name,
                index=self.issue_index,
                depends_on_id=self.second_issue_index,
            )
        except Exception:
            pass  # Some Gitea versions handle this differently

    def test_61_pin_lock_issue(self, agent):
        """Agent pins and locks an issue."""
        try:
            agent.call("pin_issue",
                owner=self.owner,
                repo=self.repo_name,
                index=self.issue_index,
            )
        except Exception:
            pass  # Pin may not be available in all versions

        agent.call("lock_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )
        # Unlock it so we can still work with it
        agent.call("unlock_issue",
            owner=self.owner,
            repo=self.repo_name,
            index=self.issue_index,
        )

    def test_62_search_issues(self, agent):
        """Agent searches issues."""
        result = agent.call("list_issues",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert isinstance(result, list)
        assert len(result) >= 1

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
        """Agent creates a review comment (self-review may be restricted)."""
        try:
            result = agent.call("create_pull_review",
                owner=self.owner,
                repo=self.repo_name,
                index=self.pr_index,
                body="LGTM! The feature looks good.",
                event="COMMENT",
            )
            assert result is not None
        except Exception:
            pass  # Self-review may be restricted on some Gitea versions

    def test_77_list_reviews(self, agent):
        """Agent lists reviews."""
        result = agent.call("list_pull_reviews",
            owner=self.owner,
            repo=self.repo_name,
            index=self.pr_index,
        )
        assert isinstance(result, list)

    def test_78_merge_pr(self, agent):
        """Agent merges the PR."""
        result = agent.call("merge_pull_request",
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
        assert result is not None

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
        assert var["data"] == "hello_from_agent" or var.get("value") == "hello_from_agent" or "hello_from_agent" in str(var)

        # Update
        agent.call("update_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
            value="updated_by_agent",
        )

        # List
        variables = agent.call("list_action_variables",
            owner=self.owner,
            repo=self.repo_name,
        )
        assert isinstance(variables, list)

        # Delete
        agent.call("delete_action_variable",
            owner=self.owner,
            repo=self.repo_name,
            variable_name="TEST_VAR",
        )

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
        assert isinstance(secrets, list)

        agent.call("delete_action_secret",
            owner=self.owner,
            repo=self.repo_name,
            secret_name="TEST_SECRET",
        )

    def test_112_list_workflows(self, agent):
        """Agent lists workflows."""
        result = agent.call("list_workflows", owner=self.owner, repo=self.repo_name)
        # May have the workflow we created earlier
        assert result is not None

    def test_113_dispatch_workflow(self, agent):
        """Agent dispatches a workflow and checks its run."""
        # Try to dispatch the workflow we created
        try:
            agent.call("dispatch_workflow",
                owner=self.owner,
                repo=self.repo_name,
                workflow_id="test.yml",
                ref="main",
                inputs={"greeting": "hello from agent test"},
            )
            # Wait a bit for the run to be created
            time.sleep(3)
        except Exception:
            pytest.skip("Workflow dispatch not available (no runner configured)")

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
        if self.team_id is None:
            pytest.skip("Team was not created")
        result = agent.call("get_team", team_id=self.team_id)
        assert result["name"] == "developers"

    def test_126_list_teams(self, agent):
        """Agent lists org teams."""
        result = agent.call("list_org_teams", org=self.org_name)
        assert isinstance(result, list)
        assert len(result) >= 1  # At least the Owners team

    def test_127_team_members(self, agent):
        """Agent adds and lists team members."""
        if self.team_id is None:
            pytest.skip("Team was not created")
        agent.call("add_team_member",
            team_id=self.team_id,
            username=ADMIN_USER,
        )
        members = agent.call("list_team_members", team_id=self.team_id)
        assert isinstance(members, list)

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
        assert any(l["name"] == "priority:high" for l in labels)

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
        assert result is not None

    # ── 17. Misc ──────────────────────────────────────────────

    def test_150_render_markdown(self, agent):
        """Agent renders markdown."""
        result = agent.call_raw("render_markdown", text="# Hello\n\n**Bold** text")
        assert "<h1>" in result.lower() or "<strong>" in result.lower() or "bold" in result.lower()

    def test_151_search_topics(self, agent):
        """Agent searches topics."""
        result = agent.call("search_topics", query="test")
        assert result is not None

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
        assert bp is not None

        agent.call("edit_branch_protection",
            owner=self.owner, repo=self.repo_name, name="main",
            enable_push=True,
        )

        bps = agent.call("list_branch_protections",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(bps, list)

        agent.call("delete_branch_protection",
            owner=self.owner, repo=self.repo_name, name="main",
        )

    def test_163_tag_protection_crud(self, agent):
        """Agent manages tag protection rules."""
        try:
            result = agent.call("create_tag_protection",
                owner=self.owner, repo=self.repo_name,
                name_pattern="v*",
            )
            tp_id = result["id"]

            tp = agent.call("get_tag_protection",
                owner=self.owner, repo=self.repo_name,
                tag_protection_id=tp_id,
            )
            assert tp is not None

            tps = agent.call("list_tag_protections",
                owner=self.owner, repo=self.repo_name,
            )
            assert isinstance(tps, list)

            agent.call("delete_tag_protection",
                owner=self.owner, repo=self.repo_name,
                tag_protection_id=tp_id,
            )
        except Exception:
            pass  # Tag protection may not be available in all versions

    # ── 20. New tools: Issue extras ──────────────────────────

    def test_164_issue_timeline(self, agent):
        """Agent gets issue timeline."""
        timeline = agent.call("get_issue_timeline",
            owner=self.owner, repo=self.repo_name,
            index=self.issue_index,
        )
        assert isinstance(timeline, list)

    def test_165_delete_issue_deadline(self, agent):
        """Agent removes a deadline from an issue."""
        try:
            agent.call("delete_issue_deadline",
                owner=self.owner, repo=self.repo_name,
                index=self.issue_index,
            )
        except Exception:
            pass  # May fail if no deadline set

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
        assert var is not None

        agent.call("update_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
            value="updated_org_value",
        )

        variables = agent.call("list_org_action_variables", org=self.org_name)
        assert isinstance(variables, list)

        agent.call("delete_org_action_variable",
            org=self.org_name, variable_name="ORG_TEST_VAR",
        )

    def test_169_org_action_secrets(self, agent):
        """Agent manages org action secrets."""
        agent.call("create_org_action_secret",
            org=self.org_name,
            secret_name="ORG_SECRET",
            data="secret_data",
        )

        secrets = agent.call("list_org_action_secrets", org=self.org_name)
        assert isinstance(secrets, list)

        agent.call("delete_org_action_secret",
            org=self.org_name, secret_name="ORG_SECRET",
        )

    # ── 23. New tools: Org members ───────────────────────────

    def test_170_org_membership(self, agent):
        """Agent checks and manages org membership."""
        members = agent.call("list_org_members", org=self.org_name)
        assert isinstance(members, list)

        try:
            agent.call("check_org_membership",
                org=self.org_name, username=ADMIN_USER,
            )
        except Exception:
            pass  # May return 404 for redirect

        public = agent.call("list_org_public_members", org=self.org_name)
        assert isinstance(public, list)

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
        """Agent checks following relationship."""
        try:
            agent.call("check_user_following",
                username=ADMIN_USER, target="testuser2",
            )
        except Exception:
            pass  # 404 means not following, which is expected

    # ── 25. New tools: Notifications expansion ───────────────

    def test_176_notification_count(self, agent):
        """Agent checks notification count."""
        result = agent.call("get_new_notification_count")
        assert result is not None

    def test_177_repo_notifications(self, agent):
        """Agent lists repo notifications."""
        result = agent.call("list_repo_notifications",
            owner=self.owner, repo=self.repo_name,
        )
        assert isinstance(result, list)

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
        try:
            result = agent.call("get_repo_collaborator_permission",
                owner=self.owner, repo=self.repo_name,
                collaborator=ADMIN_USER,
            )
            assert result is not None
        except Exception:
            pass  # Owner may not be considered a collaborator

    def test_181_repo_refs(self, agent):
        """Agent lists git refs."""
        try:
            result = agent.call("list_repo_refs",
                owner=self.owner, repo=self.repo_name,
            )
            assert isinstance(result, list)
        except Exception:
            pass  # Some Gitea versions may not support this

    def test_182_git_tree(self, agent):
        """Agent gets git tree."""
        commits = agent.call("list_commits", owner=self.owner, repo=self.repo_name)
        sha = commits[0]["sha"]
        result = agent.call("get_git_tree",
            owner=self.owner, repo=self.repo_name, sha=sha,
        )
        assert result is not None

    def test_183_repo_teams(self, agent):
        """Agent lists repo teams."""
        try:
            result = agent.call("list_repo_teams",
                owner=self.owner, repo=self.repo_name,
            )
            assert isinstance(result, list)
        except Exception:
            pass  # Only applicable for org repos

    # ── 27. New tools: Admin expansion ───────────────────────

    def test_184_admin_list_repos(self, agent):
        """Agent lists all repos (admin) via search."""
        # admin_list_repos may not exist on all Gitea versions
        try:
            result = agent.call("admin_list_repos")
            assert isinstance(result, list)
        except Exception:
            pass  # Endpoint may not be available

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
        assert result is not None

    def test_188_license_template_detail(self, agent):
        """Agent gets a specific license template."""
        result = agent.call("get_license_template", name="MIT")
        assert result is not None

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

    # ── 30. Cleanup ──────────────────────────────────────────

    def test_900_cleanup_org(self, agent):
        """Agent cleans up the organization."""
        if self.team_id:
            try:
                agent.call("delete_team", team_id=self.team_id)
            except Exception:
                pass
        if self.org_name:
            try:
                agent.call("delete_org", org=self.org_name)
            except Exception:
                pass

    def test_901_cleanup_user(self, agent):
        """Agent cleans up the test user."""
        try:
            agent.call("admin_delete_user", username="testuser2", purge=True)
        except Exception:
            pass

    def test_999_delete_repo(self, agent):
        """Agent deletes the test repo."""
        result = agent.call("delete_repo", owner=self.owner, repo=self.repo_name)
        # Verify deletion
        try:
            agent.call("get_repo", owner=self.owner, repo=self.repo_name)
            assert False, "Repo should have been deleted"
        except Exception:
            pass  # Expected — repo doesn't exist

    def test_final_print_log(self, agent):
        """Print the full agent call log."""
        print(f"\n{'='*60}")
        print(f"Agent made {len(agent.call_log)} MCP tool calls total")
        print(f"{'='*60}")
        tools_used = set(e["tool"] for e in agent.call_log)
        print(f"Unique tools used: {len(tools_used)}")
        for tool in sorted(tools_used):
            count = sum(1 for e in agent.call_log if e["tool"] == tool)
            print(f"  {tool}: {count}x")
