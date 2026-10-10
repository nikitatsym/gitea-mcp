"""Shared response formatting and validation helpers.

See build_payload for request payload omission.
"""

import os
import re
from typing import Literal

from .config import allow_public

# Callers annotate the parameter with the same literal set, so returning a bare
# `str` here would widen it back out at every assignment site.
Visibility = Literal["public", "limited", "private"] | None

_BRIEF_MAX = int(os.environ.get("MCP_GITEA_BRIEF_MAX", "100"))

_BRIEF_RE = re.compile(r"<brief>(.*?)</brief>", re.DOTALL)


def _extract_brief(body: str | None) -> str | None:
    """Extract <brief>...</brief> summary from body text."""
    if not body:
        return None
    m = _BRIEF_RE.search(body)
    return m.group(1).strip() if m else None


def validate_brief(body: str | None) -> None:
    """Raise ValueError if brief requirement is on and body lacks a valid <brief> tag."""
    if _BRIEF_MAX == 0:
        return
    if not body or not _BRIEF_RE.search(body):
        raise ValueError(
            "body must contain a <brief>one-line summary</brief> tag. "
            "Add it at the top of the body text."
        )
    brief = _extract_brief(body)
    if brief and len(brief) > _BRIEF_MAX:
        raise ValueError(
            f"<brief> too long: {len(brief)} chars, max {_BRIEF_MAX}. "
            "Keep it to a concise one-liner."
        )


def enforce_private(private: bool | None) -> bool | None:
    """Block non-private repos unless --allow-public was passed."""
    if not allow_public() and private is not True:
        raise ValueError(
            "Public repos not allowed. Set private=true explicitly."
        )
    return private


def enforce_visibility(visibility: Visibility) -> Visibility:
    """Block non-private orgs unless --allow-public was passed."""
    if not allow_public() and visibility != "private":
        raise ValueError(
            "Public orgs not allowed. Set visibility='private' explicitly."
        )
    return visibility


def unwrap_response(data):
    """Unwrap Gitea search results and represent empty responses as success."""
    if data is None:
        return {"status": "ok"}
    # Gitea search endpoints wrap results in {"ok": true, "data": [...]}
    if isinstance(data, dict) and "ok" in data and "data" in data:
        data = data["data"]
    return data


def build_payload(
    local_vars: dict,
    exclude=(),
    rename: dict | None = None,
) -> dict:
    """Build a JSON body or query-params dict from function locals.

    Drops keys whose value is `None`, plus any key listed in `exclude`: an
    optional parameter left at its `None` default stays off the wire.
    The optional `rename` map translates Python parameter names to API field
    names (e.g. `{"status_types": "status-types"}`). Use as:

        body = build_payload(locals(), exclude=("owner", "repo"))
        params = build_payload(locals(), exclude=("brief",),
                               rename={"status_types": "status-types"})

    Adding a new optional parameter forwards it automatically through
    build_payload(locals()), subject to the exclusion and omission rules;
    there is no `if x is not None` to forget.
    """
    excl = set(exclude)
    rmap = rename or {}
    out: dict = {}
    for k, v in local_vars.items():
        if k in excl or v is None:
            continue
        out[rmap.get(k, k)] = v
    return out


def attach_commit_author(body: dict, author_name: str | None, author_email: str | None) -> None:
    """Attach Gitea's `author` object to a contents-API commit body, in place.

    Gitea nests the git author override one level down, and omits the whole
    object when neither half was given: sending `{"author": {}}` makes it
    record an empty author rather than fall back to the token's identity.
    Either half alone is legal - Gitea fills the other from the account.
    """
    if author_name is None and author_email is None:
        return
    author: dict = {}
    if author_name is not None:
        author["name"] = author_name
    if author_email is not None:
        author["email"] = author_email
    body["author"] = author


# ── Slim functions ───────────────────────────────────────────────────────────


def _slim_issue(issue: dict) -> dict:
    """Strip an issue/PR to essential fields for list views."""
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "state": issue.get("state"),
        "brief": _extract_brief(issue.get("body")),
        "labels": [label["name"] for label in issue.get("labels") or []],
        "assignees": [a["login"] for a in issue.get("assignees") or []],
        "updated_at": issue.get("updated_at"),
    }


