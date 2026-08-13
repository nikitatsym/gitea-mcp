"""Semantic filter tests for the repo / file / commit query params.

The conformance suite pins the wire *names* of these params; this module pins
what they *do*. Gitea silently ignores a param it does not know, so a filter
that never applies still answers 200 with a plausible-looking body. Every
filter here is therefore checked two-sided — the matching object survives the
filter, the non-matching one drops out, and both are present unfiltered — so a
filter that excludes everything (or nothing) fails instead of passing on a
one-sided positive check. Toggles and selectors are checked by asserting the
documented difference between their two values.

The Gitea instance is shared with the other test modules, so every assertion is
scoped to this module's own `fsem-repos-` names and never to a global count.
"""

from __future__ import annotations

import base64

import pytest
from conftest import ADMIN_USER

pytestmark = pytest.mark.integration

PREFIX = "fsem-repos-"
TOPIC = f"{PREFIX}topic"

# Both repo names contain TOPIC, so a plain keyword search finds the pair while
# `topic=True` must keep only the one that actually carries the topic. The
# alpha/zulu tails give `sort`/`order` an unambiguous alphabetical pair.
REPO_TAGGED = f"{TOPIC}-alpha"
REPO_UNTAGGED = f"{TOPIC}-zulu"
# Public control: the repo that must survive admin_list_repos' private=False,
# proving `private` widens the listing to private repos rather than narrowing
# it to private-only.
REPO_PUBLIC = f"{PREFIX}public"

DEFAULT_BRANCH = "main"
REF_BRANCH = f"{PREFIX}ref"

REF_DIR = "refdir"
PROBE_PATH = f"{REF_DIR}/probe.txt"
BRANCH_ONLY_PATH = f"{REF_DIR}/branch-only.txt"
MARKER_PATH = "marker/outside.txt"

DEFAULT_TEXT = "probe content on the default branch\n"
BRANCH_TEXT = "probe content on the ref branch\n"
BRANCH_ONLY_TEXT = "file that exists only on the ref branch\n"
MARKER_TEXT = "commit target outside refdir\n"

PROBE_COMMIT = f"{PREFIX}add probe on default branch"
BRANCH_COMMIT = f"{PREFIX}rewrite probe on ref branch"
BRANCH_ONLY_COMMIT = f"{PREFIX}add branch-only file"
MARKER_COMMIT = f"{PREFIX}add marker outside refdir"


def _full(name: str) -> str:
    return f"{ADMIN_USER}/{name}"


# ── Read helpers (one per op under test) ─────────────────────────────────────


def _searched_repos(agent, query: str, **params) -> list[str]:
    """full_name list from search_repos, narrowed to this module's repos.

    Result order is preserved — the sort/order test reads positions off it.
    """
    found = agent.call("search_repos", query=query, limit=50, **params)
    mine = f"{ADMIN_USER}/{PREFIX}"
    return [r["full_name"] for r in found if r["full_name"].startswith(mine)]


def _file_text(agent, **params) -> str:
    """Decoded body of the probe file as get_file_content serves it."""
    result = agent.call("get_file_content",
        owner=ADMIN_USER, repo=REPO_TAGGED, filepath=PROBE_PATH, **params,
    )
    return base64.b64decode(result["content"]).decode()


def _raw_text(agent, **params) -> str:
    return agent.call_raw("get_raw_file",
        owner=ADMIN_USER, repo=REPO_TAGGED, filepath=PROBE_PATH, **params,
    )


def _dir_entries(agent, **params) -> set[str]:
    entries = agent.call("get_directory_content",
        owner=ADMIN_USER, repo=REPO_TAGGED, dirpath=REF_DIR, **params,
    )
    return {entry["name"] for entry in entries}


def _tree_paths(agent, **params) -> set[str]:
    branch = agent.call("get_branch",
        owner=ADMIN_USER, repo=REPO_TAGGED, branch=DEFAULT_BRANCH,
    )
    tree = agent.call("get_git_tree",
        owner=ADMIN_USER, repo=REPO_TAGGED, sha=branch["commit"]["id"], **params,
    )
    return {entry["path"] for entry in tree["tree"]}


def _commit_messages(agent, **params) -> list[str]:
    commits = agent.call("list_commits",
        owner=ADMIN_USER, repo=REPO_TAGGED, limit=50, **params,
    )
    return [commit["message"] for commit in commits]


def _commit_stats(agent, **params) -> list:
    """Per-commit `stats` objects off the full (brief=False) commit view.

    The slim view drops stats entirely, so `stat` is only observable raw.
    """
    commits = agent.call("list_commits",
        owner=ADMIN_USER, repo=REPO_TAGGED, limit=10, brief=False, **params,
    )
    return [commit.get("stats") for commit in commits]


