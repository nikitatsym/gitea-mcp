"""Semantic tests for the misc list filters: PRs, milestones, notifications,
packages, and the three keyword searches.

Conformance tests guard param NAMES — Gitea silently ignores an unknown query
param, so a typo there reads as "no filter" and every one-sided positive check
still passes. These tests guard MEANING: a filter must keep the matching object
AND drop a non-matching one that is provably visible without the filter.
Toggles and orderings get the same treatment through their two states.

Everything is created under the `fsem-misc-` prefix and asserted only against
those objects, so this file can share one Gitea instance with other suites.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import io
import json
import os
import tarfile
import time
import urllib.parse
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import partial

import httpx
import pytest
from conftest import ADMIN_USER, AgentSimulator

pytestmark = pytest.mark.integration

PREFIX = "fsem-misc"
# ALPHA acts as the second user (Gitea never notifies the acting user, so the
# token user needs someone else to generate notifications) and owns the
# packages; BETA exists only to be the non-matching side of the searches.
ALPHA = f"{PREFIX}-alpha"
BETA = f"{PREFIX}-beta"
USER_PASSWORD = "FsemMisc12345"
EMAIL_DOMAIN = "fsem-misc.example"
ALPHA_EMAIL = f"{ALPHA}@{EMAIL_DOMAIN}"
BETA_EMAIL = f"{BETA}@{EMAIL_DOMAIN}"

PULLS_REPO = f"{PREFIX}-pulls"
NOTIFY_REPO = f"{PREFIX}-notify"
# Branch the notification PR is opened from — the third notification thread,
# and the one that makes subject-type two-sided against the two issue threads.
NOTIFY_BRANCH = f"{PREFIX}-notify-source"
TOPIC_MATCH = f"{PREFIX}-alpha-topic"
TOPIC_CONTROL = f"{PREFIX}-beta-topic"
PACKAGE_NAMES = (f"{PREFIX}-pkg-one", f"{PREFIX}-pkg-two")
# Second registry, so `type` has a matching side in both directions.
NPM_PACKAGE = f"{PREFIX}-npm"
PACKAGE_VERSION = "1.0.0"


# ── Raw-HTTP provisioning (outside the MCP surface, like conftest's uploader) ──


def _api_post_as(token: str, path: str, payload: dict) -> dict:
    """POST to /api/v1 as another user.

    The MCP client is bound to one token, so acting as the second user has to
    happen over raw HTTP — same precedent as conftest.upload_generic_package.
    """
    r = httpx.post(
        f"{os.environ['GITEA_URL']}/api/v1{path}",
        headers={"Authorization": f"token {token}"},
        json=payload,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _upload_generic_package_as(token: str, owner: str, name: str) -> None:
    """Publish a generic package into `owner`'s registry as the token's user.

    Gitea's package upload endpoint lives outside /api/v1 and has no MCP op;
    `list_packages` is what's under test here.
    """
    r = httpx.put(
        f"{os.environ['GITEA_URL']}/api/packages/{owner}/generic/"
        f"{name}/{PACKAGE_VERSION}/payload.txt",
        headers={"Authorization": f"token {token}"},
        content=b"fsem-misc payload",
        timeout=30.0,
    )
    r.raise_for_status()


def _npm_tarball(name: str) -> bytes:
    """A minimal npm tarball: one `package/package.json`, byte-deterministic.

    Zeroed mtimes and ownership keep re-runs producing identical bytes, so the
    integrity hash the publish document carries is reproducible too.
    """
    manifest = json.dumps({"name": name, "version": PACKAGE_VERSION}).encode()
    tar_bytes = io.BytesIO()
    with tarfile.open(fileobj=tar_bytes, mode="w") as tar:
        entry = tarfile.TarInfo("package/package.json")
        entry.size = len(manifest)
        entry.mtime = 0
        entry.uid = entry.gid = 0
        entry.uname = entry.gname = ""
        tar.addfile(entry, io.BytesIO(manifest))
    gzipped = io.BytesIO()
    with gzip.GzipFile(fileobj=gzipped, mode="wb", mtime=0) as gz:
        gz.write(tar_bytes.getvalue())
    return gzipped.getvalue()


def _upload_npm_package_as(token: str, owner: str, name: str) -> None:
    """Publish an npm package into `owner`'s registry the way `npm publish` does.

    The npm protocol is a single PUT of a JSON document whose `_attachments`
    carries the base64 tarball; Gitea recomputes `dist.integrity` over those
    bytes and rejects a mismatch, so the digest is derived here, not hardcoded.
    """
    tarball = _npm_tarball(name)
    digest = base64.b64encode(hashlib.sha512(tarball).digest()).decode()
    r = httpx.put(
        f"{os.environ['GITEA_URL']}/api/packages/{owner}/npm/"
        f"{urllib.parse.quote(name, safe='')}",
        headers={"Authorization": f"token {token}"},
        json={
            "_id": name,
            "name": name,
            "dist-tags": {"latest": PACKAGE_VERSION},
            "versions": {
                PACKAGE_VERSION: {
                    "name": name,
                    "version": PACKAGE_VERSION,
                    "dist": {"integrity": f"sha512-{digest}"},
                }
            },
            "_attachments": {
                f"{name}-{PACKAGE_VERSION}.tgz": {
                    "content_type": "application/octet-stream",
                    "data": base64.b64encode(tarball).decode(),
                    "length": len(tarball),
                }
            },
        },
        timeout=30.0,
    )
    r.raise_for_status()


# ── Shared assertions / readers ───────────────────────────────────────────────


def _assert_filter(
    fetch: Callable[..., list],
    filters: dict,
    *,
    keeps,
    drops,
    baseline: dict | None = None,
) -> None:
    """Assert `filters` keeps `keeps`, drops `drops`, and that both are visible
    without it.

    The baseline call is what makes this two-sided: a filter Gitea ignores
    returns everything (so `drops` would still be there) and a filter that
    matches nothing returns an empty list (so `keeps` would be gone).
    """
    unfiltered = fetch(**(baseline or {}))
    assert keeps in unfiltered, f"baseline {baseline} is missing {keeps!r}: {unfiltered}"
    assert drops in unfiltered, f"baseline {baseline} is missing {drops!r}: {unfiltered}"

    filtered = fetch(**filters)
    assert keeps in filtered, f"{filters} dropped matching {keeps!r}: {filtered}"
    assert drops not in filtered, f"{filters} kept non-matching {drops!r}: {filtered}"


def _assert_state_split(fetch: Callable[..., list], opened, closed) -> None:
    """Assert state=open/closed each keep their own item and drop the other's."""
    both = {"state": "all"}
    _assert_filter(fetch, {"state": "open"}, keeps=opened, drops=closed, baseline=both)
    _assert_filter(fetch, {"state": "closed"}, keeps=closed, drops=opened, baseline=both)


