"""Unit tests for the non-blocking wait tools (`*_wait_start` / `_poll` / `_cancel`).

Each scenario runs inside a single `asyncio.run(flow())` so the background
poll task spawned by `*_wait_start` survives across the follow-up
`*_wait_poll`. Unlike test_waiters.py we do NOT patch `asyncio.sleep` -
the background task needs a real event loop yield; with `interval=0.01`
each scenario finishes in tens of milliseconds.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import gitea_mcp.tools as tools_mod
from gitea_mcp.client import GiteaClient
from gitea_mcp.config import _reset_settings
from gitea_mcp.wait_registry import WAIT_REGISTRY


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    monkeypatch.setenv("GITEA_URL", "https://gitea.example.com")
    monkeypatch.setenv("GITEA_TOKEN", "test-token")
    _reset_settings()
    tools_mod._client = None
    WAIT_REGISTRY.clear()
    yield
    WAIT_REGISTRY.clear()
    tools_mod._client = None
    _reset_settings()


def _seed(handler) -> GiteaClient:
    client = GiteaClient(transport=httpx.MockTransport(handler))
    tools_mod._client = client
    return client


def _run(run_id: int, status: str, conclusion: str | None = None) -> dict:
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


def _job(job_id: int, status: str, conclusion: str | None = None) -> dict:
    return {
        "id": job_id,
        "name": "build",
        "status": status,
        "conclusion": conclusion,
        "run_id": 42,
        "started_at": "2026-06-13T10:00:00Z",
        "completed_at": None,
        "steps": [],
    }


def _handler(scripts: dict[str, list]):
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


_RUN_PATH = "/api/v1/repos/me/proj/actions/runs/42"
_RUN_JOBS_PATH = "/api/v1/repos/me/proj/actions/runs/42/jobs"


async def _cleanup(wait_id: str) -> None:
    handle = WAIT_REGISTRY.get(wait_id)
    if handle and handle.task and not handle.task.done():
        handle.task.cancel()
        try:
            await handle.task
        except asyncio.CancelledError:
            pass


# ── start / poll / cancel ────────────────────────────────────────────────────


class TestWaitStartPoll:
    def test_terminal_on_first_poll_returns_enriched_snapshot(self):
        scripts = {
            _RUN_PATH: [(200, _run(42, "completed", "success"))],
            _RUN_JOBS_PATH: [(200, {"jobs": [_job(7, "completed", "success")]})],
        }
        _seed(_handler(scripts))

        snap = asyncio.run(tools_mod.workflow_runs_wait_start(
            owner="me", repo="proj", run_id=42, interval=0.01,
        ))
        assert snap["terminated"] is True
        assert snap["timed_out"] is False
        assert snap["status"] == "success"
        assert snap["kind"] == "run"
        assert snap["wait_id"].startswith("wr-")
        assert snap["resource_uri"] == f"gitea://waits/{snap['wait_id']}"
        assert snap["polls"] == 1
        assert isinstance(snap["jobs"], list) and len(snap["jobs"]) == 1
        handle = WAIT_REGISTRY.get(snap["wait_id"])
        assert handle is not None and handle.task is None

    def test_max_block_waits_for_terminal(self):
        scripts = {
            _RUN_PATH: [
                (200, _run(42, "in_progress")),
                (200, _run(42, "completed", "success")),
            ],
            _RUN_JOBS_PATH: [(200, {"jobs": []})],
        }
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
            )
            poll = await tools_mod.workflow_runs_wait_poll(
                start["wait_id"], max_block=5.0,
            )
            return start, poll

        start, poll = asyncio.run(flow())
        assert start["status"] == "in_progress"
        assert poll["terminated"] is True
        assert poll["status"] == "success"
        statuses = [t["to"] for t in poll["transitions"]]
        assert "in_progress" in statuses and "success" in statuses

    def test_max_block_times_out_returns_partial(self):
        scripts = {_RUN_PATH: [(200, _run(42, "in_progress"))]}
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
                include_jobs=False,
            )
            poll = await tools_mod.workflow_runs_wait_poll(
                start["wait_id"], max_block=0.05,
            )
            await _cleanup(start["wait_id"])
            return poll

        poll = asyncio.run(flow())
        assert poll["timed_out"] is True
        assert poll["terminated"] is False
        assert poll["status"] == "in_progress"

    def test_initial_poll_failure_marks_error(self):
        _seed(_handler({}))  # everything 404
        snap = asyncio.run(tools_mod.workflow_runs_wait_start(
            owner="me", repo="proj", run_id=42, interval=0.01,
        ))
        assert snap["terminated"] is False
        assert "initial poll failed" in snap["error"]
        assert WAIT_REGISTRY.get(snap["wait_id"]).task is None

    def test_cancel_running_marks_cancelled(self):
        scripts = {_RUN_PATH: [(200, _run(42, "in_progress"))]}
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
                include_jobs=False,
            )
            return await tools_mod.workflow_runs_wait_cancel(start["wait_id"])

        snap = asyncio.run(flow())
        assert snap["error"] == "cancelled"
        assert snap["terminated"] is False

    def test_unknown_and_wrong_kind_raise(self):
        scripts = {_RUN_PATH: [(200, _run(42, "in_progress"))]}
        _seed(_handler(scripts))

        async def flow():
            with pytest.raises(ValueError, match="Unknown wait_id"):
                await tools_mod.workflow_runs_wait_poll("wr-nope")
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
                include_jobs=False,
            )
            with pytest.raises(ValueError, match="is a run wait, not job"):
                await tools_mod.workflow_jobs_wait_poll(start["wait_id"])
            await _cleanup(start["wait_id"])

        asyncio.run(flow())


# ── resilience: budget and lifetime ──────────────────────────────────────────


class TestWaitResilience:
    def test_transient_poll_failure_recovers(self):
        scripts = {
            _RUN_PATH: [
                (200, _run(42, "in_progress")),    # initial inline poll
                (502, {"message": "bad gateway"}),  # transient blip
                (200, _run(42, "completed", "success")),
            ],
            _RUN_JOBS_PATH: [(200, {"jobs": []})],
        }
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
            )
            return await tools_mod.workflow_runs_wait_poll(
                start["wait_id"], max_block=5.0,
            )

        poll = asyncio.run(flow())
        assert poll["terminated"] is True
        assert poll["status"] == "success"
        assert poll["poll_failures"] == 1
        assert "502" in poll["last_poll_error"]

    def test_consecutive_failures_exhaust_budget(self):
        scripts = {
            _RUN_PATH: [
                (200, _run(42, "in_progress")),
                (502, {"message": "bad gateway"}),  # repeats forever
            ],
        }
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
                max_poll_failures=2, include_jobs=False,
            )
            return await tools_mod.workflow_runs_wait_poll(
                start["wait_id"], max_block=5.0,
            )

        poll = asyncio.run(flow())
        assert poll["terminated"] is False
        assert poll["timed_out"] is False
        assert "2 consecutive failures" in poll["error"]
        assert poll["poll_failures"] == 2

    def test_max_lifetime_marks_timed_out(self):
        scripts = {_RUN_PATH: [(200, _run(42, "in_progress"))]}
        _seed(_handler(scripts))

        async def flow():
            start = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
                max_lifetime=0.05, include_jobs=False,
            )
            return await tools_mod.workflow_runs_wait_poll(
                start["wait_id"], max_block=5.0,
            )

        poll = asyncio.run(flow())
        assert poll["timed_out"] is True
        assert poll["terminated"] is False
        assert "max_lifetime" in poll["error"]


# ── waits_list and dispatch ──────────────────────────────────────────────────


class TestWaitsListAndDispatch:
    def test_waits_list_filters(self):
        scripts = {
            _RUN_PATH: [(200, _run(42, "completed", "success"))],
            _RUN_JOBS_PATH: [(200, {"jobs": []})],
        }
        _seed(_handler(scripts))

        async def flow():
            await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
            )
            return (
                tools_mod.waits_list(),
                tools_mod.waits_list(kind="run", terminated=True),
                tools_mod.waits_list(kind="job"),
            )

        all_waits, run_waits, job_waits = asyncio.run(flow())
        assert len(all_waits) == 1
        assert len(run_waits) == 1
        assert run_waits[0]["run_id"] == 42
        assert run_waits[0]["timed_out"] is False
        assert job_waits == []
        with pytest.raises(ValueError, match="kind must be"):
            tools_mod.waits_list(kind="pipeline")

    def test_dispatch_through_meta_tool_path(self):
        """The async meta-tool must await waiter coroutines and reject ctx
        injection via params."""
        scripts = {
            _RUN_PATH: [(200, _run(42, "completed", "success"))],
            _RUN_JOBS_PATH: [(200, {"jobs": []})],
        }
        _seed(_handler(scripts))
        from gitea_mcp import server

        async def flow():
            coro = server._dispatch(
                "WorkflowRunsWaitStart",
                "gitea_read",
                {"owner": "me", "repo": "proj", "run_id": 42, "interval": 0.01},
            )
            assert asyncio.iscoroutine(coro)
            snap = await coro
            poll = server._dispatch(
                "WorkflowRunsWaitPoll", "gitea_read",
                {"wait_id": snap["wait_id"]},
            )
            return snap, await poll

        snap, poll = asyncio.run(flow())
        assert snap["status"] == "success"
        assert poll["wait_id"] == snap["wait_id"]

        with pytest.raises(ValueError, match="ctx"):
            server._dispatch(
                "WorkflowRunsWait", "gitea_read",
                {"owner": "me", "repo": "proj", "run_id": 42, "ctx": "evil"},
            )

    def test_wrong_group_hint(self):
        _seed(_handler({}))
        from gitea_mcp import server
        with pytest.raises(ValueError, match="belongs to 'gitea_read'"):
            server._dispatch(
                "WorkflowRunsWaitStart", "gitea_execute",
                {"owner": "me", "repo": "proj", "run_id": 42},
            )

    def test_resource_returns_snapshot_json(self):
        scripts = {
            _RUN_PATH: [(200, _run(42, "completed", "success"))],
            _RUN_JOBS_PATH: [(200, {"jobs": []})],
        }
        _seed(_handler(scripts))
        from gitea_mcp import server  # noqa: F401 - registers the resource

        async def flow():
            snap = await tools_mod.workflow_runs_wait_start(
                owner="me", repo="proj", run_id=42, interval=0.01,
            )
            handle = WAIT_REGISTRY.get(snap["wait_id"])
            return json.loads(json.dumps(handle.snapshot(), default=str))

        data = asyncio.run(flow())
        assert data["status"] == "success"
        assert data["kind"] == "run"
