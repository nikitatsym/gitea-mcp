"""Unit tests for the blocking waiters (`workflow_runs_wait`, `workflow_jobs_wait`).

Polling logic is exercised with `httpx.MockTransport` returning a scripted
sequence of statuses. A fake Context records `report_progress` and `log`
calls so we can assert that notifications fire on status transitions.

`asyncio.sleep` is patched to a no-op so the tests don't actually sleep.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from waiter_fixtures import (
    JOB_LOGS_PATH,
    JOB_PATH,
    RUN_JOBS_PATH,
    RUN_PATH,
    job_payload,
    make_handler,
    run_payload,
    seed,
    tools_state,
)

import gitea_mcp.tools as tools_mod
from gitea_mcp.client import GiteaError


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    # Make `asyncio.sleep(...)` a no-op so polling loops don't actually wait.
    async def _instant_sleep(_secs):
        return None
    monkeypatch.setattr(asyncio, "sleep", _instant_sleep)
    with tools_state(monkeypatch):
        yield


class FakeContext:
    """Minimal stand-in for mcp.server.fastmcp.Context."""

    def __init__(self):
        self.progress: list[dict] = []
        self.logs: list[dict] = []

    async def report_progress(self, progress, total=None, message=None):
        self.progress.append(
            {"progress": progress, "total": total, "message": message}
        )

    async def log(self, level, message, logger_name=None):
        self.logs.append(
            {"level": level, "message": message, "logger_name": logger_name}
        )


# ── workflow_runs_wait ───────────────────────────────────────────────────────


class TestRunsWait:
    def test_reaches_success_after_polls(self):
        scripts = {
            RUN_PATH: [
                (200, run_payload(42, "queued")),
                (200, run_payload(42, "in_progress")),
                (200, run_payload(42, "completed", "success")),
            ],
            RUN_JOBS_PATH: [
                (200, {"jobs": [job_payload(7, "completed", "success")]}),
            ],
        }
        seed(make_handler(scripts))
        ctx = FakeContext()
        result = asyncio.run(
            tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=60.0, interval=0.01, ctx=ctx,
            )
        )
        assert result["terminated"] is True
        assert result["timed_out"] is False
        # Effective status: conclusion once completed.
        assert result["status"] == "success"
        assert result["polls"] == 3
        statuses_seen = [p["message"].rsplit(": ", 1)[1] for p in ctx.progress]
        assert statuses_seen == ["queued", "in_progress", "success"]
        assert isinstance(result["jobs"], list) and len(result["jobs"]) == 1
        assert result["failed_logs"] == {}

    def test_failure_attaches_failed_job_logs(self):
        scripts = {
            RUN_PATH: [
                (200, run_payload(42, "in_progress")),
                (200, run_payload(42, "completed", "failure")),
            ],
            RUN_JOBS_PATH: [
                (200, {"jobs": [
                    job_payload(6, "completed", "success", name="passing"),
                    job_payload(7, "completed", "failure", name="failing"),
                ]}),
            ],
            JOB_LOGS_PATH: [
                (200, "line1\nline2\nFAIL: boom"),
            ],
        }
        seed(make_handler(scripts))
        result = asyncio.run(
            tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=60.0, interval=0.01, log_tail=2,
            )
        )
        assert result["status"] == "failure"
        assert list(result["failed_logs"].keys()) == [7]
        log = result["failed_logs"][7]
        assert "FAIL: boom" in log["text"]
        assert log["total_lines"] == 3
        assert log["truncated"] is True

    def test_timeout_returns_partial(self):
        scripts = {RUN_PATH: [(200, run_payload(42, "in_progress"))]}
        seed(make_handler(scripts))
        result = asyncio.run(
            tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=0.05, interval=0.02,
                include_jobs=False,
            )
        )
        assert result["terminated"] is False
        assert result["timed_out"] is True
        assert result["status"] == "in_progress"

    def test_blocked_is_terminal(self):
        """Approval gates won't change without an external trigger - stop
        polling instead of burning the timeout."""
        scripts = {RUN_PATH: [(200, run_payload(42, "blocked"))]}
        seed(make_handler(scripts))
        result = asyncio.run(
            tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=60.0, interval=0.01, include_jobs=False,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "blocked"
        assert result["polls"] == 1

    def test_enrichment_failure_does_not_discard_result(self):
        scripts = {
            RUN_PATH: [(200, run_payload(42, "completed", "success"))],
            # no jobs path -> 404 on enrichment
        }
        seed(make_handler(scripts))
        result = asyncio.run(
            tools_mod.workflow_runs_wait(owner="me", repo="proj", run_id=42,
                                         timeout=10.0, interval=0.01)
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert "failed to fetch jobs" in result["enrichment_error"]
        assert "jobs" not in result

    def test_rejects_bad_params(self):
        seed(make_handler({}))
        with pytest.raises(ValueError, match="interval must be > 0"):
            asyncio.run(tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42, interval=0))
        with pytest.raises(ValueError, match="max_poll_failures must be >= 1"):
            asyncio.run(tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42, max_poll_failures=0))


# ── poll-failure budget ──────────────────────────────────────────────────────


class TestPollFailureBudget:
    def test_transient_blip_is_tolerated(self):
        scripts = {
            RUN_PATH: [
                (200, run_payload(42, "in_progress")),
                (502, {"message": "bad gateway"}),
                (200, run_payload(42, "completed", "success")),
            ],
        }
        seed(make_handler(scripts))
        ctx = FakeContext()
        result = asyncio.run(
            tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=60.0, interval=0.01, include_jobs=False, ctx=ctx,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert result["polls"] == 3  # failed call counts as a poll
        assert result["poll_failures"] == 1
        assert "502" in result["last_poll_error"]
        assert any(
            "poll failed (1/3 consecutive)" in e["message"] for e in ctx.logs
        )

    def test_budget_exhaustion_raises(self):
        scripts = {
            RUN_PATH: [
                (200, run_payload(42, "in_progress")),
                (502, {"message": "bad gateway"}),  # repeats forever
            ],
        }
        seed(make_handler(scripts))
        with pytest.raises(GiteaError, match="502"):
            asyncio.run(tools_mod.workflow_runs_wait(
                owner="me", repo="proj", run_id=42,
                timeout=60.0, interval=0.01, max_poll_failures=2,
                include_jobs=False,
            ))

    def test_fatal_4xx_raises_immediately(self):
        calls = {"n": 0}

        def handler(req):
            calls["n"] += 1
            if calls["n"] == 1:
                return httpx.Response(200, json=job_payload(7, "in_progress"))
            return httpx.Response(404, json={"message": "404 Not Found"})

        seed(handler)
        with pytest.raises(GiteaError, match="404"):
            asyncio.run(tools_mod.workflow_jobs_wait(
                owner="me", repo="proj", job_id=7,
                timeout=60.0, interval=0.01, max_poll_failures=5,
                include_log=False,
            ))
        assert calls["n"] == 2  # exactly one failed poll, no retries


# ── workflow_jobs_wait ───────────────────────────────────────────────────────


class TestJobsWait:
    def test_job_wait_attaches_log(self):
        scripts = {
            JOB_PATH: [
                (200, job_payload(7, "in_progress")),
                (200, job_payload(7, "completed", "success")),
            ],
            JOB_LOGS_PATH: [
                (200, "a\nb\nc\nd"),
            ],
        }
        seed(make_handler(scripts))
        result = asyncio.run(
            tools_mod.workflow_jobs_wait(
                owner="me", repo="proj", job_id=7,
                timeout=60.0, interval=0.01, log_tail=2,
            )
        )
        assert result["terminated"] is True
        assert result["status"] == "success"
        assert result["log"]["text"] == "c\nd"
        assert result["log"]["truncated"] is True