def _assert_status_types_split(
    fetch: Callable[..., list], threads: _Notifications
) -> None:
    """Assert each status-types value keeps its status and drops the other.

    Gitea applies status-types only when `all` is falsy, so the baseline that
    has to show both threads is the all=True call.
    """
    both = {"all": True}
    _assert_filter(
        fetch,
        {"status_types": ["read"]},
        keeps=threads.read,
        drops=threads.unread,
        baseline=both,
    )
    _assert_filter(
        fetch,
        {"status_types": ["unread"]},
        keeps=threads.unread,
        drops=threads.read,
        baseline=both,
    )


def _assert_marker_search(
    fetch: Callable[..., list],
    first: tuple[str, str],
    second: tuple[str, str],
    *,
    baseline: str,
) -> None:
    """Assert each (query, expected hit) pair excludes the other pair's hit.

    `baseline` is the wider keyword that must return both hits, so a search
    that matches everything or nothing fails here.
    """
    both = {"query": baseline}
    for (query, keeps), (_, drops) in ((first, second), (second, first)):
        _assert_filter(
            fetch, {"query": query}, keeps=keeps, drops=drops, baseline=both
        )


def _pr_numbers(agent: AgentSimulator, **params) -> list[int]:
    return [
        pr["number"]
        for pr in agent.call(
            "list_pull_requests", owner=ADMIN_USER, repo=PULLS_REPO, **params
        )
    ]