# ── Provisioning ─────────────────────────────────────────────────────────────


def _create_file(agent, filepath: str, content: str, message: str, branch: str) -> None:
    agent.call("create_file",
        owner=ADMIN_USER, repo=REPO_TAGGED, filepath=filepath,
        content=content, message=message, branch=branch,
    )


def _delete_prefixed_repos(agent) -> None:
    """Drop every `fsem-repos-` repo — leftovers of an interrupted run, then teardown.

    Deleting what the search actually finds keeps the fixture idempotent
    without swallowing a 404 for a repo that was never there.
    """
    for full_name in _searched_repos(agent, PREFIX):
        agent.call("delete_repo", owner=ADMIN_USER, repo=full_name.split("/", 1)[1])


@pytest.fixture(scope="class")
def repos(agent):
    """Provision the repos, topic, files and branch every assertion is scoped to.

    REPO_TAGGED also hosts the file/commit fixtures: the probe file differs
    between the default branch and REF_BRANCH, BRANCH_ONLY_PATH exists only on
    the branch, and MARKER_PATH is the default-branch commit that the `path`
    filter must exclude.
    """
    _delete_prefixed_repos(agent)
    for name, private in ((REPO_TAGGED, True), (REPO_UNTAGGED, True), (REPO_PUBLIC, False)):
        agent.call("create_repo",
            name=name, private=private, auto_init=True, default_branch=DEFAULT_BRANCH,
        )
    # Deleting the repo (or its topics) leaves the global topic row behind with
    # repo_count=0 — Gitea exposes no API or cron to remove it. Accepted here:
    # the instance is disposable and `npm run gitea:down` wipes the volume.
    agent.call("set_repo_topics", owner=ADMIN_USER, repo=REPO_TAGGED, topics=[TOPIC])

    _create_file(agent, PROBE_PATH, DEFAULT_TEXT, PROBE_COMMIT, DEFAULT_BRANCH)
    probe = agent.call("get_file_content",
        owner=ADMIN_USER, repo=REPO_TAGGED, filepath=PROBE_PATH,
    )
    # new_branch forks REF_BRANCH off the default branch and commits there,
    # leaving the default branch on DEFAULT_TEXT.
    agent.call("update_file",
        owner=ADMIN_USER, repo=REPO_TAGGED, filepath=PROBE_PATH,
        content=BRANCH_TEXT, message=BRANCH_COMMIT, sha=probe["sha"],
        branch=DEFAULT_BRANCH, new_branch=REF_BRANCH,
    )
    _create_file(agent, BRANCH_ONLY_PATH, BRANCH_ONLY_TEXT, BRANCH_ONLY_COMMIT, REF_BRANCH)
    _create_file(agent, MARKER_PATH, MARKER_TEXT, MARKER_COMMIT, DEFAULT_BRANCH)
    yield
    _delete_prefixed_repos(agent)


