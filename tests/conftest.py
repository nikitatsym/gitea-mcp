"""Test infrastructure: Docker Compose Gitea instance + MCP agent simulator."""

from __future__ import annotations

import json
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

from gitea_mcp.server import mcp

COMPOSE_DIR = Path(__file__).parent
GITEA_URL = "http://localhost:3000"
ADMIN_USER = "testadmin"
ADMIN_PASS = "testadmin1234"
ADMIN_EMAIL = "admin@test.local"


# ── Docker Compose lifecycle ──────────────────────────────────────────────────


def _compose(*args: str):
    subprocess.run(
        ["docker", "compose", *args],
        cwd=COMPOSE_DIR,
        check=True,
        capture_output=True,
    )


def _docker_exec(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "exec", "-T", "-u", "git", "gitea", *args],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
    )


def _wait_for_gitea(timeout: int = 90):
    """Poll Gitea until it's ready."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            r = httpx.get(f"{GITEA_URL}/api/v1/version", timeout=5)
            if r.status_code == 200:
                return
        except Exception:
            pass
        time.sleep(2)
    raise TimeoutError("Gitea did not start in time")


def _create_admin_user() -> str:
    """Create admin user via gitea CLI and return API token."""
    # Create admin user via `gitea admin user create` inside the container
    result = _docker_exec(
        "gitea", "admin", "user", "create",
        "--username", ADMIN_USER,
        "--password", ADMIN_PASS,
        "--email", ADMIN_EMAIL,
        "--admin",
        "--must-change-password=false",
    )
    # Ignore error if user already exists
    if result.returncode != 0 and "already exists" not in (result.stderr + result.stdout):
        raise RuntimeError(f"Failed to create admin user: stdout={result.stdout} stderr={result.stderr}")

    # Create API token using basic auth
    r = httpx.post(
        f"{GITEA_URL}/api/v1/users/{ADMIN_USER}/tokens",
        json={"name": "test-token", "scopes": ["all"]},
        auth=(ADMIN_USER, ADMIN_PASS),
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    return data.get("sha1") or data.get("token")


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
        """Call an MCP tool by name and return parsed result."""
        fn = self._tools.get(tool_name)
        if fn is None:
            raise ValueError(f"Unknown tool: {tool_name}. Available: {sorted(self._tools.keys())}")

        result_str = fn(**kwargs)

        self.call_log.append({"tool": tool_name, "kwargs": kwargs, "result": result_str})

        try:
            return json.loads(result_str)
        except (json.JSONDecodeError, TypeError):
            return result_str

    def call_raw(self, tool_name: str, **kwargs) -> str:
        """Call an MCP tool and return raw string result."""
        fn = self._tools.get(tool_name)
        if fn is None:
            raise ValueError(f"Unknown tool: {tool_name}")

        result_str = fn(**kwargs)
        self.call_log.append({"tool": tool_name, "kwargs": kwargs, "result": result_str})
        return result_str

    @property
    def total_calls(self) -> int:
        return len(self.call_log)

    @property
    def unique_tools_used(self) -> set[str]:
        return {e["tool"] for e in self.call_log}

    def print_log(self):
        """Print the call log for debugging."""
        for i, entry in enumerate(self.call_log):
            print(f"\n[{i}] {entry['tool']}({entry['kwargs']})")
            result = entry["result"]
            if len(str(result)) > 200:
                print(f"  => {str(result)[:200]}...")
            else:
                print(f"  => {result}")


# ── Fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture(scope="session")
def gitea_instance():
    """Start Gitea via Docker Compose, yield the URL, then tear down."""
    _compose("up", "-d")
    try:
        _wait_for_gitea()
        yield GITEA_URL
    finally:
        _compose("down", "-v")


@pytest.fixture(scope="session")
def gitea_token(gitea_instance):
    """Create admin user and return API token."""
    return _create_admin_user()


@pytest.fixture(scope="session")
def configure_env(gitea_instance, gitea_token):
    """Set environment variables for GiteaClient."""
    import os

    os.environ["GITEA_URL"] = gitea_instance
    os.environ["GITEA_TOKEN"] = gitea_token

    # Reset the cached client so it picks up new env
    import gitea_mcp.server as srv

    srv._client = None
    yield
    srv._client = None


@pytest.fixture(scope="session")
def agent(configure_env) -> AgentSimulator:
    """Return an AgentSimulator connected to the test Gitea instance."""
    return AgentSimulator()