def _milestone_titles(agent: AgentSimulator, **params) -> list[str]:
    return [
        m["title"]
        for m in agent.call(
            "list_milestones", owner=ADMIN_USER, repo=PULLS_REPO, **params
        )
    ]


def _note_ids(agent: AgentSimulator, **params) -> list[int]:
    """Thread IDs from the global notification list, scoped to our repo.

    Notifications are per-user and instance-wide, so anything from another
    suite's repo is filtered out here rather than asserted against.
    """
    return [
        n["id"]
        for n in agent.call("list_notifications", **params)
        if n["repo"] == f"{ADMIN_USER}/{NOTIFY_REPO}"
    ]


def _repo_note_ids(agent: AgentSimulator, **params) -> list[int]:
    return [
        n["id"]
        for n in agent.call(
            "list_repo_notifications", owner=ADMIN_USER, repo=NOTIFY_REPO, **params
        )
    ]


def _package_names(agent: AgentSimulator, **params) -> list[str]:
    return [p["name"] for p in agent.call("list_packages", owner=ALPHA, **params)]


def _user_logins(agent: AgentSimulator, **params) -> list[str]:
    return [
        u["login"]
        for u in agent.call("search_users", **params)
        if u["login"].startswith(PREFIX)
    ]


def _emails(agent: AgentSimulator, **params) -> list[str]:
    return [
        e["email"]
        for e in agent.call("admin_search_emails", **params)
        if e["email"].endswith(EMAIL_DOMAIN)
    ]


def _topic_names(agent: AgentSimulator, **params) -> list[str]:
    result = agent.call("search_topics", **params)
    topics = result.get("topics") if isinstance(result, dict) else result
    return [t["topic_name"] for t in topics or []]


def _wait_for_notifications(
    agent: AgentSimulator, count: int, *, timeout: float = 30.0, interval: float = 0.5
) -> list[dict]:
    """Poll until Gitea's async notification queue has produced `count` threads.

    Notification rows are written by a background worker, so they are not there
    the instant the issue POST returns.
    """
    deadline = time.monotonic() + timeout
    latest: list[dict] = []
    while time.monotonic() < deadline:
        latest = _repo_notifications(agent)
        if len(latest) >= count:
            return latest
        time.sleep(interval)
    raise AssertionError(
        f"only {len(latest)} of {count} notifications appeared for "
        f"{ADMIN_USER}/{NOTIFY_REPO} within {timeout}s: {latest}. "
        "Check that the second user could open the issues and the pull request "
        "and that testadmin still watches the repo."
    )


def _repo_notifications(agent: AgentSimulator) -> list[dict]:
    return agent.call(
        "list_repo_notifications", owner=ADMIN_USER, repo=NOTIFY_REPO, all=True
    )


# ── Idempotent pre-clean (a crashed run must not block the next one) ──────────


def _drop_user(agent: AgentSimulator, username: str) -> None:
    if any(u["login"] == username for u in agent.call("search_users", query=username)):
        agent.call("admin_delete_user", username=username, purge=True)