@pytest.mark.usefixtures("repos")
class TestRepoFilterSemantics:
    """Each param must demonstrably change the result the way it is documented."""

    # ── search_repos ─────────────────────────────────────────

    def test_search_repos_query_narrows_the_result(self, agent):
        """`q` matches repo names: the keyword both repos share finds the pair,
        the keyword only one repo carries drops the other."""
        broad = _searched_repos(agent, TOPIC)
        assert _full(REPO_TAGGED) in broad
        assert _full(REPO_UNTAGGED) in broad

        narrow = _searched_repos(agent, REPO_TAGGED)
        assert _full(REPO_TAGGED) in narrow
        assert _full(REPO_UNTAGGED) not in narrow

    def test_search_repos_topic_matches_topics_only(self, agent):
        """`topic=True` matches `q` against topic names only: the repo whose
        *name* contains the keyword but which carries no topic drops out."""
        unfiltered = _searched_repos(agent, TOPIC)
        assert _full(REPO_TAGGED) in unfiltered
        assert _full(REPO_UNTAGGED) in unfiltered

        filtered = _searched_repos(agent, TOPIC, topic=True)
        assert _full(REPO_TAGGED) in filtered
        assert _full(REPO_UNTAGGED) not in filtered

    def test_search_repos_sort_order_reorders_results(self, agent):
        """`sort=alpha` with `order` asc/desc really reorders: this module's two
        repos swap relative position between the two directions."""
        ascending = _searched_repos(agent, TOPIC, sort="alpha", order="asc")
        descending = _searched_repos(agent, TOPIC, sort="alpha", order="desc")

        assert ascending.index(_full(REPO_TAGGED)) < ascending.index(_full(REPO_UNTAGGED))
        assert descending.index(_full(REPO_TAGGED)) > descending.index(_full(REPO_UNTAGGED))

    # ── admin_list_repos ─────────────────────────────────────

    def test_admin_list_repos_private_widens_to_private_repos(self, agent):
        """`private` is include-private, not the private-only `is_private` filter.

        Omitting it sends no `private` param at all, so Gitea's default (include
        private) lists this module's two private repos next to its public one;
        `private=False` drops the private pair and keeps the public repo. The
        public control is what pins the direction — a private-only reading of
        the param would drop it instead.
        """
        # Raised limit: the instance is shared, the default page of 20 could
        # miss this module's repos behind other suites' fixtures.
        listed = {repo["full_name"]: repo for repo in agent.call("admin_list_repos", limit=50)}

        assert listed[_full(REPO_TAGGED)]["private"] is True
        assert listed[_full(REPO_UNTAGGED)]["private"] is True
        assert listed[_full(REPO_PUBLIC)]["private"] is False

        public_only = {
            repo["full_name"]
            for repo in agent.call("admin_list_repos", limit=50, private=False)
        }
        assert _full(REPO_TAGGED) not in public_only
        assert _full(REPO_UNTAGGED) not in public_only
        assert _full(REPO_PUBLIC) in public_only

    # ── ref: get_file_content / get_raw_file / get_directory_content ──

    def test_get_file_content_ref_selects_the_branch(self, agent):
        """`ref` reads the named branch; omitting it reads the default branch."""
        assert _file_text(agent) == DEFAULT_TEXT
        assert _file_text(agent, ref=REF_BRANCH) == BRANCH_TEXT

    def test_get_raw_file_ref_selects_the_branch(self, agent):
        """Same file, raw endpoint: `ref` must move the same way."""
        assert _raw_text(agent) == DEFAULT_TEXT
        assert _raw_text(agent, ref=REF_BRANCH) == BRANCH_TEXT

    def test_get_directory_content_ref_selects_the_branch(self, agent):
        """`ref` lists the branch's tree: the branch-only file appears only there,
        while the file both branches share stays in both listings."""
        default_entries = _dir_entries(agent)
        branch_entries = _dir_entries(agent, ref=REF_BRANCH)
        probe_name = PROBE_PATH.split("/")[-1]
        branch_only_name = BRANCH_ONLY_PATH.split("/")[-1]

        assert probe_name in default_entries
        assert branch_only_name not in default_entries
        assert {probe_name, branch_only_name} <= branch_entries

    # ── get_git_tree ─────────────────────────────────────────

    def test_get_git_tree_recursive_lists_nested_paths(self, agent):
        """`recursive=True` walks into subtrees: the nested blob path shows up
        only there, while the subtree entry itself is in both responses."""
        shallow = _tree_paths(agent)
        deep = _tree_paths(agent, recursive=True)

        assert REF_DIR in shallow
        assert PROBE_PATH not in shallow
        assert {REF_DIR, PROBE_PATH} <= deep

    # ── list_commits ─────────────────────────────────────────

    def test_list_commits_sha_selects_the_branch_history(self, agent):
        """`sha` walks the named branch: the branch-only commit is reachable only
        with it, and the shared default-branch commit stays in both walks."""
        default_history = _commit_messages(agent)
        branch_history = _commit_messages(agent, sha=REF_BRANCH)

        assert PROBE_COMMIT in default_history
        assert BRANCH_ONLY_COMMIT not in default_history
        assert {PROBE_COMMIT, BRANCH_ONLY_COMMIT} <= set(branch_history)

    def test_list_commits_path_excludes_untouched_commits(self, agent):
        """`path` keeps only commits that touched it: the commit on another path
        drops out, though both are in the unfiltered history."""
        unfiltered = _commit_messages(agent)
        assert PROBE_COMMIT in unfiltered
        assert MARKER_COMMIT in unfiltered

        filtered = _commit_messages(agent, path=PROBE_PATH)
        assert PROBE_COMMIT in filtered
        assert MARKER_COMMIT not in filtered

    def test_list_commits_stat_toggles_diff_stats(self, agent):
        """`stat` decides whether per-commit diff stats are computed at all —
        the same commits come back with and without a `stats` object."""
        with_stats = _commit_stats(agent, stat=True)
        without_stats = _commit_stats(agent, stat=False)

        assert with_stats
        assert len(with_stats) == len(without_stats)
        assert all({"total", "additions", "deletions"} <= set(s) for s in with_stats)
        assert all(s is None for s in without_stats)
