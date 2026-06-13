"""Registry of long-running wait handles for the workflow_*_wait_* tools.

Module-level singleton; terminal handles reaped after 1h TTL. The poll task
is the sole writer to a handle (HTTP in a worker thread, mutations applied
back on the loop), so reads need no lock. Pattern: mcp-server-v2 spec,
"Long-running waiters".
"""

from __future__ import annotations

import asyncio
import secrets
import time
from typing import Any

# Union of GitHub-style ("completed" + conclusion) and bare final statuses -
# Gitea serializes both. `blocked` = approval gate: won't change without
# external action, so polling it is pointless.
TERMINAL_STATUSES = frozenset(
    {"success", "failure", "cancelled", "skipped", "completed", "blocked"}
)


_DEFAULT_TTL_SECONDS = 3600  # keep terminal handles 1h so agents can re-fetch


class WaitHandle:
    """One long-running wait operation."""

    __slots__ = (
        "wait_id",
        "kind",
        "owner",
        "repo",
        "target_id",
        "options",
        "status",
        "terminated",
        "timed_out",
        "polls",
        "poll_failures",
        "last_poll_error",
        "started_at",
        "ended_at",
        "last_payload",
        "transitions",
        "final_extras",
        "error",
        "task",
        "done_event",
    )

    def __init__(
        self,
        wait_id: str,
        kind: str,
        owner: str,
        repo: str,
        target_id: int,
        options: dict[str, Any],
    ):
        self.wait_id = wait_id
        self.kind = kind  # "run" | "job"
        self.owner = owner
        self.repo = repo
        self.target_id = target_id
        self.options = options

        self.status: str | None = None
        self.terminated: bool = False
        self.timed_out: bool = False
        self.polls: int = 0
        self.poll_failures: int = 0
        self.last_poll_error: str | None = None
        self.started_at: float = time.time()
        self.ended_at: float | None = None
        self.last_payload: Any = None
        self.transitions: list[dict[str, Any]] = []
        self.final_extras: dict[str, Any] = {}
        self.error: str | None = None

        self.task: asyncio.Task | None = None
        self.done_event: asyncio.Event = asyncio.Event()

    @property
    def elapsed_seconds(self) -> float:
        end = self.ended_at if self.ended_at is not None else time.time()
        return round(end - self.started_at, 2)

    def record_transition(self, new_status: str | None) -> bool:
        """If `new_status` differs from current, log a transition. Returns True
        when a transition was recorded."""
        if new_status == self.status:
            return False
        self.transitions.append({
            "from": self.status,
            "to": new_status,
            "elapsed_seconds": round(time.time() - self.started_at, 2),
        })
        self.status = new_status
        return True

    def record_poll_failure(self, message: str) -> None:
        """A failed HTTP call still counts into `polls`; kept on the handle
        so snapshots show flakiness even after recovery."""
        self.polls += 1
        self.poll_failures += 1
        self.last_poll_error = message

    def mark_terminated(self, *, error: str | None = None) -> None:
        self.terminated = error is None
        self.error = error
        self.ended_at = time.time()
        self.done_event.set()

    def mark_timed_out(self, message: str) -> None:
        """The wait gave up (max_lifetime exceeded) without a terminal status."""
        self.timed_out = True
        self.error = message
        self.ended_at = time.time()
        self.done_event.set()

    def snapshot(self) -> dict[str, Any]:
        """JSON-serializable state; latest payload always, enrichment when terminal."""
        payload_key = "run" if self.kind == "run" else "job"
        target_key = "run_id" if self.kind == "run" else "job_id"

        snap: dict[str, Any] = {
            "wait_id": self.wait_id,
            "resource_uri": f"gitea://waits/{self.wait_id}",
            "kind": self.kind,
            "owner": self.owner,
            "repo": self.repo,
            target_key: self.target_id,
            "status": self.status,
            "terminated": self.terminated,
            "timed_out": self.timed_out,
            "polls": self.polls,
            "elapsed_seconds": self.elapsed_seconds,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "transitions": list(self.transitions),
        }
        if self.poll_failures:
            snap["poll_failures"] = self.poll_failures
            snap["last_poll_error"] = self.last_poll_error
        if self.last_payload is not None:
            snap[payload_key] = self.last_payload
        if self.error is not None:
            snap["error"] = self.error
        if self.terminated:
            for k, v in self.final_extras.items():
                snap[k] = v
        return snap


class WaitRegistry:
    """Module-level singleton holding all in-flight and recently-terminal waits."""

    def __init__(self, ttl_seconds: int = _DEFAULT_TTL_SECONDS):
        self._waits: dict[str, WaitHandle] = {}
        self._ttl = ttl_seconds

    def new_handle(
        self,
        kind: str,
        owner: str,
        repo: str,
        target_id: int,
        options: dict[str, Any],
    ) -> WaitHandle:
        prefix = "wr" if kind == "run" else "wj"
        wait_id = f"{prefix}-{secrets.token_hex(4)}"
        handle = WaitHandle(wait_id, kind, owner, repo, target_id, options)
        self._waits[wait_id] = handle
        return handle

    def get(self, wait_id: str) -> WaitHandle | None:
        return self._waits.get(wait_id)

    def all_handles(self) -> list[WaitHandle]:
        return list(self._waits.values())

    def reap_old(self, *, now: float | None = None) -> int:
        """Drop terminal handles older than TTL. Returns count removed."""
        now = now if now is not None else time.time()
        stale = [
            wid for wid, h in self._waits.items()
            if h.ended_at is not None and (now - h.ended_at) > self._ttl
        ]
        for wid in stale:
            del self._waits[wid]
        return len(stale)

    def clear(self) -> None:
        """Cancel running tasks and drop all handles (test reset)."""
        for h in self._waits.values():
            if h.task is not None and not h.task.done():
                h.task.cancel()
        self._waits.clear()


WAIT_REGISTRY = WaitRegistry()