def _drop_repo(agent: AgentSimulator, name: str) -> None:
    full_name = f"{ADMIN_USER}/{name}"
    if any(r["full_name"] == full_name for r in agent.call("search_repos", query=name)):
        agent.call("delete_repo", owner=ADMIN_USER, repo=name)


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="module")
def alpha_token(agent: AgentSimulator) -> Iterator[str]:
    """Create the second user (plus the search control user) and mint a token.

    CreateUserAccessToken authenticates over HTTP Basic as the target user, so
    the fresh account's own password is what unlocks it.
    """
    for username, email in ((ALPHA, ALPHA_EMAIL), (BETA, BETA_EMAIL)):
        _drop_user(agent, username)
        agent.call(
            "admin_create_user",
            username=username,
            email=email,
            password=USER_PASSWORD,
            must_change_password=False,
        )
    token = agent.call(
        "create_user_access_token",
        name=f"{PREFIX}-token",
        scopes=["all"],
        username=ALPHA,
        password=USER_PASSWORD,
    )["sha1"]

    yield token

    for username in (ALPHA, BETA):
        agent.call("admin_delete_user", username=username, purge=True)


@dataclass(frozen=True)
class _Pulls:
    """PRs and their metadata in the pulls repo, in creation order."""

    match: int  # open, carries the milestone and the label
    plain: int  # open, carries neither — the control for milestone/labels
    closed: int  # closed — the control for state
    milestone_open: str
    milestone_open_id: int
    milestone_closed: str
    label_id: int
    other_label_id: int


@pytest.fixture(scope="module")
def pulls(agent: AgentSimulator) -> Iterator[_Pulls]:
    """A repo with three PRs, two milestones and two labels."""
    _drop_repo(agent, PULLS_REPO)
    agent.call(
        "create_repo",
        name=PULLS_REPO,
        description="fsem-misc pull request filter fixtures",
        private=True,
        auto_init=True,
        default_branch="main",
    )

    numbers: dict[str, int] = {}
    for tag in ("match", "plain", "closed"):
        branch = f"{PREFIX}-{tag}"
        agent.call(
            "create_file",
            owner=ADMIN_USER,
            repo=PULLS_REPO,
            filepath=f"{tag}.txt",
            content=tag,
            message=f"add {tag}",
            branch="main",
            new_branch=branch,
        )
        numbers[tag] = agent.call(
            "create_pull_request",
            owner=ADMIN_USER,
            repo=PULLS_REPO,
            title=f"{PREFIX} {tag}",
            head=branch,
            base="main",
            body=f"<brief>{PREFIX} {tag} pull request</brief>",
        )["number"]

    milestone_open = agent.call(
        "create_milestone", owner=ADMIN_USER, repo=PULLS_REPO, title=f"{PREFIX}-open"
    )
    milestone_closed = agent.call(
        "create_milestone", owner=ADMIN_USER, repo=PULLS_REPO, title=f"{PREFIX}-closed"
    )
    agent.call(
        "edit_milestone",
        owner=ADMIN_USER,
        repo=PULLS_REPO,
        milestone_id=milestone_closed["id"],
        state="closed",
    )
    label = agent.call(
        "create_repo_label",
        owner=ADMIN_USER,
        repo=PULLS_REPO,
        name=f"{PREFIX}-label",
        color="#00ff00",
    )
    other_label = agent.call(
        "create_repo_label",
        owner=ADMIN_USER,
        repo=PULLS_REPO,
        name=f"{PREFIX}-other-label",
        color="#ff0000",
    )

    agent.call(
        "edit_pull_request",
        owner=ADMIN_USER,
        repo=PULLS_REPO,
        index=numbers["match"],
        milestone=milestone_open["id"],
        labels=[label["id"]],
    )
    agent.call(
        "edit_pull_request",
        owner=ADMIN_USER,
        repo=PULLS_REPO,
        index=numbers["closed"],
        state="closed",
    )

    # Detaching a topic (or dropping the repo) leaves the global topic row
    # behind at repo_count=0 — Gitea has no API or cron that removes it. Left
    # as is: the instance is disposable and `npm run gitea:down` wipes it.
    agent.call("add_repo_topic", owner=ADMIN_USER, repo=PULLS_REPO, topic=TOPIC_MATCH)
    agent.call("add_repo_topic", owner=ADMIN_USER, repo=PULLS_REPO, topic=TOPIC_CONTROL)

    yield _Pulls(
        match=numbers["match"],
        plain=numbers["plain"],
        closed=numbers["closed"],
        milestone_open=milestone_open["title"],
        milestone_open_id=milestone_open["id"],
        milestone_closed=milestone_closed["title"],
        label_id=label["id"],
        other_label_id=other_label["id"],
    )

    for topic in (TOPIC_MATCH, TOPIC_CONTROL):
        agent.call(
            "delete_repo_topic", owner=ADMIN_USER, repo=PULLS_REPO, topic=topic
        )
    agent.call("delete_repo", owner=ADMIN_USER, repo=PULLS_REPO)


