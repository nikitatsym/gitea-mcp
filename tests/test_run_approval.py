"""Approving a pull-request run that Gitea holds for approval.

Gitea holds a run triggered by a pull request from a fork when the PR author
can neither write to the base repository's Actions nor has a merged pull
request there. A second user forks a repository carrying a pull_request
workflow and opens a PR from the fork. No runner is registered, so approval
releases the held job into the queue, where it stays.
"""

from __future__ import annotations

import base64
import os
import time
from collections.abc import Iterator
from contextlib import ExitStack
from typing import Any

import httpx
import pytest
from conftest import ADMIN_USER, AgentSimulator

pytestmark = pytest.mark.integration

PREFIX = "approval"
FORKER = f"{PREFIX}-forker"
FORKER_PASSWORD = "ApprovalForker12345"
BASE = f"{PREFIX}-base"
BRANCH = f"{PREFIX}-change"
WORKFLOW = """on: pull_request
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - run: echo checked
"""


def _ok(agent: AgentSimulator, op: str, **params: Any) -> None:
    assert agent.call(op, **params) == {"status": "ok"}


def _created(result: Any) -> dict:
    assert isinstance(result, dict) and "error" not in result, result
    return result


def _post_as(token: str, path: str, payload: dict) -> dict:
    """POST as the forker: the MCP client is bound to the admin's token."""
    r = httpx.post(
        f"{os.environ['GITEA_URL']}/api/v1{path}",
        headers={"Authorization": f"token {token}"},
        json=payload,
        timeout=30.0,
    )
    r.raise_for_status()
    return r.json()


def _drop_leftovers(agent: AgentSimulator) -> None:
    if any(u["login"] == FORKER for u in agent.call("search_users", query=FORKER)):
        _ok(agent, "admin_delete_user", username=FORKER, purge=True)
    if any(r["full_name"] == f"{ADMIN_USER}/{BASE}" for r in agent.call("search_repos", query=BASE)):
        _ok(agent, "delete_repo", owner=ADMIN_USER, repo=BASE)


@pytest.fixture(scope="module")
def held_run(agent: AgentSimulator) -> Iterator[dict]:
    """The run a fork's pull request triggers in the base repository, held for approval."""
    _drop_leftovers(agent)
    with ExitStack() as cleanup:
        _created(agent.call(
            "admin_create_user",
            username=FORKER,
            email=f"{FORKER}@approval.example",
            password=FORKER_PASSWORD,
            must_change_password=False,
        ))
        # purge also removes the fork.
        cleanup.callback(_ok, agent, "admin_delete_user", username=FORKER, purge=True)
        token = _created(agent.call(
            "create_user_access_token",
            name=f"{PREFIX}-token",
            scopes=["all"],
            username=FORKER,
            password=FORKER_PASSWORD,
        ))["sha1"]

        _created(agent.call(
            "create_repo", name=BASE, private=False, auto_init=True, default_branch="main",
        ))
        cleanup.callback(_ok, agent, "delete_repo", owner=ADMIN_USER, repo=BASE)
        _created(agent.call(
            "create_file",
            owner=ADMIN_USER,
            repo=BASE,
            filepath=".gitea/workflows/pr.yml",
            content=WORKFLOW,
            message="add pull_request workflow",
            branch="main",
        ))

        _post_as(token, f"/repos/{ADMIN_USER}/{BASE}/forks", {})
        _post_as(token, f"/repos/{FORKER}/{BASE}/contents/change.txt", {
            "content": base64.b64encode(b"a change from the fork\n").decode(),
            "message": "change from the fork",
            "branch": "main",
            "new_branch": BRANCH,
        })
        _post_as(token, f"/repos/{ADMIN_USER}/{BASE}/pulls", {
            "title": "change from the fork",
            "head": f"{FORKER}:{BRANCH}",
            "base": "main",
        })

        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            runs = agent.call("list_workflow_runs", owner=ADMIN_USER, repo=BASE)
            if runs:
                yield runs[0]
                return
            time.sleep(0.5)
        raise AssertionError(f"the fork's pull request triggered no run in {ADMIN_USER}/{BASE}")


def test_approval_releases_a_held_fork_run(agent: AgentSimulator, held_run: dict) -> None:
    run_id = held_run["id"]
    assert (held_run["event"], held_run["status"]) == ("pull_request", "waiting")
    jobs = agent.call("list_workflow_run_jobs", owner=ADMIN_USER, repo=BASE, run_id=run_id)
    assert [j["status"] for j in jobs] == ["waiting"]

    approved = agent.call("approve_workflow_run", owner=ADMIN_USER, repo=BASE, run_id=run_id)
    assert (approved["id"], approved["status"]) == (run_id, "queued")
    assert agent.call(
        "get_workflow_run", owner=ADMIN_USER, repo=BASE, run_id=run_id
    )["status"] == "queued"
    jobs = agent.call("list_workflow_run_jobs", owner=ADMIN_USER, repo=BASE, run_id=run_id)
    assert [j["status"] for j in jobs] == ["queued"]

    again = agent.call("approve_workflow_run", owner=ADMIN_USER, repo=BASE, run_id=run_id)
    assert (again["id"], again["status"]) == (run_id, "queued")
