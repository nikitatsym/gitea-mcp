"""Shared scaffolding for the waiter unit tests.

Both waiter suites (test_waiters.py for the blocking ops, test_wait_async.py
for the start/poll/cancel trio) drive the same code paths through
`httpx.MockTransport` with a scripted per-path response list. The payload
builders and the transport live here so the two suites cannot drift apart on
what a workflow run or job looks like.
"""

from __future__ import annotations

from contextlib import contextmanager

import httpx

import gitea_mcp.tools as tools_mod
from gitea_mcp.client import GiteaClient
from gitea_mcp.config import _reset_settings

RUN_PATH = "/api/v1/repos/me/proj/actions/runs/42"
RUN_JOBS_PATH = "/api/v1/repos/me/proj/actions/runs/42/jobs"
JOB_PATH = "/api/v1/repos/me/proj/actions/jobs/7"
JOB_LOGS_PATH = "/api/v1/repos/me/proj/actions/jobs/7/logs"


def seed(handler) -> GiteaClient:
    """Point the tools module at a mock-transport client."""
    client = GiteaClient(transport=httpx.MockTransport(handler))
    tools_mod._client = client
    return client


def run_payload(run_id: int, status: str, conclusion: str | None = None) -> dict:
    return {
        "id": run_id,
        "display_title": "test run",
        "status": status,
        "conclusion": conclusion,
        "event": "push",
        "head_branch": "main",
        "head_sha": "deadbeef" * 5,
        "run_number": 1,
        "path": ".gitea/workflows/ci.yml",
        "started_at": "2026-06-13T10:00:00Z",
        "completed_at": None,
    }


def job_payload(
    job_id: int,
    status: str,
    conclusion: str | None = None,
    name: str = "build",
) -> dict:
    return {
        "id": job_id,
        "name": name,
        "status": status,
        "conclusion": conclusion,
        "run_id": 42,
        "started_at": "2026-06-13T10:00:00Z",
        "completed_at": None,
        "steps": [],
    }


def make_handler(scripts: dict[str, list]):
    """Handler popping from a per-path script list each call; the last item
    repeats forever. Unmatched paths return 404."""
    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        script = scripts.get(path)
        if not script:
            return httpx.Response(404, json={"path": path})
        item = script.pop(0) if len(script) > 1 else script[0]
        status, body = item
        if isinstance(body, str):
            return httpx.Response(status, text=body, headers={"content-type": "text/plain"})
        return httpx.Response(status, json=body)
    return handler


@contextmanager
def tools_state(monkeypatch):
    """Point the tools module at a throwaway instance for one test."""
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    _reset_settings()
    tools_mod._client = None
    try:
        yield
    finally:
        tools_mod._client = None
        _reset_settings()


def seed_run_script(*statuses, jobs: list | None = None, extra: list | None = None):
    """Seed the run endpoint with `(status, conclusion)` steps (last repeats).

    `extra` inserts raw `(http_status, body)` items after the first step - used
    to script a transient 5xx blip. `jobs` also seeds the run's jobs endpoint.
    """
    steps = [(200, run_payload(42, st, cc)) for st, cc in statuses]
    if extra:
        steps[1:1] = extra
    scripts: dict[str, list] = {RUN_PATH: steps}
    if jobs is not None:
        scripts[RUN_JOBS_PATH] = [(200, {"jobs": jobs})]
    seed(make_handler(scripts))
