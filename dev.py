#!/usr/bin/env python3
"""gitea-mcp dev script: lint / test / e2e / check (dev-script spec).

One entry point for the same checks locally and in CI; `check` = lint +
test and is what the pre-commit hook and CI run. e2e boots the dockerized
Gitea from tests/docker-compose.yml (digest-pinned image, no pull when it
is already local) and leaves it running for the next iteration; tear down
with `npm run gitea:down`.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent
_COMPOSE = ["docker", "compose", "-f", str(_ROOT / "tests" / "docker-compose.yml")]


def _run(cmd: list[str]) -> int:
    print(f"dev.py: {shlex.join(cmd)}", flush=True)
    return subprocess.run(cmd, cwd=_ROOT, check=False).returncode


def _aggregate(codes: list[int]) -> int:
    return next((c for c in codes if c != 0), 0)


def lint() -> int:
    # Run both so one failing linter never hides the other's findings.
    # ruff is version-pinned via the dev group (a floating linter is a
    # drifting gate); tackbox tracks @latest per its consumer contract.
    return _aggregate(
        [
            _run(["uv", "run", "ruff", "check", "."]),
            _run(["uvx", "tackbox@latest", "lint", "."]),
        ]
    )


def _gitea_up() -> int:
    code = _run(_COMPOSE + ["up", "-d", "gitea"])
    if code:
        return code
    return _run(["uv", "run", "python", "scripts/bootstrap.py"])


def e2e() -> int:
    # The integration marker is this repo's e2e surface: the suite drives the
    # MCP tools against a real Gitea end to end.
    code = _gitea_up()
    if code:
        return code
    return _run(["uv", "run", "pytest", "-m", "integration", "-q"])


def test() -> int:
    return _aggregate(
        [
            _run(["uv", "run", "pytest", "-m", "not integration", "-q"]),
            e2e(),
        ]
    )


def install_hook() -> int:
    """Install the pre-commit hook. Idempotent."""
    if (_ROOT / ".pre-commit-config.yaml").exists():
        return _run(["uv", "run", "pre-commit", "install"])
    print("no tracked hook: expected .pre-commit-config.yaml", file=sys.stderr)
    return 1


def _hook_ready() -> bool:
    return (_ROOT / ".git" / "hooks" / "pre-commit").exists()


def _hook_hint() -> None:
    # A fresh clone gates nothing until asked; CI has no use for a hook.
    if not os.environ.get("CI") and not _hook_ready():
        print("hint: `python dev.py hook` installs the pre-commit gate", file=sys.stderr)


def check() -> int:
    _hook_hint()
    # Run both so one failing gate never hides the other; aggregate non-zero.
    return _aggregate([lint(), test()])


_COMMANDS = {"lint": lint, "test": test, "e2e": e2e, "check": check, "hook": install_hook}


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] not in _COMMANDS:
        print(f"usage: dev.py {{{'|'.join(_COMMANDS)}}}", file=sys.stderr)
        return 2
    return _COMMANDS[args[0]]()


if __name__ == "__main__":
    sys.exit(main())