@dataclass(frozen=True)
class _Notifications:
    """Thread IDs, each named after the filter side it plays."""

    read: int  # issue thread, marked read
    unread: int  # issue thread, left unread
    pull: int  # pull request thread — the subject-type control


@pytest.fixture(scope="module")
def notifications(agent: AgentSimulator, alpha_token: str) -> Iterator[_Notifications]:
    """Three notification threads for testadmin: two issues and one pull request.

    Gitea does not notify the acting user, so every thread is opened by the
    second user in a repo testadmin owns and watches. read/unread stay on the
    two issue threads, so the status-types tests keep a same-subject-type pair
    while the PR thread gives subject-type its non-matching side.
    """
    _drop_repo(agent, NOTIFY_REPO)
    agent.call(
        "create_repo",
        name=NOTIFY_REPO,
        description="fsem-misc notification fixtures",
        private=True,
        auto_init=True,
        default_branch="main",
    )
    agent.call(
        "add_repo_collaborator",
        owner=ADMIN_USER,
        repo=NOTIFY_REPO,
        collaborator=ALPHA,
        permission="write",
    )
    # Notifications reach repo watchers; assert the subscription instead of
    # trusting Gitea's auto-watch-on-create default.
    agent.call("watch_repo", owner=ADMIN_USER, repo=NOTIFY_REPO)

    for tag in ("read", "unread"):
        _api_post_as(
            alpha_token,
            f"/repos/{ADMIN_USER}/{NOTIFY_REPO}/issues",
            {
                "title": f"{PREFIX} {tag} notification",
                "body": f"<brief>{PREFIX} {tag} notification</brief> @{ADMIN_USER}",
                "assignees": [ADMIN_USER],
            },
        )

    # The PR needs a branch with a commit on it, and only testadmin's token is
    # wired into the MCP client; who *opens* the PR is what decides who gets
    # notified, so testadmin pushes the branch and ALPHA opens the PR.
    agent.call(
        "create_file",
        owner=ADMIN_USER,
        repo=NOTIFY_REPO,
        filepath="notify.txt",
        content="fsem-misc notification fixture\n",
        message=f"{PREFIX} add notification fixture file",
        branch="main",
        new_branch=NOTIFY_BRANCH,
    )
    # The <brief> tag is an MCP-side rule (gitea_mcp.prepare), not a Gitea one,
    # so the raw endpoint takes a plain body.
    _api_post_as(
        alpha_token,
        f"/repos/{ADMIN_USER}/{NOTIFY_REPO}/pulls",
        {
            "title": f"{PREFIX} pull notification",
            "head": NOTIFY_BRANCH,
            "base": "main",
            "body": f"{PREFIX} pull notification @{ADMIN_USER}",
            "assignees": [ADMIN_USER],
        },
    )

    # Classified by the subject type Gitea reports, not by creation order: the
    # notification rows are written by an async worker that promises no order.
    threads = _wait_for_notifications(agent, 3)
    issue_ids = sorted(n["id"] for n in threads if n["subject_type"] == "Issue")
    pull_ids = [n["id"] for n in threads if n["subject_type"] == "Pull"]
    assert len(issue_ids) == 2, f"expected two issue threads, got {threads}"
    assert len(pull_ids) == 1, f"expected one pull request thread, got {threads}"

    read, unread = issue_ids
    agent.call("mark_notification_read", thread_id=read)

    yield _Notifications(read=read, unread=unread, pull=pull_ids[0])

    agent.call("delete_repo", owner=ADMIN_USER, repo=NOTIFY_REPO)


