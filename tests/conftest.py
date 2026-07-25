"""Integration test fixtures.

This conftest does NOT manage the Docker lifecycle. It assumes a Gitea
instance is already running with a valid token recorded in `tests/.env`
(produced by `scripts/bootstrap.py` — usually invoked via `npm run gitea:up`).

Integration tests are opt-in (deselected by default). When explicitly
selected without a running instance they fail loudly (see
pytest_collection_modifyitems), never skip silently into a false green.
"""

from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

import gitea_mcp.tools as _tools
from gitea_mcp.config import set_allow_public
from gitea_mcp.server import mcp, _all_grouped, _to_pascal

TESTS_DIR = Path(__file__).parent
ENV_FILE = TESTS_DIR / ".env"

# Constants kept here so individual integration tests can still reference
# the admin user without round-tripping through env.
ADMIN_USER = "testadmin"
ADMIN_PASS = "testadmin1234"


def _load_env_file() -> None:
    """Load GITEA_URL / GITEA_TOKEN / GITEA_ADMIN_* from tests/.env.

    Called at collect-time so a selected integration suite picks up the
    bootstrap env. `os.environ.setdefault` — explicit env vars win.
    """
    if not ENV_FILE.exists():
        return
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env_file()


@pytest.hookimpl(trylast=True)  # after pytest's own -m deselection prunes items
def pytest_collection_modifyitems(config, items):
    # Selected integration tests with no instance must fail, not skip to false green.
    selected = [it for it in items if it.get_closest_marker("integration")]
    missing = [n for n in ("GITEA_URL", "GITEA_TOKEN") if not os.environ.get(n)]
    if selected and missing:
        raise pytest.UsageError(
            f"{len(selected)} integration test(s) selected but {missing} not set. "
            "Run `npm run gitea:up` first."
        )


# ── Agent simulator ───────────────────────────────────────────────────────────


class AgentSimulator:
    """Simulates an MCP agent calling tools by name.

    Mirrors how an LLM agent interacts with MCP — call tools by name with kwargs,
    receive JSON results. Maintains a call log for debugging and introspection.

    Usage:
        result = agent.call("create_repo", name="test-repo", private=False)
        repos = agent.call("list_issues", owner="testadmin", repo="test-repo")
    """

    def __init__(self):
        self.call_log: list[dict] = []
        self._tools: dict[str, Any] = {}
        # Build tool lookup once
        for tool in mcp._tool_manager._tools.values():
            self._tools[tool.name] = tool.fn

    def call(self, tool_name: str, **kwargs) -> Any:
        """Call an MCP tool by name and return its result.

        Tools hand back Python objects (dict/list) already, or plain text for
        the raw endpoints (diffs, archives, logs) — nothing on this path needs
        JSON decoding. `call_raw` is the same thing, named at the call site
        where the test deliberately wants unparsed text.
        """
        return self.call_raw(tool_name, **kwargs)

    def call_raw(self, tool_name: str, **kwargs) -> Any:
        """Call an MCP tool and return its raw result.

        Converts snake_case to the PascalCase operation and dispatches via
        the right meta-tool group (or a ROOT tool directly).
        """
        pascal = _to_pascal(tool_name)
        if pascal in _all_grouped:
            fn = self._tools[_all_grouped[pascal]]
            result = fn(operation=pascal, params=kwargs)
        else:
            fn = self._tools.get(tool_name)
            if fn is None:
                raise ValueError(
                    f"Unknown tool: {tool_name}. "
                    f"Available: {sorted(self._tools.keys())}"
                )
            result = fn(**kwargs)
        if inspect.iscoroutine(result):
            result = asyncio.run(result)  # meta-tools are async; tests call sync
        self.call_log.append({"tool": tool_name, "kwargs": kwargs, "result": result})
        return result

    def print_call_log(self):
        """Print all tool calls and their results for debugging."""
        for entry in self.call_log:
            print(f"call: {entry['tool']}({entry['kwargs']})")
            result = entry["result"]
            if len(str(result)) > 200:
                print(f"  => {str(result)[:200]}...")
            else:
                print(f"  => {result}")


# ── Test helpers ──────────────────────────────────────────────────────────────


