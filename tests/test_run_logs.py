"""get_workflow_run_logs: the run's zip archive, unpacked into text per log file."""

from __future__ import annotations

import asyncio
import io
import zipfile
from collections.abc import Iterator

import httpx
import pytest
from waiter_fixtures import RUN_PATH, seed, tools_state

from gitea_mcp import server, tools

LOGS_PATH = f"{RUN_PATH}/logs"
MIB = 2**20


@pytest.fixture(autouse=True)
def _reset_state(monkeypatch):
    with tools_state(monkeypatch):
        yield


def _archive(*entries: tuple[str, str | bytes], compression: int = zipfile.ZIP_STORED) -> bytes:
    """A zip of the entries; stored by default, so tests can corrupt entry bytes in place."""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=compression) as zf:
        for name, payload in entries:
            zf.writestr(name, payload)
    return buf.getvalue()


def _serve(archive: bytes) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == LOGS_PATH:
            return httpx.Response(200, content=archive, headers={"content-type": "application/zip"})
        return httpx.Response(404, json={"message": "not found"})

    seed(handler)


def _run_logs(**params):
    return asyncio.run(
        server._dispatch(
            "GetWorkflowRunLogs",
            "gitea_read",
            {"owner": "me", "repo": "proj", "run_id": 42, **params},
        )
    )


def test_each_log_file_comes_back_as_text_under_giteas_name():
    _serve(_archive(
        ("ci-build-101.log", "compiling\nbuilt\n"),
        ("ci-test (ubuntu_latest)-102.log", "ran 3 tests\n"),
    ))

    assert _run_logs() == [
        {"file": "ci-build-101.log", "log": "compiling\nbuilt"},
        {"file": "ci-test (ubuntu_latest)-102.log", "log": "ran 3 tests"},
    ]


def test_filter_then_tail_apply_to_each_file_separately():
    _serve(_archive(
        ("ci-build-101.log", "ok 1\nERROR one\nok 2\nerror two\nok 3\n"),
        ("ci-test-102.log", "error three\nok 4\n"),
    ))

    assert [entry["log"] for entry in _run_logs(filter="error", tail=1)] == [
        "error two",
        "error three",
    ]


def test_tail_zero_keeps_every_line():
    _serve(_archive(("ci-build-101.log", "a\nb\nc\n")))

    assert _run_logs(tail=0)[0]["log"] == "a\nb\nc"


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_files_sharing_a_name_are_read_one_by_one():
    _serve(_archive(("ci-matrix-__.log", "JOB SEVEN\n"), ("ci-matrix-__.log", "JOB EIGHT\n")))

    assert [entry["log"] for entry in _run_logs()] == ["JOB SEVEN", "JOB EIGHT"]


def test_a_file_that_is_not_utf8_is_an_error_naming_it():
    _serve(_archive(("ci-build-101.log", b"prefix\xffsuffix\n")))

    error = _run_logs()["error"]

    assert error.startswith("run 42 of me/proj: log file 'ci-build-101.log': ")
    assert "can't decode byte 0xff" in error


def test_a_corrupt_file_is_an_error_naming_it():
    archive = _archive(("ci-build-101.log", "intact log line\n"))
    _serve(archive.replace(b"intact", b"broken"))

    error = _run_logs()["error"]

    assert error.startswith("run 42 of me/proj: log file 'ci-build-101.log': ")
    assert "Bad CRC-32" in error


def test_an_archive_that_is_not_a_zip_is_an_error_naming_the_run():
    _serve(b"<html>proxy error</html>")

    assert _run_logs()["error"].startswith(
        "run 42 of me/proj: the log archive is not a readable zip: "
    )


def test_a_run_without_logs_reports_giteas_404():
    seed(lambda request: httpx.Response(404, json={"message": "logs not found"}))

    assert _run_logs()["error"].startswith(
        f"Gitea API 404 GET {LOGS_PATH.removeprefix('/api/v1')}"
    )


def test_an_archive_past_the_cap_is_an_error_and_is_not_read_to_the_end(monkeypatch):
    monkeypatch.setattr(tools, "_RUN_LOGS_MAX_BYTES", MIB)
    sent: list[int] = []

    def body() -> Iterator[bytes]:
        for i in range(8):
            sent.append(i)
            yield b"x" * (MIB // 4)

    seed(lambda request: httpx.Response(200, content=body()))

    error = _run_logs()["error"]

    assert error == (
        "run 42 of me/proj: the log archive exceeds the 1 MiB cap; "
        "read the jobs one by one with get_workflow_job_logs"
    )
    assert len(sent) < 8


def test_unpacked_logs_past_the_cap_together_are_an_error(monkeypatch):
    monkeypatch.setattr(tools, "_RUN_LOGS_MAX_BYTES", MIB)
    # Each file stays under the cap; the two together do not.
    half = "a" * (MIB // 2 + 1)
    archive = _archive(
        ("ci-build-101.log", half), ("ci-test-102.log", half), compression=zipfile.ZIP_DEFLATED
    )
    assert len(archive) < MIB
    _serve(archive)

    assert _run_logs()["error"] == (
        "run 42 of me/proj: the unpacked log text exceeds the 1 MiB cap; "
        "read the jobs one by one with get_workflow_job_logs"
    )
