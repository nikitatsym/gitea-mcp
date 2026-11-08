"""Workflow run results: actions return the compact run shape read ops use, the logs archive comes back base64-encoded."""

from __future__ import annotations

import asyncio
import base64

import httpx
import pytest
from waiter_fixtures import RUN_PATH, run_payload, seed, tools_state

from gitea_mcp import server, tools
from gitea_mcp.client import GiteaError


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    with tools_state(monkeypatch):
        yield


def test_rerun_workflow_run_returns_slim_run():
    payload = {
        **run_payload(42, "queued"),
        "run_attempt": 2,
        "repository_id": 9,
        "repository": {
            "id": 9,
            "name": "proj",
            "full_name": "me/proj",
            "private": True,
            "owner": {"id": 3, "login": "me", "full_name": "Test User"},
            "html_url": "https://gitea.example.com/me/proj",
        },
        "head_repository": {"id": 9, "full_name": "me/proj"},
        "actor": {"id": 3, "login": "me", "full_name": "Test User"},
        "trigger_actor": {"id": 3, "login": "me", "full_name": "Test User"},
        "previous_attempt_url": "https://gitea.example.com/api/v1/repos/me/proj/actions/runs/42/attempts/1",
        "html_url": "https://gitea.example.com/me/proj/actions/runs/1",
        "url": "https://gitea.example.com/api/v1/repos/me/proj/actions/runs/42",
        "pull_requests": [],
    }
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(201, json=payload)

    seed(handler)
    result = asyncio.run(
        server._dispatch(
            "RerunWorkflowRun", "gitea_execute",
            {"owner": "me", "repo": "proj", "run_id": 42},
        )
    )

    assert requests == [("POST", f"{RUN_PATH}/rerun")]
    assert result == {
        **run_payload(42, "queued"),
        "head_sha": "deadbeefdead",
    }


def test_rerun_workflow_run_propagates_api_error():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(409, json={"message": "workflow run is still running"})

    seed(handler)
    with pytest.raises(GiteaError) as raised:
        tools.rerun_workflow_run(owner="me", repo="proj", run_id=42)

    assert raised.value.status == 409
    assert raised.value.method == "POST"
    assert raised.value.path == "/repos/me/proj/actions/runs/42/rerun"
    assert raised.value.body == {"message": "workflow run is still running"}


@pytest.mark.parametrize(
    ("operation", "suffix"),
    [
        ("CancelWorkflowRun", "cancel"),
        ("ForceCancelWorkflowRun", "force-cancel"),
        ("ApproveWorkflowRun", "approve"),
    ],
)
def test_run_state_actions_return_slim_run(operation, suffix):
    payload = {
        **run_payload(42, "completed", "cancelled"),
        "repository": {"id": 9, "full_name": "me/proj"},
        "actor": {"id": 3, "login": "me"},
        "html_url": "https://gitea.example.com/me/proj/actions/runs/1",
    }
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, json=payload)

    seed(handler)
    result = asyncio.run(
        server._dispatch(
            operation, "gitea_execute",
            {"owner": "me", "repo": "proj", "run_id": 42},
        )
    )

    assert requests == [("POST", f"{RUN_PATH}/{suffix}")]
    assert result == {
        **run_payload(42, "completed", "cancelled"),
        "head_sha": "deadbeefdead",
    }


def test_download_workflow_run_logs_returns_the_zip_base64():
    archive = b"PK\x03\x04 not really a zip, the op passes bytes through"
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.method, request.url.path))
        return httpx.Response(200, content=archive, headers={"Content-Type": "application/zip"})

    seed(handler)
    result = tools.download_workflow_run_logs(owner="me", repo="proj", run_id=42)

    assert requests == [("GET", f"{RUN_PATH}/logs")]
    assert result["run_id"] == 42
    assert result["size_in_bytes"] == len(archive)
    assert base64.b64decode(result["zip_base64"]) == archive
