"""Expected failures reach the MCP caller as result data, not as exceptions.

An exception crossing the tool boundary is reported by MCP clients as a
contextless execution failure, so the Gitea status, body, and failing request
would all be lost. `_coerce_call`'s own validation contract is covered by
test_coerce.py; this file pins the dispatch boundary, the async waiter path,
the ROOT tool, and the programming-error edge.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from waiter_fixtures import seed, tools_state

from gitea_mcp import server


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    with tools_state(monkeypatch):
        yield


def _root_tool(name):
    """The registered ROOT tool, i.e. exactly what an MCP client calls."""
    return server.mcp._tool_manager._tools[name].fn


def _group_tool(name: str):
    """The registered group tool, i.e. exactly what an MCP client calls."""
    return server.mcp._tool_manager._tools[name].fn


def _responding(status: int, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body)

    return handler


def _refusing(exc: httpx.RequestError):
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc

    return handler


def test_api_error_keeps_status_method_path_and_body():
    seed(_responding(404, {"message": "repo does not exist"}))

    result = server._dispatch("GetRepo", "gitea_read", {"owner": "me", "repo": "gone"})

    assert result == {
        "error": (
            "Gitea API 404 GET /repos/me/gone: {'message': 'repo does not exist'}"
        )
    }


def test_transport_error_names_request_without_query():
    seed(_refusing(httpx.ConnectError("Connection refused")))

    result = server._dispatch(
        "ListIssues",
        "gitea_read",
        {"owner": "me", "repo": "proj", "assignee": "must-not-leak"},
    )

    assert result == {
        "error": (
            "Gitea request failed: GET /api/v1/repos/me/proj/issues: "
            "ConnectError: Connection refused"
        )
    }
    assert "must-not-leak" not in repr(result)


def test_missing_required_param_is_reported():
    seed(_responding(200, {}))

    result = server._dispatch("GetRepo", "gitea_read", {"owner": "me"})

    assert "repo" in result["error"]


def test_registered_group_reports_invalid_help_input():
    seed(_responding(200, {}))

    result = asyncio.run(
        _group_tool("gitea_read")(operation="help", params={"search": 1})
    )

    assert result == {"error": "help parameter 'search' must be a string"}


def test_async_waiter_failure_maps_at_await_time():
    """A waiter's body runs only once awaited - outside `_dispatch`'s own guard."""
    seed(_responding(500, {"message": "internal server error"}))

    async def flow():
        coro = server._dispatch(
            "WorkflowRunsWait",
            "gitea_read",
            {
                "owner": "me", "repo": "proj", "run_id": 42,
                "timeout": 60.0, "interval": 0.01, "max_poll_failures": 1,
            },
        )
        assert asyncio.iscoroutine(coro)
        return await coro

    result = asyncio.run(flow())

    assert "Gitea API 500" in result["error"]
    assert "/repos/me/proj/actions/runs/42" in result["error"]


def test_root_version_reports_api_failure():
    """gitea_version bypasses _dispatch; the registration seam guards it."""
    seed(_responding(503, {"message": "service unavailable"}))

    result = _root_tool("gitea_version")()

    assert result == {
        "error": (
            "Gitea API 503 GET /version: {'message': 'service unavailable'}"
        )
    }


def test_root_version_reports_transport_failure():
    seed(_refusing(httpx.ReadTimeout("timed out")))

    result = _root_tool("gitea_version")()

    assert result == {
        "error": (
            "Gitea request failed: GET /api/v1/version: ReadTimeout: timed out"
        )
    }


def test_root_version_keeps_its_success_shape():
    seed(_responding(200, {"version": "1.24.0"}))

    result = _root_tool("gitea_version")()

    assert result["service"] == {"version": "1.24.0"}
    assert result["mcp"]


def test_programming_error_still_propagates(monkeypatch):
    """A bug must stay a crash: only expected failures become result data."""
    def boom(owner: str, repo: str):
        """Synthetic op that hits a bug instead of an expected failure."""
        raise AttributeError("'NoneType' object has no attribute 'get'")

    boom._params_model = server._build_params_model(boom)
    monkeypatch.setitem(server._group_ops["gitea_read"], "GetRepo", boom)

    with pytest.raises(AttributeError):
        server._dispatch("GetRepo", "gitea_read", {"owner": "me", "repo": "proj"})


def test_cancellation_still_propagates_from_registered_group(monkeypatch):
    seed(_responding(200, {}))

    async def cancelled(owner: str, repo: str):
        raise asyncio.CancelledError

    cancelled._params_model = server._build_params_model(cancelled)
    monkeypatch.setitem(server._group_ops["gitea_read"], "GetRepo", cancelled)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            _group_tool("gitea_read")(
                operation="GetRepo", params={"owner": "me", "repo": "proj"},
            )
        )