def slim_issues(data) -> list:
    """Apply _slim_issue to a list of issues/PRs."""
    if isinstance(data, list):
        return [_slim_issue(i) for i in data]
    return data


def _slim_repo(repo: dict) -> dict:
    """Strip a repo to essential fields for list views."""
    return {
        "full_name": repo.get("full_name"),
        "description": repo.get("description"),
        "private": repo.get("private"),
        "fork": repo.get("fork"),
        "language": repo.get("language"),
        "stars_count": repo.get("stars_count"),
        "open_issues_count": repo.get("open_issues_count"),
        "default_branch": repo.get("default_branch"),
        "updated_at": repo.get("updated_at"),
    }


def slim_repos(data) -> list:
    if isinstance(data, list):
        return [_slim_repo(r) for r in data]
    return data


def _slim_notification(n: dict) -> dict:
    """Strip a notification to essential fields."""
    return {
        "id": n.get("id"),
        "repo": n["repository"]["full_name"] if n.get("repository") else None,
        "subject_type": n.get("subject", {}).get("type"),
        "subject_title": n.get("subject", {}).get("title"),
        "subject_url": n.get("subject", {}).get("url"),
        "unread": n.get("unread"),
        "updated_at": n.get("updated_at"),
    }


def slim_notifications(data) -> list:
    if isinstance(data, list):
        return [_slim_notification(n) for n in data]
    return data


def _slim_comment(c: dict) -> dict:
    """Strip a comment to essential fields."""
    return {
        "id": c.get("id"),
        "user": c["user"]["login"] if c.get("user") else None,
        "body": c.get("body"),
        "created_at": c.get("created_at"),
        "updated_at": c.get("updated_at"),
    }


def slim_comments(data) -> list:
    if isinstance(data, list):
        return [_slim_comment(c) for c in data]
    return data


def _slim_commit(c: dict) -> dict:
    """Strip a commit to essential fields."""
    commit = c.get("commit", {})
    return {
        "sha": c.get("sha", "")[:12],
        "message": commit.get("message", "").split("\n")[0],
        "author": commit.get("author", {}).get("name"),
        "date": commit.get("author", {}).get("date"),
    }


def slim_commits(data) -> list:
    if isinstance(data, list):
        return [_slim_commit(c) for c in data]
    return data


def slim_workflow_run(run: dict) -> dict:
    """Strip a workflow run to essential fields."""
    return {
        "id": run.get("id"),
        "display_title": run.get("display_title"),
        "status": run.get("status"),
        "conclusion": run.get("conclusion"),
        "event": run.get("event"),
        "head_branch": run.get("head_branch"),
        "head_sha": (run.get("head_sha") or "")[:12],
        "run_number": run.get("run_number"),
        "path": run.get("path"),
        "started_at": run.get("started_at"),
        "completed_at": run.get("completed_at"),
    }


def slim_workflow_runs(data):
    if isinstance(data, dict) and "workflow_runs" in data:
        return [slim_workflow_run(r) for r in data["workflow_runs"]]
    if isinstance(data, list):
        return [slim_workflow_run(r) for r in data]
    return data


def slim_job(job: dict) -> dict:
    """Strip a workflow job to essential fields."""
    return {
        "id": job.get("id"),
        "name": job.get("name"),
        "status": job.get("status"),
        "conclusion": job.get("conclusion"),
        "run_id": job.get("run_id"),
        "started_at": job.get("started_at"),
        "completed_at": job.get("completed_at"),
        "steps": [
            {"name": s.get("name"), "status": s.get("status"), "conclusion": s.get("conclusion")}
            for s in (job.get("steps") or [])
        ],
    }


def slim_jobs(data):
    if isinstance(data, dict) and "jobs" in data:
        return [slim_job(j) for j in data["jobs"]]
    if isinstance(data, list):
        return [slim_job(j) for j in data]
    return data
