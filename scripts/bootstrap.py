#!/usr/bin/env python3
"""Wait for the test Gitea container to be ready and seed an admin + API token.

Usage: `uv run python scripts/bootstrap.py`

Exports:
  - Writes GITEA_URL, GITEA_TOKEN, GITEA_ADMIN_USER, GITEA_ADMIN_PASSWORD
    to `tests/.env` for pytest + interactive shells.
  - Prints the token to stdout on success.

Idempotent: safe to re-run.
  - If `tests/.env` already holds a token that still works against the running
    instance (`GET /api/v1/version` with `Authorization: token ...` returns
    200), the script no-ops and prints the existing token.
  - Otherwise it deletes the named token (ignoring 404) and POSTs a fresh one,
    overwriting `tests/.env`.

Both DELETE and POST against `/users/{user}/tokens` require HTTP Basic auth
(admin user/password) — token-based auth is rejected for these endpoints.
"""

from __future__ import annotations

import subprocess
import sys
import time
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = ROOT / "tests" / "docker-compose.yml"
ENV_FILE = ROOT / "tests" / ".env"

GITEA_URL = "http://localhost:3000"
ADMIN_USER = "testadmin"
ADMIN_PASS = "testadmin1234"
ADMIN_EMAIL = "admin@test.local"
TOKEN_NAME = "test-token"
READY_TIMEOUT = 120  # seconds


def _compose(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["docker", "compose", "-f", str(COMPOSE_FILE), *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _docker_exec(*args: str) -> subprocess.CompletedProcess:
    return _compose("exec", "-T", "-u", "git", "gitea", *args)


def wait_for_ready(url: str, timeout: int) -> None:
    """Poll /api/v1/version until Gitea returns 200."""
    deadline = time.time() + timeout
    last_error: str = "(no attempt yet)"
    attempts = 0
    while time.time() < deadline:
        attempts += 1
        try:
            r = httpx.get(f"{url}/api/v1/version", timeout=5)
            if r.status_code == 200:
                elapsed = int(timeout - (deadline - time.time()))
                print(f"[bootstrap] ready after {elapsed}s ({attempts} attempts)")
                return
            last_error = f"HTTP {r.status_code}"
        except Exception as e:  # noqa: BLE001 - container not up yet
            # no-report: container not up yet is expected; last error goes into the TimeoutError
            last_error = type(e).__name__
        if attempts % 6 == 0:
            elapsed = int(timeout - (deadline - time.time()))
            print(f"[bootstrap] waiting... {elapsed}s, last: {last_error}")
        time.sleep(2)
    raise TimeoutError(f"{url} did not become ready in {timeout}s (last: {last_error})")


def ensure_admin_user() -> None:
    """Create the admin user via `gitea admin user create` inside the container.

    Ignores 'already exists' so re-running is safe."""
    result = _docker_exec(
        "gitea", "admin", "user", "create",
        "--username", ADMIN_USER,
        "--password", ADMIN_PASS,
        "--email", ADMIN_EMAIL,
        "--admin",
        "--must-change-password=false",
    )
    if result.returncode == 0:
        print(f"[bootstrap] created admin user {ADMIN_USER!r}")
        return
    combined = (result.stdout or "") + (result.stderr or "")
    if "already exists" in combined or "user already exists" in combined:
        print(f"[bootstrap] admin user {ADMIN_USER!r} already exists")
        return
    raise RuntimeError(
        f"Failed to create admin user: stdout={result.stdout!r} stderr={result.stderr!r}"
    )


def existing_token_works(url: str) -> str | None:
    """If tests/.env carries a token that authenticates, return it; else None."""
    if not ENV_FILE.exists():
        return None
    token: str | None = None
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line.startswith("GITEA_TOKEN="):
            token = line.split("=", 1)[1].strip()
            break
    if not token:
        return None
    try:
        # Probe a write-scoped endpoint so we don't accept a token that's
        # missing the scopes we need for the test suite — `/user` requires
        # any non-anonymous token.
        r = httpx.get(
            f"{url}/api/v1/user",
            headers={"Authorization": f"token {token}"},
            timeout=5,
        )
        if r.status_code == 200:
            return token
    except Exception:  # noqa: BLE001 - any failure means "re-mint"
        # no-report: an unusable cached token just means the caller mints a fresh one
        return None
    return None


def delete_existing_token(url: str, name: str) -> None:
    """DELETE the named token if it exists. Uses HTTP Basic auth — token
    auth is rejected by /users/{user}/tokens endpoints."""
    r = httpx.delete(
        f"{url}/api/v1/users/{ADMIN_USER}/tokens/{name}",
        auth=(ADMIN_USER, ADMIN_PASS),
        timeout=10,
    )
    if r.status_code in (200, 204):
        print(f"[bootstrap] deleted stale token {name!r}")
    elif r.status_code == 404:
        # Token doesn't exist — that's fine, nothing to delete.
        pass
    else:
        # Don't abort; the subsequent POST may still succeed if 422 was about
        # something else, but surface the situation for diagnosis.
        print(
            f"[bootstrap] WARN: unexpected status {r.status_code} from DELETE "
            f"tokens/{name}: {r.text}"
        )


def create_token(url: str, name: str) -> str:
    r = httpx.post(
        f"{url}/api/v1/users/{ADMIN_USER}/tokens",
        json={"name": name, "scopes": ["all"]},
        auth=(ADMIN_USER, ADMIN_PASS),
        timeout=10,
    )
    r.raise_for_status()
    data = r.json()
    token = data.get("sha1") or data.get("token")
    if not token:
        raise RuntimeError(f"Token endpoint returned no token field: {data}")
    return token


def write_env_file(url: str, token: str) -> None:
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text(
        f"# Written by scripts/bootstrap.py — consumed by tests and local shells\n"
        f"GITEA_URL={url}\n"
        f"GITEA_TOKEN={token}\n"
        f"GITEA_ADMIN_USER={ADMIN_USER}\n"
        f"GITEA_ADMIN_PASSWORD={ADMIN_PASS}\n"
    )
    print(f"[bootstrap] wrote {ENV_FILE}")


def main() -> int:
    print(f"[bootstrap] target: {GITEA_URL}")

    try:
        wait_for_ready(GITEA_URL, READY_TIMEOUT)
    except TimeoutError as e:
        # no-report: top-level CLI failure report - stderr message plus exit code 1
        print(f"[bootstrap] FAILED: {e}", file=sys.stderr)
        return 1

    ensure_admin_user()

    existing = existing_token_works(GITEA_URL)
    if existing:
        print("[bootstrap] existing token still valid — no-op")
        # Refresh the env file in case admin user/password changed; token stays.
        write_env_file(GITEA_URL, existing)
        print(f"[bootstrap] OK — token: {existing}")
        return 0

    delete_existing_token(GITEA_URL, TOKEN_NAME)
    token = create_token(GITEA_URL, TOKEN_NAME)
    write_env_file(GITEA_URL, token)
    print(f"[bootstrap] OK — token: {token}")
    print(f"[bootstrap] For interactive shell: `source {ENV_FILE.relative_to(ROOT)}`")
    return 0


if __name__ == "__main__":
    sys.exit(main())