def wait_for_pr_mergeable(
    agent: "AgentSimulator",
    owner: str,
    repo: str,
    index: int,
    *,
    timeout: float = 30.0,
    interval: float = 1.0,
) -> dict:
    """Block until Gitea has computed the PR's `mergeable` flag.

    Gitea derives `mergeable` asynchronously after PR creation. Calling
    `/repos/{owner}/{repo}/pulls/{index}/merge` while it's still `null` gets
    a misleading 405 with body `{"message": "Please try again later"}` — the
    HTTP verb is fine, the merge engine just hasn't finished its test-merge
    yet.

    Polls `get_pull_request` every `interval` seconds for up to `timeout`.
    Returns the final PR snapshot once `mergeable=True`. Three failure modes
    surface as `AssertionError` (so the calling test fails loudly instead of
    silently retrying forever or masking a real merge-blocking condition):

      - mergeable=False before timeout — real obstruction (conflicts, draft,
        branch protection). The caller would otherwise have gotten the same
        405 on the next merge attempt; raising here points at the cause.
      - PR moved to state='closed' / 'merged' while we were waiting —
        someone else (or a hook) acted; merging again is a no-op or error.
      - Timeout — `mergeable` never converged. Surfaces the last snapshot
        so the failure message names what the polling saw.
    """
    deadline = time.monotonic() + timeout
    last_pr: dict | None = None
    while time.monotonic() < deadline:
        pr = agent.call("get_pull_request", owner=owner, repo=repo, index=index)
        last_pr = pr
        state = pr.get("state")
        if state in ("closed", "merged") or pr.get("merged"):
            raise AssertionError(
                f"PR #{index} is no longer open while waiting for mergeable: "
                f"state={state!r}, merged={pr.get('merged')!r}. "
                "Something else moved the PR before the test could merge it."
            )
        mergeable = pr.get("mergeable")
        has_conflicts = pr.get("has_merge_conflicts")
        if mergeable is True:
            return pr
        # Gitea flips mergeable to False before has_merge_conflicts converges;
        # only trust False once conflicts is also set (True/False).
        if mergeable is False and has_conflicts is not None:
            raise AssertionError(
                f"PR #{index} reached mergeable=False before timeout: "
                f"state={state!r}, "
                f"has_merge_conflicts={has_conflicts!r}, "
                f"draft={pr.get('draft')!r}. Gitea will reject the merge."
            )
        time.sleep(interval)
    snapshot = (
        f"state={last_pr.get('state')!r}, "
        f"mergeable={last_pr.get('mergeable')!r}, "
        f"has_merge_conflicts={last_pr.get('has_merge_conflicts')!r}"
        if last_pr else "no PR snapshot captured"
    )
    raise AssertionError(
        f"PR #{index} mergeable status did not converge within {timeout}s. "
        f"Last snapshot: {snapshot}. "
        "Gitea's async mergeability checker did not finish; try increasing "
        "the timeout or check the gitea logs."
    )


def wait_for_workflow_run(
    agent: "AgentSimulator",
    owner: str,
    repo: str,
    *,
    timeout: float = 20.0,
    interval: float = 0.5,
) -> dict:
    """Block until a dispatched workflow run is recorded, then return it.

    `dispatch_workflow` answers 204 before the run row exists, so reading it
    back needs a poll. With no act_runner registered the run then parks in
    `queued` forever — which is enough for the run/job read ops.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        runs = agent.call("list_workflow_runs", owner=owner, repo=repo)
        if runs:
            return runs[0]
        time.sleep(interval)
    raise AssertionError(
        f"No workflow run appeared for {owner}/{repo} within {timeout}s after "
        "dispatch. Check that GITEA__actions__ENABLED is set and that the "
        "workflow file declares `on: workflow_dispatch`."
    )


def upload_generic_package(
    name: str,
    version: str,
    filename: str,
    content: bytes,
) -> None:
    """Publish a generic package so the package read/delete ops have a target.

    Gitea's upload endpoint lives outside /api/v1 and has no MCP tool, so this
    is provisioning done over raw HTTP — the package ops are what's under test.
    """
    r = httpx.put(
        f"{os.environ['GITEA_URL']}/api/packages/{ADMIN_USER}/generic/"
        f"{name}/{version}/{filename}",
        headers={"Authorization": f"token {os.environ['GITEA_TOKEN']}"},
        content=content,
        timeout=30.0,
    )
    r.raise_for_status()


# ── Fixtures ──────────────────────────────────────────────────────────────────


def _require_env(*names: str) -> dict[str, str]:
    """Skip the calling integration test if any of the env vars are missing.

    Docker lifecycle now lives in `scripts/bootstrap.py` (run via
    `npm run gitea:up`); this fixture path just reads what bootstrap produced.
    """
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        pytest.skip(
            f"Integration test requires env vars {missing}. "
            f"Run `npm run gitea:up` to start Gitea and write tests/.env."
        )
    return {n: os.environ[n] for n in names}


@pytest.fixture(scope="session")
def gitea_instance() -> str:
    """URL of the running Gitea instance. Skips the test if env isn't set."""
    return _require_env("GITEA_URL")["GITEA_URL"]


@pytest.fixture(scope="session")
def gitea_token(gitea_instance: str) -> str:
    """API token for the test instance. Skips the test if env isn't set."""
    return _require_env("GITEA_TOKEN")["GITEA_TOKEN"]


@pytest.fixture(scope="session")
def configure_env(gitea_instance: str, gitea_token: str):
    """Set process env so GiteaClient picks up the right URL/token,
    enable public-repo creation for the test session, and reset the cached
    client so subsequent calls use the fresh settings.

    Mirrors the pre-split behaviour exactly — without `set_allow_public(True)`,
    integration tests that create public repos start failing because the
    production-default safeguard kicks in.
    """
    os.environ["GITEA_URL"] = gitea_instance
    os.environ["GITEA_TOKEN"] = gitea_token

    set_allow_public(True)
    _tools._client = None
    yield
    _tools._client = None
    set_allow_public(False)


@pytest.fixture(scope="session")
def agent(configure_env) -> AgentSimulator:
    """Return an AgentSimulator connected to the test Gitea instance."""
    return AgentSimulator()


@pytest.fixture
def ssh_pubkey() -> str:
    # Fresh temp dir per call so re-runs don't hit ssh-keygen's overwrite prompt.
    path = Path(tempfile.mkdtemp()) / "key"
    subprocess.run(
        ["ssh-keygen", "-t", "ed25519", "-f", str(path), "-N", "", "-q"],
        check=True,
    )
    return path.with_suffix(".pub").read_text().strip()