@pytest.fixture(scope="module")
def packages(agent: AgentSimulator, alpha_token: str) -> Iterator[tuple[str, ...]]:
    """Two generic packages and one npm package, owned by the second user.

    Two registries, so `type` has a matching object on either side instead of
    only a dropped one. They live in the second user's registry so this file
    never asserts against (or pollutes) testadmin's package list, which other
    suites read.
    """
    for name in PACKAGE_NAMES:
        _upload_generic_package_as(alpha_token, ALPHA, name)
    _upload_npm_package_as(alpha_token, ALPHA, NPM_PACKAGE)

    yield PACKAGE_NAMES

    for name in PACKAGE_NAMES:
        agent.call(
            "delete_package",
            owner=ALPHA,
            type="generic",
            name=name,
            version=PACKAGE_VERSION,
        )
    agent.call(
        "delete_package",
        owner=ALPHA,
        type="npm",
        name=NPM_PACKAGE,
        version=PACKAGE_VERSION,
    )


# ── list_pull_requests ────────────────────────────────────────────────────────


def test_list_pull_requests_state_selects_open_or_closed(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """state=open/closed each keep their own PRs and drop the other's."""
    _assert_state_split(partial(_pr_numbers, agent), pulls.match, pulls.closed)


def test_list_pull_requests_sort_reverses_ordering(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """sort=oldest turns the default ordering head to tail.

    Gitea's sort enum has no "newest": the unsorted call already answers
    newest-first, and an off-enum value would silently return that same default
    order. Pinning the default explicitly is what makes sort=oldest provably a
    reversal rather than a value the server threw away.

    The repo holds only this file's PRs, so exact orderings are asserted.
    """
    ascending = sorted([pulls.match, pulls.plain, pulls.closed])
    assert _pr_numbers(agent, state="all", sort="oldest") == ascending
    assert _pr_numbers(agent, state="all") == ascending[::-1]


def test_list_pull_requests_milestone_filters_by_id(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """milestone takes the milestone's integer ID and drops PRs without it."""
    _assert_filter(
        partial(_pr_numbers, agent),
        {"milestone": pulls.milestone_open_id},
        keeps=pulls.match,
        drops=pulls.plain,
    )


def test_list_pull_requests_labels_filters_by_label_id(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """labels takes label IDs and drops PRs that do not carry them."""
    fetch = partial(_pr_numbers, agent)
    _assert_filter(
        fetch, {"labels": [pulls.label_id]}, keeps=pulls.match, drops=pulls.plain
    )
    # The unused label matches nothing: proof the filter reads the ID, not just
    # "any label".
    assert fetch(labels=[pulls.other_label_id]) == []


def test_list_pull_requests_labels_multiple_ids_match_any(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """Multiple IDs go out as repeated labels= params and match ANY of them.

    The matching PR carries only the first label, so all-of semantics — or the
    comma-joined single value Gitea answers with a 500 — fails here.
    """
    _assert_filter(
        partial(_pr_numbers, agent),
        {"labels": [pulls.label_id, pulls.other_label_id]},
        keeps=pulls.match,
        drops=pulls.plain,
    )


# ── list_milestones ───────────────────────────────────────────────────────────


def test_list_milestones_state_selects_open_or_closed(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """state=open/closed each keep their own milestone and drop the other."""
    _assert_state_split(
        partial(_milestone_titles, agent), pulls.milestone_open, pulls.milestone_closed
    )


# ── list_notifications / list_repo_notifications ──────────────────────────────


def test_list_notifications_all_includes_read_threads(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """all=True widens the list to read threads; omitting it narrows to unread."""
    _assert_filter(
        partial(_note_ids, agent),
        {},
        keeps=notifications.unread,
        drops=notifications.read,
        baseline={"all": True},
    )


def test_list_notifications_status_types_selects_read_or_unread(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """status-types picks the thread status; each value excludes the other."""
    _assert_status_types_split(partial(_note_ids, agent), notifications)


def test_list_repo_notifications_all_includes_read_threads(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """Repo-scoped list: all=True widens to read threads, default is unread."""
    _assert_filter(
        partial(_repo_note_ids, agent),
        {},
        keeps=notifications.unread,
        drops=notifications.read,
        baseline={"all": True},
    )


def test_list_repo_notifications_status_types_selects_read_or_unread(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """Repo-scoped list: each status-types value excludes the other status."""
    _assert_status_types_split(partial(_repo_note_ids, agent), notifications)


def test_list_notifications_subject_type_selects_issue_or_pull(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """subject-type picks the notified object's kind; each value drops the other.

    Unlike status-types, Gitea applies subject-type even when `all` is set, so
    all=True is carried into the filtered calls too — it keeps the baseline and
    the filtered view differing in exactly one param.
    """
    fetch = partial(_note_ids, agent)
    both = {"all": True}
    _assert_filter(
        fetch,
        {"all": True, "subject_type": ["issue"]},
        keeps=notifications.unread,
        drops=notifications.pull,
        baseline=both,
    )
    _assert_filter(
        fetch,
        {"all": True, "subject_type": ["pull"]},
        keeps=notifications.pull,
        drops=notifications.unread,
        baseline=both,
    )


def test_list_notifications_status_types_pinned_excludes_unpinned(
    agent: AgentSimulator, notifications: _Notifications
) -> None:
    """No thread is pinned, so the pinned-only view must drop both of ours.

    Pinning is a web-UI action with no API op, hence no "matching" side here —
    what is testable is that the value is not ignored.
    """
    pinned = _note_ids(agent, status_types=["pinned"])
    assert notifications.read not in pinned, f"pinned view kept a read thread: {pinned}"
    assert notifications.unread not in pinned, f"pinned view kept an unread one: {pinned}"


# ── list_packages ─────────────────────────────────────────────────────────────


def test_list_packages_type_filters_by_registry(
    agent: AgentSimulator, packages: tuple[str, ...]
) -> None:
    """Each type keeps its own registry's package and drops the other's.

    The npm package is what makes this symmetric: with only generic packages
    around, type=npm could be shown to drop things but never to keep the
    matching side of a non-default filter value.
    """
    fetch = partial(_package_names, agent)
    for generic in packages:
        _assert_filter(fetch, {"type": "generic"}, keeps=generic, drops=NPM_PACKAGE)
    _assert_filter(fetch, {"type": "npm"}, keeps=NPM_PACKAGE, drops=packages[0])


# ── search_users / search_topics / admin_search_emails ────────────────────────


def test_search_users_query_matches_only_its_marker(
    agent: AgentSimulator, alpha_token: str
) -> None:
    """Each user's own marker finds them and excludes the other user."""
    _assert_marker_search(
        partial(_user_logins, agent), (ALPHA, ALPHA), (BETA, BETA), baseline=PREFIX
    )


def test_admin_search_emails_query_matches_only_its_marker(
    agent: AgentSimulator, alpha_token: str
) -> None:
    """Each address finds its own row and excludes the other user's."""
    _assert_marker_search(
        partial(_emails, agent),
        (ALPHA_EMAIL, ALPHA_EMAIL),
        (BETA_EMAIL, BETA_EMAIL),
        baseline=f"@{EMAIL_DOMAIN}",
    )


def test_search_topics_query_matches_only_its_marker(
    agent: AgentSimulator, pulls: _Pulls
) -> None:
    """Each topic marker finds its own topic and excludes the sibling topic."""
    _assert_marker_search(
        partial(_topic_names, agent),
        (f"{PREFIX}-alpha", TOPIC_MATCH),
        (f"{PREFIX}-beta", TOPIC_CONTROL),
        baseline=PREFIX,
    )
