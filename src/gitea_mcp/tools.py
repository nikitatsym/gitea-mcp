"""Gitea tool operations. All public functions are auto-registered as MCP tools."""

import asyncio
import base64
import logging
import re
import time
from importlib.metadata import version as _pkg_version
from typing import Annotated, Literal

import httpx
from pydantic import Field

from .client import GiteaClient, GiteaError
from .config import get_settings
from .prepare import (
    _body,
    _enforce_private,
    _enforce_visibility,
    _ok,
    _slim_comments,
    _slim_commits,
    _slim_issues,
    _slim_job,
    _slim_jobs,
    _slim_notifications,
    _slim_repos,
    _slim_workflow_run,
    _slim_workflow_runs,
    _validate_brief,
)
from .registry import ROOT, Group, _op
from .wait_registry import (
    TERMINAL_STATUSES as _WAIT_TERMINAL,
)
from .wait_registry import (
    WAIT_REGISTRY as _WAIT_REGISTRY,
)
from .wait_registry import (
    WaitHandle as _WaitHandle,
)

_client: GiteaClient | None = None


def _get_client() -> GiteaClient:
    global _client
    if _client is None:
        _client = GiteaClient()
    return _client


_PATH_PLACEHOLDER = re.compile(r"\{(\w+)\}")


def _call(
    method: str,
    path: str,
    kw: dict,
    *,
    rename: dict | None = None,
    exclude=(),
    keep_null=(),
):
    """Make a Gitea API call, deriving path-params and body/query from `kw`.

    Placeholders in `path` (e.g. `/repos/{owner}/{repo}`) are interpolated
    from `kw` and automatically excluded from the request body/query.
    Remaining entries in `kw` become JSON body for write methods
    (POST/PUT/PATCH) or query params for GET/DELETE.

    `rename` maps Python names to API field names (e.g. snake→kebab).
    `exclude` drops internal helper args (e.g. `brief`) not meant for the
    wire. `keep_null` opts specific fields into Gitea's nullable-clear
    semantics — callers passing `None` for those fields get JSON `null` on
    the wire (instead of the value being dropped); other params default
    to `_UNSET` so omission still excludes them. See `prepare.py:_body`.

    Use this for the simple one-call pattern; for paginated, slimmed,
    text, or transformation-heavy endpoints, call the client directly.
    """
    placeholders = _PATH_PLACEHOLDER.findall(path)
    excl = set(placeholders) | set(exclude)
    formatted = path.format(**{k: kw[k] for k in placeholders})
    payload = _body(kw, exclude=excl, rename=rename, keep_null=keep_null)
    client = _get_client()
    method = method.upper()
    if method == "GET":
        return _ok(client.get(formatted, params=payload or None))
    if method == "DELETE":
        return _ok(client.delete(formatted, params=payload or None))
    if method == "POST":
        return _ok(client.post(formatted, json=payload))
    if method == "PUT":
        return _ok(client.put(formatted, json=payload))
    if method == "PATCH":
        return _ok(client.patch(formatted, json=payload))
    raise ValueError(f"Unsupported HTTP method {method!r}")


# ── Groups ────────────────────────────────────────────────────────────────────

# Operation names are `$`-placeholders resolved by `server._render_group_doc`;
# the columns line up in the rendered doc, not in this source literal.
_GROUP_USAGE = (
    "\n\n"
    "operation='$help'                        — list ops with parameter names + types.\n"
    "operation='$help' params={'search':'X'}  — same, filtered to ops whose name contains X (case-insensitive).\n"
    "operation='$schema'                      — JSON Schema for one op. params={'op': 'OpName'} or params={} to list op names.\n"
    "operation='<OpName>' params={...}       — invoke. Params validated strictly: "
    "unknown keys, wrong types, missing required → ValueError with field-level detail."
)

# ── Group policy ──────────────────────────────────────────────────────────
#
# Risk-graded scoping per the v2 MCP spec — agents choose a tool surface by
# the kind of side effect, not the HTTP verb. Aligned with gitlab-mcp's
# convention so a user moving between MCPs sees the same shape of names.
#
#   gitea_read         — GETs. Safe, read-only.
#   gitea_write        — POST/PUT/PATCH that create or update resources
#                        (issues, PRs, files, repos, labels, ...).
#   gitea_execute      — action-trigger ops with real-world side effects
#                        beyond CRUD: merging PRs, dispatching workflows,
#                        retry/cancel/rerun, mirror sync. Promoted to a
#                        separate surface so privilege boundaries can be
#                        drawn at this level rather than per-tool.
#   gitea_delete       — destructive DELETEs.
#   gitea_admin_read   — admin-scope GETs (instance-wide visibility).
#   gitea_admin_write  — admin-scope POST/PUT/PATCH/DELETE (and admin
#                        actions like `admin_run_cron_job`). Admin actions
#                        stay here rather than moving into gitea_execute
#                        because the permission boundary matters more than
#                        the verb.
#
# Token-issuance ops (`create_*_runner_token`) stay in gitea_write — they're
# functionally similar to "create a resource", and clustering them with
# `merge_pull_request` / `dispatch_workflow` would dilute the meaning of
# gitea_execute.

gitea_read = Group("gitea_read", "Gitea read operations — safe, GET-only." + _GROUP_USAGE)
gitea_write = Group(
    "gitea_write",
    "Gitea write operations — create or update resources (POST/PUT/PATCH)." + _GROUP_USAGE,
)
gitea_execute = Group(
    "gitea_execute",
    "Gitea action triggers — merge PRs, dispatch workflows, and other "
    "side-effecting actions beyond plain CRUD." + _GROUP_USAGE,
)
gitea_delete = Group("gitea_delete", "Gitea delete operations (DELETE) — destructive." + _GROUP_USAGE)
gitea_admin_read = Group("gitea_admin_read", "Gitea admin read operations (GET /admin/*)." + _GROUP_USAGE)
gitea_admin_write = Group(
    "gitea_admin_write",
    "Gitea admin write operations (POST/PUT/PATCH/DELETE /admin/*) and "
    "admin-scope actions like running cron jobs." + _GROUP_USAGE,
)

# Backward-compat aliases — pre-v2 code in this module still references
# `gitea_create` and `gitea_update`. The actual decorator calls are migrated
# to `gitea_write` below; these aliases keep import paths stable for any
# external integration that picked them up.
gitea_create = gitea_write
gitea_update = gitea_write

# ── Shared parameter annotations ─────────────────────────────────────────────
# One definition per parameter that repeats across create/edit twins, so the
# generated help and schema cannot drift between them.

_HookConfig = Annotated[dict, Field(description="Webhook config (string keys, string values). Required keys depend on hook_type. For 'gitea'/'gogs': {'url': 'https://...', 'content_type': 'json'|'form', 'secret': '...'}. Other hook types use their own URL/token fields.")]
_HookConfigPatch = Annotated[dict | None, Field(description="Replacement webhook config (string keys/values). Shape depends on the hook's type — for 'gitea'/'gogs': {'url': ..., 'content_type': 'json'|'form', 'secret': ...}.")]
_HookEvents = Annotated[list[str], Field(description="Gitea event names to subscribe to, e.g. ['push', 'pull_request', 'issues', 'create', 'delete', 'release', 'issue_comment'].")]
_HookEventsPatch = Annotated[list[str] | None, Field(description="Replacement list of Gitea event names (e.g. ['push', 'pull_request']).")]
_HookType = Annotated[Literal["gitea", "gogs", "slack", "discord", "dingtalk", "telegram", "msteams", "feishu", "matrix", "wechatwork", "packagist"], Field(description="Webhook delivery type — determines required keys in `config`.")]

_LabelIds = Annotated[list[int] | None, Field(description=(
    "Label IDs (int64) from list_repo_labels — NOT names. "
    "Calling list_repo_labels first to look up IDs is required. "
    "Passing names like ['frontend'] returns 422 from Gitea."
))]
_Assignees = Annotated[list[str] | None, Field(description="List of USERNAMES to assign (NOT user IDs / NOT display names).")]
_AssigneesPatch = Annotated[list[str] | None, Field(description="Replacement list of assignee USERNAMES (NOT user IDs / NOT display names).")]
_MilestoneId = Annotated[int | None, Field(description="Milestone integer ID from list_milestones (NOT the milestone title).")]
_MilestonePatch = Annotated[int | None, Field(description="Milestone integer ID from list_milestones (NOT the milestone title). Pass 0 to clear.")]

_Visibility = Annotated[Literal["public", "limited", "private"] | None, Field(description="Visibility level: public (anyone), limited (logged-in users), private (members only).")]
_TeamPermission = Annotated[Literal["none", "read", "write", "admin", "owner"] | None, Field(description="Access level granted to team members on the team's repos.")]

_BpEnablePush = Annotated[bool | None, Field(description="If False, nobody can push directly to the matched branch (PR-only).")]
_BpEnablePushWhitelist = Annotated[bool | None, Field(description="If True, only users listed in `push_whitelist_usernames` may push.")]
_BpEnableMergeWhitelist = Annotated[bool | None, Field(description="If True, only users in `merge_whitelist_usernames` may merge PRs into the matched branch.")]
_BpMergeWhitelistUsernames = Annotated[list[str] | None, Field(description="USERNAMES allowed to merge when merge whitelist is enabled.")]
_BpRequiredApprovals = Annotated[int | None, Field(description="Minimum number of approving reviews required before a PR may merge.")]
_BpEnableStatusCheck = Annotated[bool | None, Field(description="If True, listed status contexts must be green before merge.")]
_BpStatusCheckContexts = Annotated[list[str] | None, Field(description="Required commit-status context strings (matches the `context` field of create_commit_status).")]


# ── General ──────────────────────────────────────────────────────────────────


@_op(ROOT)
def gitea_version():
    """Get the Gitea MCP server version and service version."""
    return {"mcp": _pkg_version("gitea-mcp"), "service": _get_client().get("/version")}

@_op(gitea_read)
def get_current_user():
    """Get the currently authenticated user."""
    return _ok(_get_client().get("/user"))

# ── Users ────────────────────────────────────────────────────────────────────


@_op(gitea_read)
def search_users(
    query: Annotated[str, Field(description="Search keyword (substring match against username/full name).")],
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = None,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
):
    """Search for users by keyword."""
    return _call("GET", "/users/search", locals(), rename={"query": "q"})

@_op(gitea_read)
def get_user(username: str):
    """Get a user's profile by username."""
    return _ok(_get_client().get(f"/users/{username}"))

@_op(gitea_read)
def list_user_repos(username: str, brief: bool = True):
    """List a user's public repositories.

    brief (default True): compact view — full_name, description, language,
    stars, issues count, default_branch, updated_at.
    Set brief=False for full Gitea API response objects."""
    data = _get_client().paginate(f"/users/{username}/repos")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_read)
def list_followers(username: str):
    """List a user's followers."""
    return _ok(_get_client().paginate(f"/users/{username}/followers"))

@_op(gitea_read)
def list_following(username: str):
    """List the users that a user is following."""
    return _ok(_get_client().paginate(f"/users/{username}/following"))

@_op(gitea_write)
def follow_user(username: str):
    """Follow a user."""
    return _ok(_get_client().put(f"/user/following/{username}"))

@_op(gitea_delete)
def unfollow_user(username: str):
    """Unfollow a user."""
    return _ok(_get_client().delete(f"/user/following/{username}"))

@_op(gitea_read)
def list_user_heatmap(username: str):
    """Get a user's contribution heatmap."""
    return _ok(_get_client().get(f"/users/{username}/heatmap"))

@_op(gitea_read)
def get_user_settings():
    """Get the current user's settings."""
    return _ok(_get_client().get("/user/settings"))

@_op(gitea_read)
def check_user_following(username: str, target: str):
    """Check if a user is following another user."""
    return _ok(_get_client().get(f"/users/{username}/following/{target}"))

@_op(gitea_read)
def list_user_emails():
    """List the current user's email addresses."""
    return _ok(_get_client().get("/user/emails"))

@_op(gitea_write)
def add_user_email(emails: list[str]):
    """Add email addresses for the current user."""
    return _ok(_get_client().post("/user/emails", json={"emails": emails}))

@_op(gitea_delete)
def delete_user_email(emails: list[str]):
    """Delete email addresses for the current user."""
    return _ok(
        _get_client()._json("DELETE", "/user/emails", json={"emails": emails})
    )

@_op(gitea_read)
def list_user_teams():
    """List teams the current user belongs to."""
    return _ok(_get_client().paginate("/user/teams"))

@_op(gitea_read)
def list_oauth2_apps():
    """List the current user's OAuth2 applications."""
    return _ok(_get_client().paginate("/user/applications/oauth2"))

@_op(gitea_write)
def create_oauth2_app(
    name: str,
    redirect_uris: Annotated[list[str], Field(description="Allowed OAuth2 redirect URIs (absolute URLs).")],
    confidential_client: Annotated[bool | None, Field(description="True = confidential client (server-side, uses client_secret); False = public (SPA/native).")] = None,
):
    """Create an OAuth2 application for the current user."""
    return _call("POST", "/user/applications/oauth2", locals())

@_op(gitea_read)
def get_oauth2_app(app_id: int):
    """Get an OAuth2 application by ID."""
    return _ok(_get_client().get(f"/user/applications/oauth2/{app_id}"))

@_op(gitea_write)
def edit_oauth2_app(
    app_id: int,
    name: str | None = None,
    redirect_uris: Annotated[list[str] | None, Field(description="Replacement set of allowed redirect URIs.")] = None,
    confidential_client: Annotated[bool | None, Field(description="True = confidential (uses client_secret); False = public.")] = None,
):
    """Edit an OAuth2 application."""
    return _call("PATCH", "/user/applications/oauth2/{app_id}", locals())

@_op(gitea_delete)
def delete_oauth2_app(app_id: int):
    """Delete an OAuth2 application."""
    return _ok(_get_client().delete(f"/user/applications/oauth2/{app_id}"))

@_op(gitea_read)
def list_blocked_users():
    """List users blocked by the current user."""
    return _ok(_get_client().paginate("/user/blocks"))

@_op(gitea_write)
def block_user(username: str):
    """Block a user."""
    return _ok(_get_client().put(f"/user/blocks/{username}"))

@_op(gitea_delete)
def unblock_user(username: str):
    """Unblock a user."""
    return _ok(_get_client().delete(f"/user/blocks/{username}"))

@_op(gitea_write)
def update_user_settings(
    description: str | None = None,
    full_name: str | None = None,
    location: str | None = None,
    website: str | None = None,
    language: Annotated[str | None, Field(description="UI language code, e.g. 'en-US', 'ru-RU'.")] = None,
    hide_email: bool | None = None,
    hide_activity: bool | None = None,
    theme: Annotated[str | None, Field(description="UI theme name as configured in Gitea (e.g. 'gitea-light', 'gitea-dark', 'arc-green').")] = None,
    diff_view_style: Annotated[Literal["split", "unified"] | None, Field(description="Default diff view style.")] = None,
):
    """Update the current user's settings."""
    return _call("PATCH", "/user/settings", locals())

# ── Access Tokens ────────────────────────────────────────────────────────────


def _basic_auth_request(method: str, path: str, username: str, password: str, json=None):
    """Call Gitea API with HTTP Basic auth.

    /users/{username}/tokens hard-requires basic auth (reqBasicOrRevProxyAuth
    in routers/api/v1/api.go) — the token-auth client cannot reach it.
    """
    base = _get_client()._base
    r = httpx.request(
        method,
        f"{base}/api/v1{path}",
        auth=(username, password),
        json=json,
        timeout=30.0,
    )
    if r.status_code >= 400:
        try:
            body = r.json()
        except ValueError:
            # no-report: parse fallback for a non-JSON error body; the HTTP error raises below
            body = r.text
        raise GiteaError(r.status_code, method, path, body)
    return r.json() if r.content else None


@_op(gitea_write)
def create_user_access_token(
    name: Annotated[str, Field(description="Human-readable token name (shown in user's token list).")],
    scopes: Annotated[list[str], Field(description=(
        "OAuth-style scope strings. Format: '<verb>:<resource>' or 'all'. "
        "Verbs: read, write. Resources: activitypub, admin, issue, misc, "
        "notification, organization, package, repository, user. "
        "Examples: ['write:repository'], ['read:user', 'read:package'], ['all']."
    ))],
    username: Annotated[str | None, Field(description="Target username. Defaults to the authenticated user (derived from /user).")] = None,
    password: Annotated[str | None, Field(description="Basic-auth password OR an existing PAT with 'write:user' / 'all' scope. Defaults to GITEA_TOKEN.")] = None,
):
    """Create a personal access token for a user.

    Requires HTTP Basic auth. Defaults to self-token-rotation using the
    configured `GITEA_TOKEN` as the Basic password (Gitea's Basic.Verify
    accepts a PAT in the password field if it has `write:user` or `all`
    scope); username is auto-derived from /user. Pass `username` +
    `password` to create a token for a different user.

    The response's `sha1` field is the raw token — Gitea will not show it again.
    """
    s = get_settings()
    pwd = password or s.gitea_token
    user = username
    if not user:
        me = _get_client().get("/user") or {}
        user = me.get("login")
    if not user or not pwd:
        raise ValueError(
            "username/password unresolved — pass them as args, or ensure "
            "GITEA_TOKEN is set and has write:user (or all) scope"
        )
    return _ok(
        _basic_auth_request(
            "POST",
            f"/users/{user}/tokens",
            user,
            pwd,
            json={"name": name, "scopes": scopes},
        )
    )

# ── SSH / GPG Keys ──────────────────────────────────────────────────────────


@_op(gitea_read)
def list_ssh_keys():
    """List the current user's SSH keys."""
    return _ok(_get_client().paginate("/user/keys"))

@_op(gitea_write)
def create_ssh_key(
    title: Annotated[str, Field(description="Human-readable key label.")],
    key: Annotated[str, Field(description="OpenSSH public-key text — full line, e.g. 'ssh-ed25519 AAAA... user@host'.")],
):
    """Add a new SSH key for the current user."""
    return _ok(_get_client().post("/user/keys", json={"title": title, "key": key}))

@_op(gitea_delete)
def delete_ssh_key(key_id: int):
    """Delete an SSH key by ID."""
    return _ok(_get_client().delete(f"/user/keys/{key_id}"))

@_op(gitea_read)
def list_gpg_keys():
    """List the current user's GPG keys."""
    return _ok(_get_client().paginate("/user/gpg_keys"))

@_op(gitea_write)
def create_gpg_key(
    armored_public_key: Annotated[str, Field(description="ASCII-armored OpenPGP public key block (begins with '-----BEGIN PGP PUBLIC KEY BLOCK-----').")],
):
    """Add a new GPG key for the current user."""
    return _ok(
        _get_client().post(
            "/user/gpg_keys", json={"armored_public_key": armored_public_key}
        )
    )

@_op(gitea_delete)
def delete_gpg_key(key_id: int):
    """Delete a GPG key by ID."""
    return _ok(_get_client().delete(f"/user/gpg_keys/{key_id}"))

# ── Repositories ─────────────────────────────────────────────────────────────


@_op(gitea_read)
def search_repos(
    query: str,
    topic: Annotated[bool | None, Field(description="True = match `query` against repository topic names instead of repo name/description.")] = None,
    sort: Annotated[Literal["alpha", "created", "updated", "size", "id"] | None, Field(description="Sort field for results.")] = None,
    order: Annotated[Literal["asc", "desc"] | None, Field(description="Sort direction.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea repo objects.")] = True,
):
    """Search for repositories by keyword.

    brief (default True): compact view — full_name, description, language,
    stars, issues count, default_branch, updated_at.
    Set brief=False for full Gitea API response objects."""
    params = _body(locals(), exclude=("brief",), rename={"query": "q"})
    data = _get_client().get("/repos/search", params=params)
    if isinstance(data, dict) and "ok" in data and "data" in data:
        data = data["data"]
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_write)
def create_repo(
    name: str,
    description: str | None = None,
    private: bool | None = None,
    auto_init: Annotated[bool | None, Field(description="True = initialize repo with README/.gitignore/license per the template fields below.")] = None,
    gitignores: Annotated[str | None, Field(description="Comma-separated Gitea .gitignore template names (e.g. 'Go,Python'). Names — not file contents.")] = None,
    license: Annotated[str | None, Field(description="Gitea license template name (e.g. 'MIT', 'Apache-2.0'). Name — not the license text.")] = None,
    readme: Annotated[str | None, Field(description="Gitea README template name (e.g. 'Default'). Name — not file contents.")] = None,
    default_branch: str | None = None,
):
    """Create a new repository for the authenticated user."""
    private = _enforce_private(private)
    return _call("POST", "/user/repos", locals())

@_op(gitea_read)
def get_repo(owner: str, repo: str):
    """Get a repository by owner and name."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}"))

@_op(gitea_write)
def edit_repo(
    owner: str,
    repo: str,
    name: str | None = None,
    description: str | None = None,
    website: str | None = None,
    private: bool | None = None,
    has_issues: bool | None = None,
    has_wiki: bool | None = None,
    has_pull_requests: bool | None = None,
    has_projects: bool | None = None,
    has_releases: bool | None = None,
    has_packages: bool | None = None,
    has_actions: bool | None = None,
    default_branch: str | None = None,
    archived: bool | None = None,
    template: Annotated[bool | None, Field(description="True = mark the repo as a template (usable by CreateRepoFromTemplate); False = plain repo.")] = None,
):
    """Edit a repository's properties."""
    private = _enforce_private(private)
    return _call("PATCH", "/repos/{owner}/{repo}", locals())

@_op(gitea_delete)
def delete_repo(owner: str, repo: str):
    """Delete a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}"))

@_op(gitea_write)
def fork_repo(
    owner: str,
    repo: str,
    organization: str | None = None,
    name: str | None = None,
):
    """Fork a repository."""
    return _call("POST", "/repos/{owner}/{repo}/forks", locals())

@_op(gitea_read)
def list_forks(
    owner: str,
    repo: str,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea repo objects.")] = True,
):
    """List forks of a repository.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate(f"/repos/{owner}/{repo}/forks")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_read)
def list_repo_topics(owner: str, repo: str):
    """List a repository's topics."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/topics"))

@_op(gitea_write)
def set_repo_topics(
    owner: str,
    repo: str,
    topics: Annotated[list[str], Field(description="Full replacement list of topic names — existing topics are removed if not present here.")],
):
    """Set a repository's topics, replacing all existing ones."""
    return _ok(
        _get_client().put(f"/repos/{owner}/{repo}/topics", json={"topics": topics})
    )

@_op(gitea_read)
def list_repo_collaborators(owner: str, repo: str):
    """List a repository's collaborators."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/collaborators"))

@_op(gitea_write)
def add_repo_collaborator(
    owner: str,
    repo: str,
    collaborator: str,
    permission: Annotated[Literal["read", "write", "admin"], Field(description="Permission level granted to the collaborator on this repo.")],
):
    """Add a collaborator to a repository."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/collaborators/{collaborator}",
            json={"permission": permission},
        )
    )

@_op(gitea_delete)
def remove_repo_collaborator(owner: str, repo: str, collaborator: str):
    """Remove a collaborator from a repository."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/collaborators/{collaborator}")
    )

@_op(gitea_write)
def star_repo(owner: str, repo: str):
    """Star a repository."""
    return _ok(_get_client().put(f"/user/starred/{owner}/{repo}"))

@_op(gitea_delete)
def unstar_repo(owner: str, repo: str):
    """Unstar a repository."""
    return _ok(_get_client().delete(f"/user/starred/{owner}/{repo}"))

@_op(gitea_read)
def list_my_starred_repos(
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea repo objects.")] = True,
):
    """List repositories starred by the current user.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate("/user/starred")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_write)
def add_repo_topic(owner: str, repo: str, topic: str):
    """Add a topic to a repository."""
    return _ok(_get_client().put(f"/repos/{owner}/{repo}/topics/{topic}"))

@_op(gitea_delete)
def delete_repo_topic(owner: str, repo: str, topic: str):
    """Delete a topic from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/topics/{topic}"))

@_op(gitea_read)
def list_repo_watchers(owner: str, repo: str):
    """List users watching a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/subscribers"))

@_op(gitea_read)
def list_my_subscriptions(
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea repo objects.")] = True,
):
    """List repositories watched by the current user.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate("/user/subscriptions")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_write)
def watch_repo(owner: str, repo: str):
    """Watch a repository."""
    return _ok(_get_client().put(f"/repos/{owner}/{repo}/subscription"))

@_op(gitea_delete)
def unwatch_repo(owner: str, repo: str):
    """Unwatch a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/subscription"))

@_op(gitea_read)
def list_repo_teams(owner: str, repo: str):
    """List teams that have access to a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/teams"))

@_op(gitea_read)
def check_repo_collaborator(owner: str, repo: str, collaborator: str):
    """Check if a user is a collaborator of a repository."""
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/collaborators/{collaborator}"
        )
    )

@_op(gitea_read)
def get_repo_collaborator_permission(
    owner: str, repo: str, collaborator: str
):
    """Get a collaborator's permission level for a repository."""
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/collaborators/{collaborator}/permission"
        )
    )

def _hook_body(hook_type: str, config: dict, events: list, active: bool) -> dict:
    return {"type": hook_type, "config": config, "events": events, "active": active}


# ── Webhooks ─────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_webhooks(owner: str, repo: str):
    """List a repository's webhooks."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/hooks"))

@_op(gitea_write)
def create_repo_webhook(
    owner: str,
    repo: str,
    config: _HookConfig,
    events: _HookEvents,
    hook_type: _HookType = "gitea",
    active: bool = True,
):
    """Create a webhook for a repository."""
    return _ok(_get_client().post(
        f"/repos/{owner}/{repo}/hooks", json=_hook_body(hook_type, config, events, active),
    ))

@_op(gitea_write)
def edit_repo_webhook(
    owner: str,
    repo: str,
    hook_id: int,
    config: _HookConfigPatch = None,
    events: _HookEventsPatch = None,
    active: bool | None = None,
):
    """Edit a repository webhook."""
    return _call("PATCH", "/repos/{owner}/{repo}/hooks/{hook_id}", locals())

@_op(gitea_delete)
def delete_repo_webhook(owner: str, repo: str, hook_id: int):
    """Delete a repository webhook."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/hooks/{hook_id}"))

@_op(gitea_write)
def test_repo_webhook(owner: str, repo: str, hook_id: int):
    """Test a repository webhook."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/hooks/{hook_id}/tests"))

# ── Org Webhooks ─────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_webhooks(org: str):
    """List webhooks for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/hooks"))

@_op(gitea_write)
def create_org_webhook(
    org: str,
    config: _HookConfig,
    events: _HookEvents,
    hook_type: _HookType = "gitea",
    active: bool = True,
):
    """Create a webhook for an organization."""
    return _ok(_get_client().post(
        f"/orgs/{org}/hooks", json=_hook_body(hook_type, config, events, active),
    ))

@_op(gitea_write)
def edit_org_webhook(
    org: str,
    hook_id: int,
    config: _HookConfigPatch = None,
    events: _HookEventsPatch = None,
    active: bool | None = None,
):
    """Edit an organization webhook."""
    return _call("PATCH", "/orgs/{org}/hooks/{hook_id}", locals())

@_op(gitea_delete)
def delete_org_webhook(org: str, hook_id: int):
    """Delete an organization webhook."""
    return _ok(_get_client().delete(f"/orgs/{org}/hooks/{hook_id}"))

# ── Deploy Keys ──────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_deploy_keys(owner: str, repo: str):
    """List a repository's deploy keys."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/keys"))

@_op(gitea_write)
def create_deploy_key(
    owner: str,
    repo: str,
    title: str,
    key: Annotated[str, Field(description="OpenSSH public-key text — full line, e.g. 'ssh-ed25519 AAAA... user@host'.")],
    read_only: Annotated[bool, Field(description="True (default) = read-only clone access; False = read+write (push allowed).")] = True,
):
    """Add a deploy key to a repository."""
    body: dict = {"title": title, "key": key, "read_only": read_only}
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/keys", json=body))

@_op(gitea_read)
def get_deploy_key(owner: str, repo: str, key_id: int):
    """Get a deploy key by ID."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/keys/{key_id}"))

@_op(gitea_delete)
def delete_deploy_key(owner: str, repo: str, key_id: int):
    """Delete a deploy key from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/keys/{key_id}"))

# ── Files and Content ────────────────────────────────────────────────────────


@_op(gitea_read)
def get_file_content(
    owner: str,
    repo: str,
    filepath: str,
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Get the metadata and content of a file in a repository."""
    return _call("GET", "/repos/{owner}/{repo}/contents/{filepath}", locals())

@_op(gitea_write)
def create_file(
    owner: str,
    repo: str,
    filepath: str,
    content: Annotated[str, Field(description="File body as PLAINTEXT. The tool base64-encodes it for the API — do NOT pre-encode.")],
    message: Annotated[str, Field(description="Git commit message for this change.")],
    branch: Annotated[str | None, Field(description="Branch to commit on (HEAD advances to the new commit). Defaults to the repo's default branch.")] = None,
    new_branch: Annotated[str | None, Field(description="If set, create this new branch from `branch` and commit there (PR-style flow). The base `branch` is left untouched.")] = None,
    author_name: Annotated[str | None, Field(description="Override the git author name for this commit.")] = None,
    author_email: Annotated[str | None, Field(description="Override the git author email for this commit.")] = None,
):
    """Create a new file in a repository. Content is provided as plain text and will be base64-encoded automatically."""
    encoded = base64.b64encode(content.encode()).decode()
    body: dict = {"content": encoded, "message": message}
    if branch is not None:
        body["branch"] = branch
    if new_branch is not None:
        body["new_branch"] = new_branch
    if author_name is not None or author_email is not None:
        author: dict = {}
        if author_name is not None:
            author["name"] = author_name
        if author_email is not None:
            author["email"] = author_email
        body["author"] = author
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/contents/{filepath}", json=body)
    )

@_op(gitea_write)
def update_file(
    owner: str,
    repo: str,
    filepath: str,
    content: Annotated[str, Field(description="New file body as PLAINTEXT. The tool base64-encodes it for the API — do NOT pre-encode.")],
    message: Annotated[str, Field(description="Git commit message for this change.")],
    sha: Annotated[str, Field(description="Blob SHA of the existing file (optimistic concurrency). Fetch via get_file_content — the response's top-level `sha`.")],
    branch: Annotated[str | None, Field(description="Branch to commit on (HEAD advances to the new commit). Defaults to the repo's default branch.")] = None,
    new_branch: Annotated[str | None, Field(description="If set, create this new branch from `branch` and commit there (PR-style flow). The base `branch` is left untouched.")] = None,
):
    """Update an existing file in a repository. Content is provided as plain text and will be base64-encoded automatically. The sha of the existing file is required."""
    encoded = base64.b64encode(content.encode()).decode()
    body: dict = {"content": encoded, "message": message, "sha": sha}
    if branch is not None:
        body["branch"] = branch
    if new_branch is not None:
        body["new_branch"] = new_branch
    return _ok(
        _get_client().put(f"/repos/{owner}/{repo}/contents/{filepath}", json=body)
    )

@_op(gitea_delete)
def delete_file(
    owner: str,
    repo: str,
    filepath: str,
    message: Annotated[str, Field(description="Git commit message for the deletion.")],
    sha: Annotated[str, Field(description="Blob SHA of the file to delete (optimistic concurrency). Fetch via get_file_content.")],
    branch: Annotated[str | None, Field(description="Branch to commit the deletion on. Defaults to the repo's default branch.")] = None,
):
    """Delete a file in a repository. The sha of the file to delete is required."""
    body: dict = {"message": message, "sha": sha}
    if branch is not None:
        body["branch"] = branch
    return _ok(
        _get_client()._json(
            "DELETE", f"/repos/{owner}/{repo}/contents/{filepath}", json=body
        )
    )

@_op(gitea_read)
def get_directory_content(
    owner: str,
    repo: str,
    dirpath: Annotated[str, Field(description="Path inside the repo. Empty string = repo root.")] = "",
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Get the contents of a directory in a repository."""
    return _call("GET", "/repos/{owner}/{repo}/contents/{dirpath}", locals())

@_op(gitea_read)
def get_raw_file(
    owner: str,
    repo: str,
    filepath: str,
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Get the raw content of a file in a repository."""
    params = _body(locals(), exclude=("owner", "repo", "filepath"))
    return _get_client().get_text(
        f"/repos/{owner}/{repo}/raw/{filepath}", params=params or None
    )

# ── Branches ─────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_branches(owner: str, repo: str):
    """List a repository's branches."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/branches"))

@_op(gitea_read)
def get_branch(owner: str, repo: str, branch: str):
    """Get a specific branch of a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/branches/{branch}"))

@_op(gitea_write)
def create_branch(
    owner: str,
    repo: str,
    new_branch_name: Annotated[str, Field(description="Name of the new branch to create.")],
    old_branch_name: Annotated[str | None, Field(description="Source branch to fork from. Mutually exclusive with `old_ref_name`. If both are omitted, the repo's default branch is used.")] = None,
    old_ref_name: Annotated[str | None, Field(description="Source ref (tag name or commit SHA) to fork from. Mutually exclusive with `old_branch_name`.")] = None,
):
    """Create a new branch in a repository."""
    return _call("POST", "/repos/{owner}/{repo}/branches", locals())

@_op(gitea_delete)
def delete_branch(owner: str, repo: str, branch: str):
    """Delete a branch from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/branches/{branch}"))

@_op(gitea_read)
def list_branch_protections(owner: str, repo: str):
    """List branch protections for a repository."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/branch_protections"))

@_op(gitea_write)
def create_branch_protection(
    owner: str,
    repo: str,
    branch_name: Annotated[str, Field(description="Branch name OR glob pattern (e.g. 'main', 'release/*') the rule applies to.")],
    enable_push: _BpEnablePush = None,
    enable_push_whitelist: _BpEnablePushWhitelist = None,
    push_whitelist_usernames: Annotated[list[str] | None, Field(description="USERNAMES allowed to push when push whitelist is enabled (NOT team names — use the team-id variant on the Gitea API if needed).")] = None,
    enable_merge_whitelist: _BpEnableMergeWhitelist = None,
    merge_whitelist_usernames: _BpMergeWhitelistUsernames = None,
    required_approvals: _BpRequiredApprovals = None,
    enable_status_check: _BpEnableStatusCheck = None,
    status_check_contexts: _BpStatusCheckContexts = None,
):
    """Create a branch protection rule for a repository."""
    return _call("POST", "/repos/{owner}/{repo}/branch_protections", locals())

@_op(gitea_read)
def get_branch_protection(owner: str, repo: str, name: str):
    """Get a branch protection rule by name."""
    return _ok(
        _get_client().get(f"/repos/{owner}/{repo}/branch_protections/{name}")
    )

@_op(gitea_write)
def edit_branch_protection(
    owner: str,
    repo: str,
    name: str,
    enable_push: _BpEnablePush = None,
    enable_push_whitelist: _BpEnablePushWhitelist = None,
    push_whitelist_usernames: Annotated[list[str] | None, Field(description="USERNAMES allowed to push when push whitelist is enabled (NOT team names).")] = None,
    enable_merge_whitelist: _BpEnableMergeWhitelist = None,
    merge_whitelist_usernames: _BpMergeWhitelistUsernames = None,
    required_approvals: _BpRequiredApprovals = None,
    enable_status_check: _BpEnableStatusCheck = None,
    status_check_contexts: _BpStatusCheckContexts = None,
):
    """Edit a branch protection rule."""
    return _call("PATCH", "/repos/{owner}/{repo}/branch_protections/{name}", locals())

@_op(gitea_delete)
def delete_branch_protection(owner: str, repo: str, name: str):
    """Delete a branch protection rule by name."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/branch_protections/{name}")
    )

# ── Tag Protections ──────────────────────────────────────────────────────


@_op(gitea_read)
def list_tag_protections(owner: str, repo: str):
    """List tag protections for a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/tag_protections"))

@_op(gitea_write)
def create_tag_protection(
    owner: str,
    repo: str,
    name_pattern: Annotated[str, Field(description="Glob pattern for tag names this rule applies to (e.g. 'v*', 'release-*').")],
    whitelist_usernames: Annotated[list[str] | None, Field(description="USERNAMES allowed to create/push tags matching the pattern.")] = None,
    whitelist_teams: Annotated[list[str] | None, Field(description="Team names (within the owning organization) whose members may create/push matching tags.")] = None,
):
    """Create a tag protection rule for a repository."""
    return _call("POST", "/repos/{owner}/{repo}/tag_protections", locals())

@_op(gitea_read)
def get_tag_protection(owner: str, repo: str, tag_protection_id: int):
    """Get a tag protection rule by ID."""
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"
        )
    )

@_op(gitea_write)
def edit_tag_protection(
    owner: str,
    repo: str,
    tag_protection_id: int,
    name_pattern: Annotated[str | None, Field(description="Replacement glob pattern for tag names (e.g. 'v*').")] = None,
    whitelist_usernames: Annotated[list[str] | None, Field(description="Replacement list of USERNAMES allowed to create/push matching tags.")] = None,
    whitelist_teams: Annotated[list[str] | None, Field(description="Replacement list of team names whose members may create/push matching tags.")] = None,
):
    """Edit a tag protection rule."""
    return _call("PATCH", "/repos/{owner}/{repo}/tag_protections/{tag_protection_id}", locals())

@_op(gitea_delete)
def delete_tag_protection(owner: str, repo: str, tag_protection_id: int):
    """Delete a tag protection rule."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"
        )
    )

# ── Commits and Statuses ────────────────────────────────────────────────────


@_op(gitea_read)
def list_commits(
    owner: str,
    repo: str,
    sha: Annotated[str | None, Field(description="Start ref (branch / tag / commit SHA) to walk from. Defaults to the repo's default branch.")] = None,
    path: Annotated[str | None, Field(description="Only return commits that touched this file or directory path.")] = None,
    stat: Annotated[bool | None, Field(description="Per-commit additions/deletions stats. Gitea includes them by default; pass False to skip computing them for a faster response.")] = None,
    limit: Annotated[int | None, Field(description="Page size (commits per page).")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
    brief: Annotated[bool, Field(description="True (default) = compact view (short sha, first line of message, author, date); False = full commit objects.")] = True,
):
    """List commits in a repository.

    brief (default True): compact view — short sha, first line of message,
    author name, date. Set brief=False for full objects."""
    params: dict = {"limit": limit}
    if sha is not None:
        params["sha"] = sha
    if path is not None:
        params["path"] = path
    if stat is not None:
        params["stat"] = stat
    if page is not None:
        params["page"] = page
    data = _get_client().get(f"/repos/{owner}/{repo}/commits", params=params)
    if brief:
        data = _slim_commits(data)
    return _ok(data)

@_op(gitea_read)
def get_commit(owner: str, repo: str, sha: str):
    """Get a single commit by SHA."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/git/commits/{sha}"))

@_op(gitea_read)
def get_commit_diff(owner: str, repo: str, sha: str):
    """Get the diff of a commit."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/git/commits/{sha}.diff")

@_op(gitea_read)
def compare_commits(owner: str, repo: str, base: str, head: str):
    """Compare two commits or branches."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/compare/{base}...{head}"))

@_op(gitea_read)
def list_commit_statuses(owner: str, repo: str, sha: str):
    """List statuses for a commit."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/statuses/{sha}"))

@_op(gitea_write)
def create_commit_status(
    owner: str,
    repo: str,
    sha: str,
    state: Annotated[Literal["pending", "success", "error", "failure", "warning"], Field(description="Status state. Drives CI badge and (with branch protection) merge gating.")],
    target_url: Annotated[str | None, Field(description="URL the status badge links to — usually a CI run / log / dashboard for this check.")] = None,
    description: Annotated[str | None, Field(description="Short human-readable summary shown next to the badge.")] = None,
    context: Annotated[str | None, Field(description="Identifier for this check (e.g. 'ci/build', 'lint'). Statuses are grouped by context; posting a new state with the same context overwrites the previous one.")] = None,
):
    """Create a commit status. State must be one of: pending, success, error, failure, warning."""
    return _call("POST", "/repos/{owner}/{repo}/statuses/{sha}", locals())

@_op(gitea_read)
def get_combined_commit_status(owner: str, repo: str, ref: str):
    """Get the combined status for a commit ref."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/commits/{ref}/status"))

# ── Tags and Releases ───────────────────────────────────────────────────────


@_op(gitea_read)
def list_tags(owner: str, repo: str):
    """List a repository's tags."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/tags"))

@_op(gitea_write)
def create_tag(
    owner: str,
    repo: str,
    tag_name: str,
    target: Annotated[str | None, Field(description="Branch name, commit SHA, or existing tag to anchor the new tag to. Defaults to the repo's default branch HEAD.")] = None,
    message: Annotated[str | None, Field(description="Annotated-tag message. Omit / empty → creates a lightweight tag instead of annotated.")] = None,
):
    """Create a new tag in a repository."""
    return _call("POST", "/repos/{owner}/{repo}/tags", locals())

@_op(gitea_delete)
def delete_tag(owner: str, repo: str, tag: str):
    """Delete a tag from a repository."""
    return _call("DELETE", "/repos/{owner}/{repo}/tags/{tag}", locals())

@_op(gitea_read)
def list_releases(
    owner: str,
    repo: str,
    brief: Annotated[bool, Field(description="True (default) = compact view (id, tag_name, name, draft, prerelease, published_at); False = full release objects.")] = True,
):
    """List a repository's releases.

    brief (default True): compact view — id, tag, name, draft/prerelease,
    published date. Set brief=False for full objects."""
    data = _get_client().paginate(f"/repos/{owner}/{repo}/releases")
    if brief:
        data = [
            {
                "id": r.get("id"),
                "tag_name": r.get("tag_name"),
                "name": r.get("name"),
                "draft": r.get("draft"),
                "prerelease": r.get("prerelease"),
                "published_at": r.get("published_at"),
            }
            for r in data
        ] if isinstance(data, list) else data
    return _ok(data)

@_op(gitea_read)
def get_release(owner: str, repo: str, release_id: int):
    """Get a release by ID."""
    return _call("GET", "/repos/{owner}/{repo}/releases/{release_id}", locals())

@_op(gitea_write)
def create_release(
    owner: str,
    repo: str,
    tag_name: Annotated[str, Field(description="Git tag name. If the tag does not exist, Gitea creates it on `target_commitish`.")],
    target_commitish: Annotated[str | None, Field(description="Branch name OR commit SHA the tag should point at when it has to be created. Ignored if the tag already exists. Defaults to the repo's default branch.")] = None,
    name: Annotated[str | None, Field(description="Release title shown in the UI (distinct from `tag_name`).")] = None,
    body: Annotated[str | None, Field(description="Release notes / description (markdown).")] = None,
    draft: Annotated[bool | None, Field(description="If True, save as an unpublished draft visible only to maintainers.")] = None,
    prerelease: Annotated[bool | None, Field(description="If True, mark as a pre-release (alpha/beta/rc) — clients filtering for stable releases will skip it.")] = None,
):
    """Create a new release in a repository."""
    return _call("POST", "/repos/{owner}/{repo}/releases", locals())

@_op(gitea_write)
def edit_release(
    owner: str,
    repo: str,
    release_id: int,
    tag_name: Annotated[str | None, Field(description="Replace the release's git tag name.")] = None,
    target_commitish: Annotated[str | None, Field(description="Branch name OR commit SHA — only honored when (re)creating the tag.")] = None,
    name: Annotated[str | None, Field(description="Replace the release title shown in the UI.")] = None,
    body: Annotated[str | None, Field(description="Replace the release notes / description (markdown).")] = None,
    draft: Annotated[bool | None, Field(description="True = unpublished draft, False = published.")] = None,
    prerelease: Annotated[bool | None, Field(description="True = mark as pre-release (alpha/beta/rc), False = stable.")] = None,
):
    """Edit a release."""
    return _call("PATCH", "/repos/{owner}/{repo}/releases/{release_id}", locals())

@_op(gitea_delete)
def delete_release(owner: str, repo: str, release_id: int):
    """Delete a release by ID."""
    return _call("DELETE", "/repos/{owner}/{repo}/releases/{release_id}", locals())

# ── Labels ───────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_labels(owner: str, repo: str):
    """List a repository's labels."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/labels"))

@_op(gitea_write)
def create_repo_label(
    owner: str,
    repo: str,
    name: str,
    color: Annotated[str, Field(description="Hex color string. Accepted forms: '#rrggbb', '#rgb', or the same without the leading '#' (e.g. '#00ff00', '00ff00', '#0f0').")],
    description: str | None = None,
):
    """Create a label in a repository."""
    return _call("POST", "/repos/{owner}/{repo}/labels", locals())

@_op(gitea_write)
def edit_repo_label(
    owner: str,
    repo: str,
    label_id: int,
    name: str | None = None,
    color: Annotated[str | None, Field(description="Replacement hex color string. Accepted forms: '#rrggbb', '#rgb', or without the leading '#' (e.g. '#00ff00', '00ff00').")] = None,
    description: str | None = None,
):
    """Edit a repository label."""
    return _call("PATCH", "/repos/{owner}/{repo}/labels/{label_id}", locals())

@_op(gitea_delete)
def delete_repo_label(owner: str, repo: str, label_id: int):
    """Delete a repository label."""
    return _call("DELETE", "/repos/{owner}/{repo}/labels/{label_id}", locals())

# ── Milestones ───────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_milestones(
    owner: str,
    repo: str,
    state: Annotated[Literal["open", "closed", "all"] | None, Field(description="Filter by milestone state. Defaults to server default ('open').")] = None,
):
    """List a repository's milestones. State can be open, closed, or all."""
    params = _body(locals(), exclude=("owner", "repo"))
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/milestones", params=params or None)
    )

@_op(gitea_read)
def get_milestone(owner: str, repo: str, milestone_id: int):
    """Get a milestone by ID."""
    return _call("GET", "/repos/{owner}/{repo}/milestones/{milestone_id}", locals())

@_op(gitea_write)
def create_milestone(
    owner: str,
    repo: str,
    title: str,
    description: str | None = None,
    due_on: Annotated[str | None, Field(description="Due date as ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")] = None,
    state: Annotated[Literal["open", "closed"] | None, Field(description="Initial milestone state. Defaults to 'open'.")] = None,
):
    """Create a milestone in a repository."""
    return _call("POST", "/repos/{owner}/{repo}/milestones", locals())

@_op(gitea_write)
def edit_milestone(
    owner: str,
    repo: str,
    milestone_id: int,
    title: str | None = None,
    description: str | None = None,
    due_on: Annotated[str | None, Field(description="Replacement due date as ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")] = None,
    state: Annotated[Literal["open", "closed"] | None, Field(description="New milestone state.")] = None,
):
    """Edit a milestone."""
    return _call("PATCH", "/repos/{owner}/{repo}/milestones/{milestone_id}", locals())

@_op(gitea_delete)
def delete_milestone(owner: str, repo: str, milestone_id: int):
    """Delete a milestone."""
    return _call("DELETE", "/repos/{owner}/{repo}/milestones/{milestone_id}", locals())

# ── Issues ───────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issues(
    owner: str,
    repo: str,
    state: Annotated[Literal["open", "closed", "all"] | None, Field(description="Filter by issue state. Defaults to server default ('open').")] = None,
    labels: Annotated[str | None, Field(description=(
        "Filter by label NAMES (Gitea API quirk: read filter takes names, "
        "write ops take label IDs). Comma-separated, e.g. 'bug,frontend'. "
        "For write ops (create_issue, add_issue_labels, "
        "replace_issue_labels) use integer IDs from list_repo_labels."
    ))] = None,
    milestone: Annotated[str | None, Field(description="Filter by milestone name OR comma-separated names. Use list_milestones to enumerate.")] = None,
    assignee: Annotated[str | None, Field(description="Filter by assignee USERNAME (not user ID).")] = None,
    type: Annotated[Literal["issues", "pulls"] | None, Field(description="'issues' = exclude PRs, 'pulls' = only PRs. Omit to include both.")] = None,
    page: int | None = None,
    limit: int | None = 20,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea issue objects.")] = True,
):
    """List issues in a repository. Type can be 'issues' or 'pulls'.

    brief (default True): compact view — number, title, state, labels, assignees,
    updated_at, and body summary extracted from a <brief>...</brief> tag.
    If brief is null for an issue, use get_issue for full details or edit_issue
    to add <brief>short summary</brief> to its body for convenient list views.
    Set brief=False for full Gitea API response objects."""
    params: dict = {"limit": limit}
    if state is not None:
        params["state"] = state
    if labels is not None:
        params["labels"] = labels
    if milestone is not None:
        params["milestones"] = milestone
    if assignee is not None:
        params["assigned_by"] = assignee
    if type is not None:
        params["type"] = type
    if page is not None:
        params["page"] = page
    data = _get_client().get(f"/repos/{owner}/{repo}/issues", params=params)
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_read)
def search_issues(
    query: Annotated[str, Field(description="Keyword to match against issue title/body.")],
    owner: Annotated[str | None, Field(description="Scope search to a specific owner (username or org).")] = None,
    state: Annotated[Literal["open", "closed", "all"] | None, Field(description="Filter by issue state. Defaults to server default.")] = None,
    labels: Annotated[str | None, Field(description=(
        "Filter by label NAMES (read filter takes names; write ops take "
        "label IDs). Comma-separated, e.g. 'bug,frontend'."
    ))] = None,
    type: Annotated[Literal["issues", "pulls"] | None, Field(description="'issues' = exclude PRs, 'pulls' = only PRs. Omit to include both.")] = None,
    limit: int | None = 20,
    page: int | None = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea issue objects.")] = True,
):
    """Search issues across repositories.

    brief (default True): slim per-issue view (number, title, state, labels,
    assignee, updated_at, plus a summary pulled from a <brief>...</brief> tag
    in the body). A null brief means the body has no such tag: get_issue shows
    the full issue, edit_issue can add the tag. brief=False returns the full
    Gitea API objects."""
    params: dict = {"q": query, "limit": limit}
    if owner is not None:
        params["owner"] = owner
    if state is not None:
        params["state"] = state
    if labels is not None:
        params["labels"] = labels
    if type is not None:
        params["type"] = type
    if page is not None:
        params["page"] = page
    data = _get_client().get("/repos/issues/search", params=params)
    # Unwrap search format before slimming
    if isinstance(data, dict) and "ok" in data and "data" in data:
        data = data["data"]
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_read)
def get_issue(owner: str, repo: str, index: int):
    """Get an issue by its index number."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}", locals())

@_op(gitea_write)
def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: Annotated[str | None, Field(description="Issue body as markdown. MUST include a <brief>short summary</brief> tag (enforced) for list views.")] = None,
    assignees: _Assignees = None,
    milestone_id: _MilestoneId = None,
    labels: _LabelIds = None,
):
    """Create an issue in a repository. Body must include <brief>summary</brief> tag."""
    _validate_brief(body)
    return _call("POST", "/repos/{owner}/{repo}/issues", locals(), rename={"milestone_id": "milestone"})

@_op(gitea_write)
def edit_issue(
    owner: str,
    repo: str,
    index: int,
    title: str | None = None,
    body: Annotated[str | None, Field(description="New issue body as markdown. If provided, MUST include a <brief>short summary</brief> tag (enforced).")] = None,
    state: Annotated[Literal["open", "closed"] | None, Field(description="Change issue state.")] = None,
    assignees: _AssigneesPatch = None,
    milestone: _MilestonePatch = None,
    due_date: Annotated[str | None, Field(description="ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")] = None,
):
    """Edit an issue. State can be 'open' or 'closed'. Body must include <brief>summary</brief> tag.

    Labels cannot be changed on this endpoint; use replace_issue_labels or
    add_issue_labels."""
    if body is not None:
        _validate_brief(body)
    return _call("PATCH", "/repos/{owner}/{repo}/issues/{index}", locals())

@_op(gitea_read)
def list_issue_comments(
    owner: str,
    repo: str,
    index: int,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea comment objects.")] = True,
):
    """List comments on an issue.

    brief (default True): compact view — id, user login, body, timestamps.
    Set brief=False for full objects."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    data = _get_client().get(f"/repos/{owner}/{repo}/issues/{index}/comments")
    if brief:
        data = _slim_comments(data)
    return _ok(data)

@_op(gitea_write)
def create_issue_comment(
    owner: str,
    repo: str,
    index: int,
    body: Annotated[str, Field(description="Comment body as markdown text.")],
):
    """Create a comment on an issue."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/comments", json={"body": body}
        )
    )

@_op(gitea_write)
def edit_issue_comment(
    owner: str,
    repo: str,
    comment_id: int,
    body: Annotated[str, Field(description="Replacement comment body as markdown text.")],
):
    """Edit a comment on an issue."""
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": body}
        )
    )

@_op(gitea_delete)
def delete_issue_comment(owner: str, repo: str, comment_id: int):
    """Delete a comment on an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}", locals())

@_op(gitea_read)
def list_issue_labels(owner: str, repo: str, index: int):
    """List labels on an issue."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/labels", locals())

@_op(gitea_write)
def add_issue_labels(
    owner: str,
    repo: str,
    index: int,
    labels: Annotated[list[int], Field(description=(
        "Label IDs (int64) from list_repo_labels — NOT names. "
        "Calling list_repo_labels first to look up IDs is required. "
        "Passing names like ['frontend'] returns 422 from Gitea."
    ))],
):
    """Add labels to an issue."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/labels", json={"labels": labels}
        )
    )

@_op(gitea_delete)
def remove_issue_label(owner: str, repo: str, index: int, label_id: int):
    """Remove a label from an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/labels/{label_id}", locals())

@_op(gitea_write)
def replace_issue_labels(
    owner: str,
    repo: str,
    index: int,
    labels: Annotated[list[int], Field(description=(
        "Replacement set of label IDs (int64) from list_repo_labels — NOT names. "
        "Calling list_repo_labels first to look up IDs is required. "
        "Passing names like ['frontend'] returns 422 from Gitea."
    ))],
):
    """Replace all labels on an issue."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/issues/{index}/labels", json={"labels": labels}
        )
    )

@_op(gitea_write)
def set_issue_deadline(
    owner: str,
    repo: str,
    index: int,
    due_date: Annotated[str, Field(description="ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")],
):
    """Set a deadline on an issue. due_date should be in ISO 8601 format."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/deadline",
            json={"due_date": due_date},
        )
    )

@_op(gitea_delete)
def delete_issue_deadline(owner: str, repo: str, index: int):
    """Remove a deadline from an issue."""
    # Gitea has no DELETE on this path; POSTing a null due_date clears it.
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/deadline",
            json={"due_date": None},
        )
    )

@_op(gitea_delete)
def clear_issue_labels(owner: str, repo: str, index: int):
    """Remove all labels from an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/labels", locals())

@_op(gitea_read)
def get_issue_timeline(owner: str, repo: str, index: int):
    """Get the timeline of an issue (comments, events, label changes, etc.)."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/issues/{index}/timeline")
    )

@_op(gitea_read)
def list_repo_issue_comments(
    owner: str,
    repo: str,
    since: Annotated[str | None, Field(description="Only return comments updated at/after this ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")] = None,
    before: Annotated[str | None, Field(description="Only return comments updated at/before this ISO-8601 timestamp, e.g. '2026-05-20T00:00:00Z'.")] = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea comment objects.")] = True,
):
    """List all comments in a repository (across all issues).

    brief (default True): compact view — id, user login, body, timestamps.
    Set brief=False for full objects."""
    params = _body(locals(), exclude=("owner", "repo", "brief"))
    data = _get_client().paginate(
        f"/repos/{owner}/{repo}/issues/comments", params=params or None
    )
    if brief:
        data = _slim_comments(data)
    return _ok(data)

@_op(gitea_delete)
def delete_stopwatch(owner: str, repo: str, index: int):
    """Delete a stopwatch on an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/stopwatch/delete", locals())

# ── Issue Extended ───────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issue_dependencies(owner: str, repo: str, index: int):
    """List an issue's dependencies."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/dependencies", locals())

@_op(gitea_write)
def add_issue_dependency(
    owner: str,
    repo: str,
    index: int,
    depends_on_id: Annotated[int, Field(description=(
        "Issue index of the dependency in the SAME repo (the per-repo "
        "issue number, NOT a global issue ID). The issue at `index` will "
        "depend on issue #depends_on_id."
    ))],
):
    """Add a dependency to an issue. depends_on_id is the index of the dependency issue."""
    # Body is Gitea's IssueMeta {index, owner, repo}; owner/repo must match the
    # URL repo or Gitea resolves the body pair as a cross-repo dependency.
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/dependencies",
            json={"index": depends_on_id, "owner": owner, "repo": repo},
        )
    )

@_op(gitea_delete)
def remove_issue_dependency(
    owner: str,
    repo: str,
    index: int,
    depends_on_id: Annotated[int, Field(description=(
        "Issue index of the dependency in the SAME repo (per-repo issue "
        "number, NOT a global issue ID). Removes the dependency edge from "
        "issue #index to issue #depends_on_id."
    ))],
):
    """Remove a dependency from an issue."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{index}/dependencies",
            json={"index": depends_on_id, "owner": owner, "repo": repo},
        )
    )

@_op(gitea_write)
def pin_issue(owner: str, repo: str, index: int):
    """Pin an issue in a repository."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/issues/{index}/pin"))

@_op(gitea_delete)
def unpin_issue(owner: str, repo: str, index: int):
    """Unpin an issue in a repository."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/pin", locals())

@_op(gitea_write)
def lock_issue(owner: str, repo: str, index: int):
    """Lock an issue's conversation."""
    return _ok(_get_client().put(f"/repos/{owner}/{repo}/issues/{index}/lock", json={}))

@_op(gitea_delete)
def unlock_issue(owner: str, repo: str, index: int):
    """Unlock an issue's conversation."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/lock", locals())

@_op(gitea_read)
def list_issue_subscriptions(owner: str, repo: str, index: int):
    """List users subscribed to an issue."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/subscriptions", locals())

@_op(gitea_write)
def subscribe_to_issue(owner: str, repo: str, index: int, user: str):
    """Subscribe a user to an issue."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/issues/{index}/subscriptions/{user}"
        )
    )

@_op(gitea_delete)
def unsubscribe_from_issue(owner: str, repo: str, index: int, user: str):
    """Unsubscribe a user from an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/subscriptions/{user}", locals())

# ── Reactions ────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issue_reactions(owner: str, repo: str, index: int):
    """List reactions on an issue."""
    # Gitea serializes an empty reaction set as JSON null - keep it a list.
    return _get_client().get(f"/repos/{owner}/{repo}/issues/{index}/reactions") or []

@_op(gitea_write)
def add_issue_reaction(
    owner: str,
    repo: str,
    index: int,
    reaction: Annotated[
        Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"],
        Field(description="Emoji reaction key (GitHub-compatible set)."),
    ],
):
    """Add a reaction to an issue. Reaction can be: +1, -1, laugh, confused, heart, hooray, rocket, eyes."""
    return _call("POST", "/repos/{owner}/{repo}/issues/{index}/reactions", locals(), rename={"reaction": "content"})

@_op(gitea_delete)
def remove_issue_reaction(
    owner: str,
    repo: str,
    index: int,
    reaction: Annotated[
        Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"],
        Field(description="Emoji reaction key to remove (must match the previously-added reaction)."),
    ],
):
    """Remove a reaction from an issue."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{index}/reactions",
            json={"content": reaction},
        )
    )

@_op(gitea_read)
def list_comment_reactions(owner: str, repo: str, comment_id: int):
    """List reactions on a comment."""
    # Gitea serializes an empty reaction set as JSON null - keep it a list.
    return _get_client().get(
        f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"
    ) or []

@_op(gitea_write)
def add_comment_reaction(
    owner: str,
    repo: str,
    comment_id: int,
    reaction: Annotated[
        Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"],
        Field(description="Emoji reaction key (GitHub-compatible set)."),
    ],
):
    """Add a reaction to a comment. Reaction can be: +1, -1, laugh, confused, heart, hooray, rocket, eyes."""
    return _call("POST", "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions", locals(), rename={"reaction": "content"})

@_op(gitea_delete)
def remove_comment_reaction(
    owner: str,
    repo: str,
    comment_id: int,
    reaction: Annotated[
        Literal["+1", "-1", "laugh", "confused", "heart", "hooray", "rocket", "eyes"],
        Field(description="Emoji reaction key to remove (must match the previously-added reaction)."),
    ],
):
    """Remove a reaction from a comment."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": reaction},
        )
    )

# ── Time Tracking ────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_tracked_times(owner: str, repo: str, index: int):
    """List tracked times on an issue."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/issues/{index}/times")
    )

@_op(gitea_write)
def add_tracked_time(
    owner: str,
    repo: str,
    index: int,
    time: Annotated[int, Field(description="Tracked duration in SECONDS (integer). E.g. 3600 = 1 hour.")],
    user_name: Annotated[str | None, Field(description="USERNAME to attribute the entry to. Defaults to the authenticated user. Admin-only when set to another user.")] = None,
    created: Annotated[str | None, Field(description="ISO-8601 timestamp for when the work happened, e.g. '2026-05-20T00:00:00Z'. Defaults to now.")] = None,
):
    """Add tracked time to an issue. Time is in seconds."""
    return _call("POST", "/repos/{owner}/{repo}/issues/{index}/times", locals())

@_op(gitea_delete)
def delete_tracked_time(owner: str, repo: str, index: int, time_id: int):
    """Delete a tracked time entry from an issue."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/times/{time_id}", locals())

@_op(gitea_write)
def start_stopwatch(owner: str, repo: str, index: int):
    """Start a stopwatch on an issue."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/issues/{index}/stopwatch/start")
    )

@_op(gitea_write)
def stop_stopwatch(owner: str, repo: str, index: int):
    """Stop a stopwatch on an issue."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/issues/{index}/stopwatch/stop")
    )

# ── Pull Requests ────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_pull_requests(
    owner: str,
    repo: str,
    state: Annotated[Literal["open", "closed", "all"] | None, Field(description="Filter by PR state. Defaults to server default ('open').")] = None,
    sort: Annotated[Literal["oldest", "recentupdate", "recentclose", "leastupdate", "mostcomment", "leastcomment", "priority"] | None, Field(description="Sort order for results. Omitted = server default (newest first). Gitea has no 'newest' value — it silently falls back to the default.")] = None,
    milestone: Annotated[int | None, Field(description="Milestone integer ID from list_milestones (NOT the milestone title).")] = None,
    labels: Annotated[list[int] | None, Field(description=(
        "Label IDs (int64) from list_repo_labels — NOT names. "
        "Calling list_repo_labels first to look up IDs is required."
    ))] = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea PR objects.")] = True,
):
    """List pull requests in a repository.

    brief (default True): compact view — number, title, state, labels, assignees,
    updated_at, and body summary extracted from a <brief>...</brief> tag.
    If brief is null for a PR, use get_pull_request for full details or
    edit the PR body to add <brief>short summary</brief> for convenient list views.
    Set brief=False for full Gitea API response objects."""
    params = _body(locals(), exclude=("owner", "repo", "brief", "labels"))
    if labels is not None:
        # collectionFormat=multi: repeated labels= params, each a bare int ID;
        # a comma-joined value is a 500 (the issues endpoint is the CSV one).
        params["labels"] = labels
    data = _get_client().paginate(f"/repos/{owner}/{repo}/pulls", params=params or None)
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_read)
def get_pull_request(owner: str, repo: str, index: int):
    """Get a pull request by index."""
    return _call("GET", "/repos/{owner}/{repo}/pulls/{index}", locals())

@_op(gitea_write)
def create_pull_request(
    owner: str,
    repo: str,
    title: Annotated[str, Field(description="PR title shown in the UI and notifications.")],
    head: Annotated[str, Field(description="Source branch — where the changes live. For cross-repo PRs use 'forkOwner:branch' (e.g. 'alice:feature-x').")],
    base: Annotated[str, Field(description="Target branch — where to merge into (typically the default branch, e.g. 'main').")],
    body: Annotated[str | None, Field(description="PR description as markdown.")] = None,
    assignees: _Assignees = None,
    milestone_id: _MilestoneId = None,
    labels: _LabelIds = None,
):
    """Create a pull request."""
    return _call("POST", "/repos/{owner}/{repo}/pulls", locals(), rename={"milestone_id": "milestone"})

@_op(gitea_write)
def edit_pull_request(
    owner: str,
    repo: str,
    index: int,
    title: Annotated[str | None, Field(description="New PR title.")] = None,
    body: Annotated[str | None, Field(description="New PR description as markdown.")] = None,
    state: Annotated[Literal["open", "closed"] | None, Field(description="Change PR state. Use 'closed' to close without merging.")] = None,
    base: Annotated[str | None, Field(description="Retarget the PR — name of the new base branch to merge into.")] = None,
    assignees: _AssigneesPatch = None,
    milestone: _MilestonePatch = None,
    labels: _LabelIds = None,
):
    """Edit a pull request."""
    return _call("PATCH", "/repos/{owner}/{repo}/pulls/{index}", locals())

@_op(gitea_execute)
def merge_pull_request(
    owner: str,
    repo: str,
    index: int,
    merge_type: Annotated[
        Literal["merge", "rebase", "rebase-merge", "squash", "fast-forward-only"],
        Field(description=(
            "How to merge. 'merge' keeps history with a merge commit; "
            "'rebase' replays commits onto base with no merge commit; "
            "'rebase-merge' rebases then adds a merge commit; "
            "'squash' collapses all commits into one on base; "
            "'fast-forward-only' refuses unless base can fast-forward to head."
        )),
    ] = "merge",
    merge_message: Annotated[str | None, Field(description="Override the body of the resulting merge/squash commit message. Sent as `merge_message_field` to the API.")] = None,
    delete_branch_after_merge: Annotated[bool | None, Field(description="If True, delete the head branch after a successful merge.")] = None,
):
    """Merge a pull request. merge_type can be: merge, rebase, rebase-merge, squash, fast-forward-only."""
    return _call("POST", "/repos/{owner}/{repo}/pulls/{index}/merge", locals(), rename={"merge_type": "do", "merge_message": "merge_message_field"})

@_op(gitea_read)
def get_pull_request_diff(owner: str, repo: str, index: int):
    """Get the diff of a pull request."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/pulls/{index}.diff")

@_op(gitea_read)
def get_pull_request_files(owner: str, repo: str, index: int):
    """List files changed in a pull request."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/pulls/{index}/files")
    )

@_op(gitea_read)
def get_pull_request_commits(owner: str, repo: str, index: int):
    """List commits in a pull request."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/pulls/{index}/commits")
    )

@_op(gitea_write)
def update_pull_request_branch(
    owner: str,
    repo: str,
    index: int,
    style: Annotated[Literal["merge", "rebase"] | None, Field(description="How to sync the PR head branch with its base. 'merge' (default) merges base into head; 'rebase' rewrites head on top of base.")] = None,
):
    """Update a pull request branch. Style can be 'merge' or 'rebase'."""
    # style is a query param on this endpoint, not a body field.
    params: dict = {}
    if style is not None:
        params["style"] = style
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/update", params=params or None
        )
    )

@_op(gitea_read)
def list_pull_reviews(owner: str, repo: str, index: int):
    """List reviews on a pull request."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/pulls/{index}/reviews")
    )

@_op(gitea_write)
def create_pull_review(
    owner: str,
    repo: str,
    index: int,
    body: Annotated[str | None, Field(description="Overall review summary as markdown text.")] = None,
    event: Annotated[Literal["APPROVED", "REQUEST_CHANGES", "COMMENT", "PENDING"] | None, Field(description="Review verdict. 'APPROVED' = approve; 'REQUEST_CHANGES' = block; 'COMMENT' = comment only; 'PENDING' = save as draft (submit later via submit_pull_review).")] = None,
    comments: Annotated[list[dict] | None, Field(description=(
        "Inline file comments. Each item is a dict with keys: "
        "`path` (file path relative to repo root), `body` (comment text), "
        "`old_position` (line number in old file or null), "
        "`new_position` (line number in new file or null). "
        "Use old_position for removed lines, new_position for added/context lines."
    ))] = None,
):
    """Create a review on a pull request. Event can be: APPROVED, REQUEST_CHANGES, COMMENT, PENDING."""
    return _call("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews", locals())

@_op(gitea_write)
def submit_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: int,
    body: Annotated[str | None, Field(description="Optional summary text to attach when submitting the review.")] = None,
    event: Annotated[Literal["APPROVED", "REQUEST_CHANGES", "COMMENT", "PENDING"] | None, Field(description="Final verdict for the pending review. 'APPROVED' = approve; 'REQUEST_CHANGES' = block; 'COMMENT' = comment only; 'PENDING' keeps it draft.")] = None,
):
    """Submit a pending pull request review."""
    return _call("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}", locals())

@_op(gitea_write)
def request_pull_reviewers(
    owner: str,
    repo: str,
    index: int,
    reviewers: Annotated[list[str], Field(description="List of reviewer USERNAMES (NOT user IDs / NOT display names / NOT team names).")],
):
    """Request reviewers for a pull request."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/requested_reviewers",
            json={"reviewers": reviewers},
        )
    )

@_op(gitea_write)
def dismiss_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: int,
    message: Annotated[str | None, Field(description="Explanation shown alongside the dismissed review (why it was dismissed).")] = None,
):
    """Dismiss a pull request review."""
    return _call("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/dismissals", locals())

# ── Actions / CI ─────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_workflows(owner: str, repo: str):
    """List workflows in a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/actions/workflows"))

@_op(gitea_read)
def get_workflow(owner: str, repo: str, workflow_id: str):
    """Get a workflow by ID or filename (e.g., 'ci.yml')."""
    return _ok(
        _get_client().get(f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}")
    )

@_op(gitea_execute)
def dispatch_workflow(
    owner: str,
    repo: str,
    workflow_id: Annotated[str, Field(description="Workflow file name under .gitea/workflows/ (e.g. 'ci.yml') or its numeric ID. The workflow must declare `on: workflow_dispatch`.")],
    ref: Annotated[str, Field(description="Branch name, tag name, or commit SHA to run the workflow against (e.g. 'main', 'v1.2.0').")],
    inputs: Annotated[dict | None, Field(description="Values for the workflow's `workflow_dispatch.inputs` — string keys (input names) → string values. Keys must match what the workflow file declares.")] = None,
):
    """Dispatch a workflow run."""
    body: dict = {"ref": ref, "inputs": inputs or {}}
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches",
            json=body,
        )
    )

@_op(gitea_read)
def list_workflow_runs(
    owner: str,
    repo: str,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea workflow-run objects.")] = True,
):
    """List workflow runs for a repository.

    brief (default True): compact view — id, title, status, conclusion,
    event, branch, sha, run_number, path, timestamps.
    Set brief=False for full Gitea API response objects."""
    params: dict = {"limit": limit, "page": page}
    data = _get_client().get(f"/repos/{owner}/{repo}/actions/runs", params=params)
    if brief:
        data = _slim_workflow_runs(data)
    return _ok(data)

@_op(gitea_read)
def get_workflow_run(owner: str, repo: str, run_id: int):
    """Get a workflow run by internal ID (not run_number). Use ListWorkflowRuns to find the id."""
    return _ok(_slim_workflow_run(_get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}")))

@_op(gitea_read)
def list_workflow_run_jobs(owner: str, repo: str, run_id: int):
    """List jobs for a workflow run by internal ID (not run_number). Use ListWorkflowRuns to find the id."""
    return _ok(_slim_jobs(
        _get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
    ))

@_op(gitea_read)
def get_workflow_job(owner: str, repo: str, job_id: int):
    """Get a workflow job by its ID."""
    return _ok(_slim_job(_get_client().get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}")))

@_op(gitea_read)
def get_workflow_job_logs(
    owner: str,
    repo: str,
    job_id: int,
    tail: Annotated[int | None, Field(description="Return only the last N lines of log output. Set to 0 (or null) for the full log.")] = 200,
    filter: Annotated[str | None, Field(description="Case-insensitive Python regex; only matching log lines are kept (e.g. 'error|fail|fatal'). Applied before `tail`.")] = None,
):
    """Get logs for a workflow job.

    tail (default 200): return only the last N lines. Set to 0 for full log.
    filter: regex pattern to grep log lines (e.g. 'error|fail|fatal').
    When both are set, filter is applied first, then tail."""
    text = _get_client().get_text(
        f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"
    )
    lines = text.splitlines()
    if filter:
        pat = re.compile(filter, re.IGNORECASE)
        lines = [line for line in lines if pat.search(line)]
    if tail and tail > 0:
        lines = lines[-tail:]
    return "\n".join(lines)

@_op(gitea_read)
def list_action_secrets(owner: str, repo: str):
    """List action secrets for a repository."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/actions/secrets")
    )

@_op(gitea_write)
def create_action_secret(
    owner: str,
    repo: str,
    secret_name: Annotated[str, Field(description="Secret name as referenced from workflows via ${{ secrets.NAME }} (uppercase recommended).")],
    data: Annotated[str, Field(description="Secret PLAINTEXT value — Gitea encrypts it at rest. Send raw, do not pre-encode/base64.")],
):
    """Create or update an action secret in a repository."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/actions/secrets/{secret_name}",
            json={"data": data},
        )
    )

@_op(gitea_delete)
def delete_action_secret(owner: str, repo: str, secret_name: str):
    """Delete an action secret from a repository."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/actions/secrets/{secret_name}")
    )

@_op(gitea_read)
def list_action_variables(owner: str, repo: str):
    """List action variables for a repository."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/actions/variables")
    )

@_op(gitea_read)
def get_action_variable(owner: str, repo: str, variable_name: str):
    """Get an action variable by name."""
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/actions/variables/{variable_name}"
        )
    )

@_op(gitea_write)
def create_action_variable(
    owner: str,
    repo: str,
    variable_name: Annotated[str, Field(description="Variable name as referenced from workflows via ${{ vars.NAME }} (uppercase recommended).")],
    value: Annotated[str, Field(description="Variable value (plaintext — visible to workflow logs; use create_action_secret for sensitive data).")],
):
    """Create an action variable in a repository."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_write)
def update_action_variable(
    owner: str,
    repo: str,
    variable_name: Annotated[str, Field(description="Variable name to update (must already exist).")],
    value: Annotated[str, Field(description="New variable value (plaintext — visible to workflow logs).")],
):
    """Update an action variable in a repository."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_delete)
def delete_action_variable(owner: str, repo: str, variable_name: str):
    """Delete an action variable from a repository."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/actions/variables/{variable_name}"
        )
    )

# ── Organizations ────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_orgs():
    """List organizations for the current user."""
    return _ok(_get_client().paginate("/user/orgs"))

@_op(gitea_read)
def get_org(org: str):
    """Get an organization by name."""
    return _ok(_get_client().get(f"/orgs/{org}"))

@_op(gitea_write)
def create_org(
    username: Annotated[str, Field(description="Organization login (the org's short name / URL slug). Not an existing user — this names the new org.")],
    full_name: str | None = None,
    description: str | None = None,
    website: str | None = None,
    visibility: _Visibility = None,
):
    """Create an organization."""
    visibility = _enforce_visibility(visibility)
    return _call("POST", "/orgs", locals())

@_op(gitea_write)
def edit_org(
    org: str,
    full_name: str | None = None,
    description: str | None = None,
    website: str | None = None,
    visibility: _Visibility = None,
):
    """Edit an organization's properties."""
    visibility = _enforce_visibility(visibility)
    return _call("PATCH", "/orgs/{org}", locals())

@_op(gitea_delete)
def delete_org(org: str):
    """Delete an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}"))

@_op(gitea_read)
def list_org_repos(
    org: str,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea repo objects.")] = True,
):
    """List repositories in an organization.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate(f"/orgs/{org}/repos")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_read)
def list_org_members(org: str):
    """List members of an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/members"))

@_op(gitea_read)
def check_org_membership(org: str, username: str):
    """Check if a user is a member of an organization."""
    return _ok(_get_client().get(f"/orgs/{org}/members/{username}"))

@_op(gitea_delete)
def remove_org_member(org: str, username: str):
    """Remove a member from an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/members/{username}"))

@_op(gitea_read)
def list_org_public_members(org: str):
    """List public members of an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/public_members"))

@_op(gitea_read)
def check_org_public_member(org: str, username: str):
    """Check if a user is a public member of an organization."""
    return _ok(_get_client().get(f"/orgs/{org}/public_members/{username}"))

@_op(gitea_write)
def set_org_public_member(org: str, username: str):
    """Publicize a user's membership in an organization."""
    return _ok(_get_client().put(f"/orgs/{org}/public_members/{username}"))

@_op(gitea_delete)
def remove_org_public_member(org: str, username: str):
    """Conceal a user's membership in an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/public_members/{username}"))

@_op(gitea_write)
def create_org_repo(
    org: str,
    name: Annotated[str, Field(description="Repository slug (URL-safe short name).")],
    description: str | None = None,
    private: Annotated[bool | None, Field(description="True = private repo. Public repos are blocked unless the server was started with --allow-public.")] = None,
    auto_init: Annotated[bool | None, Field(description="True = create an initial commit (README/license/gitignore based on the fields below).")] = None,
    gitignores: Annotated[str | None, Field(description="Comma-separated .gitignore template names (e.g. 'Python,Node').")] = None,
    license: Annotated[str | None, Field(description="License template name (e.g. 'MIT', 'Apache-2.0').")] = None,
    readme: Annotated[str | None, Field(description="README template name (e.g. 'Default').")] = None,
    default_branch: Annotated[str | None, Field(description="Default branch name for the new repo (e.g. 'main').")] = None,
):
    """Create a repository in an organization."""
    private = _enforce_private(private)
    return _call("POST", "/orgs/{org}/repos", locals())

@_op(gitea_read)
def list_user_orgs(username: str):
    """List organizations for a specific user."""
    return _ok(_get_client().paginate(f"/users/{username}/orgs"))

# ── Teams ────────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_teams(org: str):
    """List teams in an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/teams"))

@_op(gitea_read)
def get_team(team_id: int):
    """Get a team by ID."""
    return _ok(_get_client().get(f"/teams/{team_id}"))

@_op(gitea_write)
def create_team(
    org: str,
    name: Annotated[str, Field(description="Team name (unique within the org).")],
    permission: _TeamPermission = None,
    units: Annotated[list[str] | None, Field(description=(
        "Repo features this team can access. Each value is a unit key: "
        "'repo.code', 'repo.issues', 'repo.pulls', 'repo.releases', "
        "'repo.wiki', 'repo.ext_wiki', 'repo.ext_issues', 'repo.projects', "
        "'repo.packages', 'repo.actions'. Omit for Gitea default set."
    ))] = None,
    description: str | None = None,
):
    """Create a team in an organization. Permission can be: read, write, admin. Units are like: repo.code, repo.issues, repo.pulls."""
    return _call("POST", "/orgs/{org}/teams", locals())

@_op(gitea_write)
def edit_team(
    team_id: int,
    name: Annotated[str, Field(description="Team name to keep or replace; Gitea requires it on every edit.")],
    description: str | None = None,
    permission: _TeamPermission = None,
    units: Annotated[list[str] | None, Field(description=(
        "Repo features this team can access. Each value is a unit key: "
        "'repo.code', 'repo.issues', 'repo.pulls', 'repo.releases', "
        "'repo.wiki', 'repo.ext_wiki', 'repo.ext_issues', 'repo.projects', "
        "'repo.packages', 'repo.actions'."
    ))] = None,
):
    """Edit a team's properties."""
    return _call("PATCH", "/teams/{team_id}", locals())

@_op(gitea_delete)
def delete_team(team_id: int):
    """Delete a team."""
    return _ok(_get_client().delete(f"/teams/{team_id}"))

@_op(gitea_read)
def list_team_members(team_id: int):
    """List members of a team."""
    return _ok(_get_client().paginate(f"/teams/{team_id}/members"))

@_op(gitea_write)
def add_team_member(team_id: int, username: str):
    """Add a member to a team."""
    return _ok(_get_client().put(f"/teams/{team_id}/members/{username}"))

@_op(gitea_delete)
def remove_team_member(team_id: int, username: str):
    """Remove a member from a team."""
    return _ok(_get_client().delete(f"/teams/{team_id}/members/{username}"))

@_op(gitea_read)
def list_team_repos(team_id: int):
    """List repositories managed by a team."""
    return _ok(_get_client().paginate(f"/teams/{team_id}/repos"))

@_op(gitea_write)
def add_team_repo(team_id: int, org: str, repo: str):
    """Add a repository to a team."""
    return _ok(_get_client().put(f"/teams/{team_id}/repos/{org}/{repo}"))

@_op(gitea_delete)
def remove_team_repo(team_id: int, org: str, repo: str):
    """Remove a repository from a team."""
    return _ok(_get_client().delete(f"/teams/{team_id}/repos/{org}/{repo}"))

@_op(gitea_read)
def check_team_repo(team_id: int, org: str, repo: str):
    """Check if a repository belongs to a team."""
    return _ok(_get_client().get(f"/teams/{team_id}/repos/{org}/{repo}"))

# ── Org Labels ───────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_labels(org: str):
    """List labels for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/labels"))

@_op(gitea_write)
def create_org_label(
    org: str,
    name: Annotated[str, Field(description="Label name (unique within the org).")],
    color: Annotated[str, Field(description="Hex color, e.g. '#00ff00' or '00ff00'.")],
    description: str | None = None,
):
    """Create a label in an organization."""
    return _call("POST", "/orgs/{org}/labels", locals())

@_op(gitea_write)
def edit_org_label(
    org: str,
    label_id: int,
    name: str | None = None,
    color: Annotated[str | None, Field(description="Hex color, e.g. '#00ff00' or '00ff00'.")] = None,
    description: str | None = None,
):
    """Edit an organization label."""
    return _call("PATCH", "/orgs/{org}/labels/{label_id}", locals())

@_op(gitea_delete)
def delete_org_label(org: str, label_id: int):
    """Delete an organization label."""
    return _ok(_get_client().delete(f"/orgs/{org}/labels/{label_id}"))

# ── Notifications ────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_notifications(
    all: Annotated[bool | None, Field(description="True = include already-read notifications. False/omitted (default) = unread only.")] = None,
    status_types: Annotated[list[Literal["unread", "read", "pinned"]] | None, Field(description="Filter by status. Defaults to ['unread', 'pinned'] server-side.")] = None,
    subject_type: Annotated[list[Literal["issue", "pull", "commit", "repository"]] | None, Field(description="Filter by notification subject type.")] = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view (id, repo, subject type/title, unread, updated_at); False = full Gitea notification objects.")] = True,
):
    """List notifications for the current user.

    brief (default True): compact view — id, repo, subject type/title, unread,
    updated_at. Set brief=False for full objects."""
    params = _body(
        locals(),
        exclude=("brief",),
        rename={"status_types": "status-types", "subject_type": "subject-type"},
    )
    data = _get_client().paginate("/notifications", params=params or None)
    if brief:
        data = _slim_notifications(data)
    return _ok(data)

@_op(gitea_write)
def mark_notifications_read(
    last_read_at: Annotated[str | None, Field(description="ISO-8601 timestamp (e.g. '2026-05-20T12:00:00Z'). Notifications updated at or before this time are marked read. Defaults to now.")] = None,
):
    """Mark all notifications as read."""
    # last_read_at is a query param on this endpoint, not a body field.
    params: dict = {}
    if last_read_at is not None:
        params["last_read_at"] = last_read_at
    return _ok(_get_client().put("/notifications", params=params or None))

@_op(gitea_read)
def get_notification_thread(thread_id: int):
    """Get a notification thread by ID."""
    return _ok(_get_client().get(f"/notifications/threads/{thread_id}"))

@_op(gitea_write)
def mark_notification_read(thread_id: int):
    """Mark a notification thread as read."""
    return _ok(_get_client().patch(f"/notifications/threads/{thread_id}"))

@_op(gitea_read)
def list_repo_notifications(
    owner: str,
    repo: str,
    all: Annotated[bool | None, Field(description="True = include already-read notifications. False/omitted (default) = unread only.")] = None,
    status_types: Annotated[list[Literal["unread", "read", "pinned"]] | None, Field(description="Filter by status. Defaults to ['unread', 'pinned'] server-side.")] = None,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea notification objects.")] = True,
):
    """List notifications for a repository.

    brief (default True): compact view. Set brief=False for full objects."""
    params = _body(
        locals(),
        exclude=("owner", "repo", "brief"),
        rename={"status_types": "status-types"},
    )
    data = _get_client().paginate(
        f"/repos/{owner}/{repo}/notifications", params=params or None
    )
    if brief:
        data = _slim_notifications(data)
    return _ok(data)

@_op(gitea_write)
def mark_repo_notifications_read(
    owner: str,
    repo: str,
    last_read_at: Annotated[str | None, Field(description="ISO-8601 timestamp (e.g. '2026-05-20T12:00:00Z'). Notifications updated at or before this time are marked read. Defaults to now.")] = None,
):
    """Mark all notifications in a repository as read."""
    # last_read_at is a query param on this endpoint, not a body field.
    params: dict = {}
    if last_read_at is not None:
        params["last_read_at"] = last_read_at
    return _ok(
        _get_client().put(f"/repos/{owner}/{repo}/notifications", params=params or None)
    )

@_op(gitea_read)
def get_new_notification_count():
    """Get the count of unread notifications."""
    return _ok(_get_client().get("/notifications/new"))

# ── Wiki ─────────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_wiki_pages(owner: str, repo: str):
    """List wiki pages in a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/wiki/pages"))

@_op(gitea_read)
def get_wiki_page(owner: str, repo: str, page_name: str):
    """Get a wiki page by name."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/wiki/page/{page_name}"))

@_op(gitea_write)
def create_wiki_page(
    owner: str,
    repo: str,
    title: Annotated[str, Field(description="Wiki page title. Used as the page's display name and slug source.")],
    content: Annotated[str, Field(description="Page body as PLAINTEXT (typically Markdown). The tool base64-encodes it for the API — do NOT pre-encode.")],
    message: Annotated[str | None, Field(description="Git commit message for the wiki commit. Defaults to a Gitea-generated message if omitted.")] = None,
):
    """Create a new wiki page. Content is provided as plain text and will be base64-encoded automatically."""
    encoded = base64.b64encode(content.encode()).decode()
    body: dict = {"title": title, "content_base64": encoded}
    if message is not None:
        body["message"] = message
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/wiki/new", json=body)
    )

@_op(gitea_write)
def edit_wiki_page(
    owner: str,
    repo: str,
    page_name: str,
    title: Annotated[str | None, Field(description="New wiki page title. Omit to keep the existing title.")] = None,
    content: Annotated[str | None, Field(description="Replacement page body as PLAINTEXT (typically Markdown). The tool base64-encodes it for the API — do NOT pre-encode.")] = None,
    message: Annotated[str | None, Field(description="Git commit message for the wiki commit. Defaults to a Gitea-generated message if omitted.")] = None,
):
    """Edit a wiki page. Content is provided as plain text and will be base64-encoded automatically."""
    body = _body(locals(), exclude=("owner", "repo", "page_name", "content"))
    if content is not None:
        body["content_base64"] = base64.b64encode(content.encode()).decode()
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/wiki/page/{page_name}", json=body
        )
    )

@_op(gitea_delete)
def delete_wiki_page(owner: str, repo: str, page_name: str):
    """Delete a wiki page."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/wiki/page/{page_name}")
    )

# ── Packages ─────────────────────────────────────────────────────────────────


_PACKAGE_TYPES = Literal["alpine", "cargo", "chef", "composer", "conan", "conda", "container", "cran", "debian", "generic", "go", "helm", "maven", "npm", "nuget", "pub", "pypi", "rpm", "rubygems", "swift", "terraform", "vagrant"]
_PackageType = Annotated[_PACKAGE_TYPES, Field(description="Gitea package registry type (the package format).")]
_PackageTypeFilter = Annotated[_PACKAGE_TYPES | None, Field(description="Filter by Gitea package registry type. Omit to list all package types for the owner.")]
_PackageName = Annotated[str, Field(description="Package name as registered (format depends on type).")]


@_op(gitea_read)
def list_packages(
    owner: str,
    type: _PackageTypeFilter = None,
):
    """List packages for an owner. Type can filter by package type."""
    params = _body(locals(), exclude=("owner",))
    return _ok(
        _get_client().paginate(f"/packages/{owner}", params=params or None)
    )

@_op(gitea_read)
def get_package(
    owner: str,
    type: Annotated[_PACKAGE_TYPES, Field(description="Gitea package registry type (the package format).")],
    name: Annotated[str, Field(description="Package name as registered (format depends on type: e.g. 'mypkg' for npm/pypi, 'group:artifact' for maven, 'image' for container).")],
    version: Annotated[str, Field(description="Package version string as registered (e.g. '1.2.3', 'v0.5.0', container tag 'latest').")],
):
    """Get a package by type, name, and version."""
    return _ok(
        _get_client().get(f"/packages/{owner}/{type}/{name}/{version}")
    )

@_op(gitea_delete)
def delete_package(
    owner: str,
    type: _PackageType,
    name: _PackageName,
    version: Annotated[str, Field(description="Package version string to delete.")],
):
    """Delete a package."""
    return _ok(
        _get_client().delete(f"/packages/{owner}/{type}/{name}/{version}")
    )

@_op(gitea_read)
def list_package_files(
    owner: str,
    type: _PackageType,
    name: _PackageName,
    version: Annotated[str, Field(description="Package version whose files should be listed.")],
):
    """List files in a package."""
    return _ok(
        _get_client().get(f"/packages/{owner}/{type}/{name}/{version}/files")
    )

# ── Admin ────────────────────────────────────────────────────────────────────


@_op(gitea_admin_read)
def admin_list_users():
    """List all users (admin only)."""
    return _ok(_get_client().paginate("/admin/users"))

@_op(gitea_admin_write)
def admin_create_user(
    username: Annotated[str, Field(description="Login name for the new user (URL slug, unique on the instance).")],
    email: Annotated[str, Field(description="Primary email address for the new user.")],
    password: Annotated[str, Field(description="Initial password (subject to Gitea's password policy).")],
    must_change_password: Annotated[bool | None, Field(description="True = force the user to set a new password on first login.")] = None,
    login_name: Annotated[str | None, Field(description="External login name when the account is linked to an auth source. Defaults to `username` for local auth.")] = None,
    send_notify: Annotated[bool | None, Field(description="True = email the new user about their account being created.")] = None,
):
    """Create a new user (admin only)."""
    return _call("POST", "/admin/users", locals())

@_op(gitea_admin_write)
def admin_edit_user(
    username: str,
    login_name: Annotated[str, Field(description="External login name; for local accounts pass the current username. Gitea requires it on every edit.")],
    email: Annotated[str | None, Field(description="New primary email address.")] = None,
    password: Annotated[str | None, Field(description="New password (subject to Gitea's password policy).")] = None,
    must_change_password: Annotated[bool | None, Field(description="True = require the user to set a new password on next login.")] = None,
    active: Annotated[bool | None, Field(description="False = deactivate the account (cannot log in).")] = None,
    admin: Annotated[bool | None, Field(description="True = grant site-admin privileges; False = revoke.")] = None,
    allow_git_hook: Annotated[bool | None, Field(description="True = allow this user to configure server-side git hooks on their repos.")] = None,
    max_repo_creation: Annotated[int | None, Field(description="Per-user repo creation cap. -1 = unlimited.")] = None,
    prohibit_login: Annotated[bool | None, Field(description="True = block this user from signing in (locks the account without deleting it).")] = None,
):
    """Edit a user's properties (admin only)."""
    return _call("PATCH", "/admin/users/{username}", locals())

@_op(gitea_admin_write)
def admin_delete_user(
    username: str,
    purge: Annotated[bool, Field(description="True = also delete the user's repositories, packages, and other owned resources. False = refuse deletion if the user still owns content.")] = False,
):
    """Delete a user (admin only). Set purge=True to also delete owned repos, etc."""
    params: dict = {}
    if purge:
        params["purge"] = True
    return _ok(_get_client().delete(f"/admin/users/{username}", params=params or None))

@_op(gitea_admin_read)
def admin_list_orgs():
    """List all organizations (admin only)."""
    return _ok(_get_client().paginate("/admin/orgs"))

@_op(gitea_admin_read)
def admin_list_cron_jobs():
    """List cron jobs (admin only)."""
    return _ok(_get_client().paginate("/admin/cron"))

@_op(gitea_admin_write)
def admin_run_cron_job(
    task_name: Annotated[str, Field(description="Cron task name as listed by admin_list_cron_jobs (e.g. 'cleanup_hook_task_table', 'sync_external_users', 'repo_health_check').")],
):
    """Run a cron job by name (admin only)."""
    return _ok(_get_client().post(f"/admin/cron/{task_name}"))

@_op(gitea_admin_read)
def admin_list_repos(
    limit: Annotated[int | None, Field(description="Page size. Defaults to 50.")] = None,
    page: Annotated[int | None, Field(description="1-based page number. When given, only that page is returned; omitted = walk every page.")] = None,
    private: Annotated[bool | None, Field(description="Include private repos the token can see. Omitted = server default (true, private repos included). False = public repos only. This widens/narrows the listing; it is NOT a private-only filter.")] = None,
):
    """List every repository on the instance (admin only).

    Gitea exposes no /admin/repos endpoint — this searches with the caller's
    token, which for an admin covers all repos including private ones."""
    page_size = limit or 50
    result: list = []
    current = page or 1
    while True:
        params: dict = {"limit": page_size, "page": current}
        if private is not None:
            params["private"] = private
        data = _get_client().get("/repos/search", params=params)
        batch = data.get("data") or [] if isinstance(data, dict) else data
        result.extend(batch)
        if page is not None or len(batch) < page_size:
            break
        current += 1
    return _ok(result)

@_op(gitea_admin_write)
def admin_create_org(
    username: Annotated[str, Field(description="Organization login (the new org's short name / URL slug). Not an existing user.")],
    owner_name: Annotated[str, Field(description="Existing username that will own the new org.")],
    full_name: Annotated[str | None, Field(description="Display name shown in the UI (free text). Defaults to `username`.")] = None,
    description: str | None = None,
    website: str | None = None,
    visibility: _Visibility = None,
):
    """Create an organization (admin only). owner_name is the user who will own the org."""
    visibility = _enforce_visibility(visibility)
    return _call("POST", "/admin/users/{owner_name}/orgs", locals())

@_op(gitea_admin_write)
def admin_create_repo_for_user(
    username: Annotated[str, Field(description="Existing username that will own the new repo.")],
    name: Annotated[str, Field(description="Repository slug (URL-safe short name).")],
    description: str | None = None,
    private: Annotated[bool | None, Field(description="True = private repo. Public repos are blocked unless the server was started with --allow-public.")] = None,
    auto_init: Annotated[bool | None, Field(description="True = create an initial commit (Gitea generates README based on defaults).")] = None,
):
    """Create a repository for a user (admin only)."""
    private = _enforce_private(private)
    return _call("POST", "/admin/users/{username}/repos", locals())

@_op(gitea_admin_write)
def admin_rename_user(
    username: str,
    new_username: Annotated[str, Field(description="New login name (URL slug) for the user. Must be unique on the instance.")],
):
    """Rename a user (admin only)."""
    return _ok(
        _get_client().post(
            f"/admin/users/{username}/rename",
            json={"new_username": new_username},
        )
    )

@_op(gitea_admin_write)
def admin_create_user_public_key(
    username: str,
    title: Annotated[str, Field(description="Human-readable key label.")],
    key: Annotated[str, Field(description="OpenSSH public-key text — full line, e.g. 'ssh-ed25519 AAAA... user@host'.")],
):
    """Add a public key for a user (admin only)."""
    return _ok(
        _get_client().post(
            f"/admin/users/{username}/keys",
            json={"title": title, "key": key},
        )
    )

@_op(gitea_admin_write)
def admin_delete_user_public_key(username: str, key_id: int):
    """Delete a public key for a user (admin only)."""
    return _ok(_get_client().delete(f"/admin/users/{username}/keys/{key_id}"))

@_op(gitea_admin_read)
def admin_list_unadopted_repos():
    """List unadopted repositories (admin only)."""
    return _ok(_get_client().paginate("/admin/unadopted"))

@_op(gitea_admin_write)
def admin_adopt_repo(owner: str, repo: str):
    """Adopt an unadopted repository (admin only)."""
    return _ok(_get_client().post(f"/admin/unadopted/{owner}/{repo}"))

@_op(gitea_admin_write)
def admin_delete_unadopted_repo(owner: str, repo: str):
    """Delete an unadopted repository (admin only)."""
    return _ok(_get_client().delete(f"/admin/unadopted/{owner}/{repo}"))

@_op(gitea_admin_read)
def admin_list_emails(
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = None,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
):
    """List all emails (admin only)."""
    params = _body(locals())
    return _ok(_get_client().paginate("/admin/emails", params=params or None))

@_op(gitea_admin_read)
def admin_search_emails(
    query: Annotated[str, Field(description="Search keyword (substring match against user email address).")],
):
    """Search emails (admin only)."""
    return _ok(_get_client().paginate("/admin/emails/search", params={"q": query}))

# ── Actions Runners ──────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_runners(owner: str, repo: str):
    """List action runners for a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/actions/runners"))

@_op(gitea_read)
def get_repo_runner(owner: str, repo: str, runner_id: int):
    """Get an action runner for a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/actions/runners/{runner_id}"))

@_op(gitea_delete)
def delete_repo_runner(owner: str, repo: str, runner_id: int):
    """Delete an action runner from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/actions/runners/{runner_id}"))

@_op(gitea_read)
def list_org_runners(org: str):
    """List action runners for an organization."""
    return _ok(_get_client().get(f"/orgs/{org}/actions/runners"))

@_op(gitea_read)
def get_org_runner(org: str, runner_id: int):
    """Get an action runner for an organization."""
    return _ok(_get_client().get(f"/orgs/{org}/actions/runners/{runner_id}"))

@_op(gitea_delete)
def delete_org_runner(org: str, runner_id: int):
    """Delete an action runner from an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/actions/runners/{runner_id}"))

@_op(gitea_admin_read)
def list_admin_runners():
    """List all action runners (admin only)."""
    return _ok(_get_client().get("/admin/actions/runners"))

@_op(gitea_admin_read)
def get_admin_runner(runner_id: int):
    """Get an action runner (admin only)."""
    return _ok(_get_client().get(f"/admin/actions/runners/{runner_id}"))

@_op(gitea_admin_write)
def delete_admin_runner(runner_id: int):
    """Delete an action runner (admin only)."""
    return _ok(_get_client().delete(f"/admin/actions/runners/{runner_id}"))

@_op(gitea_admin_write)
def create_admin_runner_token():
    """Get a global actions runner registration token (admin only)."""
    return _ok(_get_client().post("/admin/actions/runners/registration-token"))

@_op(gitea_read)
def list_user_runners():
    """List action runners for the authenticated user."""
    return _ok(_get_client().get("/user/actions/runners"))

@_op(gitea_read)
def get_user_runner(runner_id: int):
    """Get an action runner for the authenticated user."""
    return _ok(_get_client().get(f"/user/actions/runners/{runner_id}"))

@_op(gitea_delete)
def delete_user_runner(runner_id: int):
    """Delete an action runner for the authenticated user."""
    return _ok(_get_client().delete(f"/user/actions/runners/{runner_id}"))

@_op(gitea_write)
def create_user_runner_token():
    """Get a user-level actions runner registration token."""
    return _ok(_get_client().post("/user/actions/runners/registration-token"))

@_op(gitea_write)
def create_repo_runner_token(owner: str, repo: str):
    """Get a repo-level actions runner registration token."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/actions/runners/registration-token"))

@_op(gitea_write)
def create_org_runner_token(org: str):
    """Get an org-level actions runner registration token."""
    return _ok(_get_client().post(f"/orgs/{org}/actions/runners/registration-token"))

# ── Actions - Org Secrets/Variables ──────────────────────────────────────


@_op(gitea_read)
def list_org_action_secrets(org: str):
    """List action secrets for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/actions/secrets"))

@_op(gitea_write)
def create_org_action_secret(
    org: str,
    secret_name: Annotated[str, Field(description="Secret name. Must match `^[A-Z_][A-Z0-9_]*$` (Gitea constraint). Referenced from workflows via ${{ secrets.NAME }}.")],
    data: Annotated[str, Field(description="Secret PLAINTEXT value — Gitea encrypts it at rest. Not retrievable via API afterwards.")],
):
    """Create or update an action secret in an organization."""
    return _ok(
        _get_client().put(
            f"/orgs/{org}/actions/secrets/{secret_name}",
            json={"data": data},
        )
    )

@_op(gitea_delete)
def delete_org_action_secret(org: str, secret_name: str):
    """Delete an action secret from an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/actions/secrets/{secret_name}"))

@_op(gitea_read)
def list_org_action_variables(org: str):
    """List action variables for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/actions/variables"))

@_op(gitea_read)
def get_org_action_variable(org: str, variable_name: str):
    """Get an action variable for an organization."""
    return _ok(_get_client().get(f"/orgs/{org}/actions/variables/{variable_name}"))

@_op(gitea_write)
def create_org_action_variable(
    org: str,
    variable_name: Annotated[str, Field(description="Variable name. Must match `^[A-Z_][A-Z0-9_]*$` (Gitea constraint). Referenced from workflows via ${{ vars.NAME }}.")],
    value: Annotated[str, Field(description="Variable value (plaintext — visible to workflow logs; use create_org_action_secret for sensitive data).")],
):
    """Create an action variable in an organization."""
    return _ok(
        _get_client().post(
            f"/orgs/{org}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_write)
def update_org_action_variable(
    org: str,
    variable_name: Annotated[str, Field(description="Variable name to update (must already exist).")],
    value: Annotated[str, Field(description="New variable value (plaintext — visible to workflow logs).")],
):
    """Update an action variable in an organization."""
    return _ok(
        _get_client().put(
            f"/orgs/{org}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_delete)
def delete_org_action_variable(org: str, variable_name: str):
    """Delete an action variable from an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/actions/variables/{variable_name}"))

# ── Actions - User Secrets/Variables ─────────────────────────────────────
# No list op: Gitea exposes GET on org and repo secrets but not on user ones.


@_op(gitea_write)
def create_user_action_secret(
    secret_name: Annotated[str, Field(description="Secret name. Must match `^[A-Z_][A-Z0-9_]*$` (Gitea constraint). Referenced from workflows via ${{ secrets.NAME }}.")],
    data: Annotated[str, Field(description="Secret PLAINTEXT value — Gitea encrypts it at rest. Not retrievable via API afterwards.")],
):
    """Create or update an action secret for the current user."""
    return _ok(
        _get_client().put(
            f"/user/actions/secrets/{secret_name}",
            json={"data": data},
        )
    )

@_op(gitea_delete)
def delete_user_action_secret(secret_name: str):
    """Delete an action secret for the current user."""
    return _ok(_get_client().delete(f"/user/actions/secrets/{secret_name}"))

@_op(gitea_read)
def list_user_action_variables():
    """List action variables for the current user."""
    return _ok(_get_client().paginate("/user/actions/variables"))

@_op(gitea_read)
def get_user_action_variable(variable_name: str):
    """Get an action variable for the current user."""
    return _ok(_get_client().get(f"/user/actions/variables/{variable_name}"))

@_op(gitea_write)
def create_user_action_variable(
    variable_name: Annotated[str, Field(description="Variable name. Must match `^[A-Z_][A-Z0-9_]*$` (Gitea constraint). Referenced from workflows via ${{ vars.NAME }}.")],
    value: Annotated[str, Field(description="Variable value (plaintext — visible to workflow logs; use create_user_action_secret for sensitive data).")],
):
    """Create an action variable for the current user."""
    return _ok(
        _get_client().post(
            f"/user/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_write)
def update_user_action_variable(
    variable_name: Annotated[str, Field(description="Variable name to update (must already exist).")],
    value: Annotated[str, Field(description="New variable value (plaintext — visible to workflow logs).")],
):
    """Update an action variable for the current user."""
    return _ok(
        _get_client().put(
            f"/user/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_delete)
def delete_user_action_variable(variable_name: str):
    """Delete an action variable for the current user."""
    return _ok(_get_client().delete(f"/user/actions/variables/{variable_name}"))

# ── Misc ─────────────────────────────────────────────────────────────────────


@_op(gitea_write)
def render_markdown(
    text: Annotated[str, Field(description="Raw Markdown source to render.")],
    mode: Annotated[Literal["markdown", "comment", "wiki", "gfm"] | None, Field(description="Render mode. 'markdown' = strict CommonMark; 'gfm' = GitHub-flavored Markdown; 'comment' = issue/PR comment context; 'wiki' = wiki page context.")] = None,
    context: Annotated[str | None, Field(description="Repository path like 'owner/repo' — used to resolve relative links and #N issue references.")] = None,
    wiki: Annotated[bool | None, Field(description="True = treat input as a wiki page (enables wiki-style links). Equivalent to mode='wiki'.")] = None,
):
    """Render a markdown string. Returns HTML text."""
    body: dict = {"Text": text}
    if mode is not None:
        body["Mode"] = mode
    if context is not None:
        body["Context"] = context
    if wiki is not None:
        body["Wiki"] = wiki
    return _get_client()._text("POST", "/markdown", json=body)

@_op(gitea_read)
def search_topics(
    query: Annotated[str, Field(description="Search keyword (substring match against topic names).")],
):
    """Search for topics by keyword."""
    return _ok(_get_client().get("/topics/search", params={"q": query}))

@_op(gitea_read)
def list_gitignore_templates():
    """List available .gitignore templates."""
    return _ok(_get_client().get("/gitignore/templates"))

@_op(gitea_read)
def list_license_templates():
    """List available license templates."""
    return _ok(_get_client().get("/licenses"))

@_op(gitea_read)
def get_signing_key():
    """Get the default signing key for the Gitea instance."""
    return _get_client().get_text("/signing-key.gpg")

@_op(gitea_read)
def get_nodeinfo():
    """Get NodeInfo for the Gitea instance."""
    return _ok(_get_client().get("/nodeinfo"))

@_op(gitea_read)
def get_gitignore_template(
    name: Annotated[str, Field(description="Template name as returned by list_gitignore_templates (e.g. 'Go', 'Python', 'Node').")],
):
    """Get a specific .gitignore template by name."""
    return _ok(_get_client().get(f"/gitignore/templates/{name}"))

@_op(gitea_read)
def get_license_template(
    name: Annotated[str, Field(description="License template name as returned by list_license_templates (e.g. 'MIT', 'Apache-2.0', 'GPL-3.0').")],
):
    """Get a specific license template by name."""
    return _ok(_get_client().get(f"/licenses/{name}"))

@_op(gitea_read)
def list_package_versions(
    owner: str,
    type: _PackageType,
    name: _PackageName,
):
    """List versions of a package."""
    return _ok(
        _get_client().paginate(f"/packages/{owner}/{type}/{name}")
    )

@_op(gitea_read)
def get_repo_languages(owner: str, repo: str):
    """Get the languages used in a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/languages"))

@_op(gitea_read)
def list_repo_activities(
    owner: str,
    repo: str,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = None,
):
    """List activity feeds for a repository."""
    params = _body(locals(), exclude=("owner", "repo"))
    return _ok(
        _get_client().paginate(
            f"/repos/{owner}/{repo}/activities/feeds", params=params or None
        )
    )

@_op(gitea_read)
def get_repo_git_notes(
    owner: str,
    repo: str,
    sha: Annotated[str, Field(description="Commit SHA whose git-note should be fetched.")],
):
    """Get a git note for a commit."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/git/notes/{sha}"))

@_op(gitea_read)
def get_repo_archive(
    owner: str,
    repo: str,
    archive: Annotated[str, Field(description="Archive ref + format, e.g. 'main.tar.gz', 'main.zip', 'v1.2.0.tar.gz', '<commit-sha>.zip'. Extension chooses the format.")],
):
    """Get an archive of a repository. archive should be like 'main.tar.gz' or 'main.zip'."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/archive/{archive}")

@_op(gitea_read)
def list_repo_refs(
    owner: str,
    repo: str,
    ref_type: Annotated[Literal["", "heads", "tags"], Field(description="Filter: '' (default) lists all refs, 'heads' lists branches, 'tags' lists tags.")] = "",
):
    """List git references in a repository. ref_type can be empty, 'heads', or 'tags'."""
    path = f"/repos/{owner}/{repo}/git/refs"
    if ref_type:
        path = f"{path}/{ref_type}"
    return _ok(_get_client().get(path))

@_op(gitea_read)
def get_git_tree(
    owner: str,
    repo: str,
    sha: Annotated[str, Field(description="Tree SHA or commit SHA whose tree should be returned.")],
    recursive: Annotated[bool | None, Field(description="True = include all descendant entries (full tree). False/omitted = only direct children.")] = None,
):
    """Get the tree for a commit SHA."""
    return _call("GET", "/repos/{owner}/{repo}/git/trees/{sha}", locals())

@_op(gitea_write)
def transfer_repo(
    owner: str,
    repo: str,
    new_owner: Annotated[str, Field(description="Username or org login that should receive the repository.")],
    team_ids: Annotated[list[int] | None, Field(description="Team IDs to grant access on transfer (only meaningful when `new_owner` is an organization).")] = None,
):
    """Transfer a repository to another owner."""
    return _call("POST", "/repos/{owner}/{repo}/transfer", locals())

@_op(gitea_write)
def create_repo_from_template(
    template_owner: Annotated[str, Field(description="Owner (user/org) of the source template repository.")],
    template_repo: Annotated[str, Field(description="Name of the source template repository (must have `is_template` set).")],
    name: Annotated[str, Field(description="Repository slug for the new repo (URL-safe short name).")],
    owner: Annotated[str, Field(description="Username or org login that will own the new repo.")],
    description: str | None = None,
    private: Annotated[bool | None, Field(description="True = create the new repo as private.")] = None,
    git_content: Annotated[bool | None, Field(description="True = copy git history/files from the template.")] = None,
    topics: Annotated[bool | None, Field(description="True = copy the template's topics to the new repo.")] = None,
    labels: Annotated[bool | None, Field(description="True = copy issue labels from the template.")] = None,
):
    """Create a repository from a template."""
    private = _enforce_private(private)
    return _call("POST", "/repos/{template_owner}/{template_repo}/generate", locals())

@_op(gitea_read)
def list_repo_assignees(owner: str, repo: str):
    """List users who can be assigned to issues in a repository."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/assignees"))

@_op(gitea_read)
def list_repo_reviewers(owner: str, repo: str):
    """List users who can review pull requests in a repository."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/reviewers"))

@_op(gitea_read)
def get_pull_review_comments(
    owner: str, repo: str, index: int, review_id: int
):
    """List comments on a pull request review."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/comments"
        )
    )

@_op(gitea_delete)
def delete_pull_review(owner: str, repo: str, index: int, review_id: int):
    """Delete a pull request review."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}"
        )
    )

@_op(gitea_delete)
def remove_pull_reviewers(
    owner: str,
    repo: str,
    index: int,
    reviewers: Annotated[list[str], Field(description="Usernames whose review request should be removed from this PR.")],
):
    """Remove reviewers from a pull request."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/pulls/{index}/requested_reviewers",
            json={"reviewers": reviewers},
        )
    )


# ── Long-running waiters (Actions) ───────────────────────────────────────────
#
# The result dict is the source of truth; ctx progress/log are best-effort.
# All wait ops live in gitea_read: a wait only ever GETs, and cancel stops
# the local task, not the run. Pattern: mcp-server-v2 "Long-running waiters".


_log_wait = logging.getLogger("gitea_mcp.wait")

# One transient blip must not kill a minutes-long wait; fatal 4xx never heal.
_MAX_POLL_FAILURES_DEFAULT = 3
# Bounds orphan background waits (e.g. run stuck `waiting` with no runner).
_MAX_LIFETIME_DEFAULT = 7200.0

_TERMINAL_LOG_LEVEL: dict = {
    "success": "info",
    "failure": "error",
    "cancelled": "warning",
    "skipped": "warning",
    "completed": "info",
    "blocked": "info",
}


def _wait_result(
    payload_key: str, payload, status, terminated, elapsed_final,
    polls, poll_failures, last_poll_error,
) -> dict:
    result: dict = {
        payload_key: payload,
        "status": status,
        "terminated": terminated,
        "timed_out": not terminated,
        "elapsed_seconds": round(elapsed_final, 2),
        "polls": polls,
    }
    if poll_failures:
        result["poll_failures"] = poll_failures
        result["last_poll_error"] = last_poll_error
    return result


async def _emit_wait_summary(
    ctx, label: str, status, terminated: bool, timeout, polls, elapsed_final,
) -> None:
    if terminated:
        level = _TERMINAL_LOG_LEVEL.get(status or "", "info")
        await _emit_log(
            ctx, level,
            f"{label} finished with status={status} "
            f"after {polls} polls in {elapsed_final:.1f}s",
        )
    else:
        await _emit_log(
            ctx, "warning",
            f"{label} did not reach a terminal status "
            f"in {timeout}s (last status={status}, polls={polls})",
        )


def _poll_error_is_fatal(e: Exception) -> bool:
    """4xx (except 429) won't heal on retry; everything else is budgeted."""
    return (
        isinstance(e, GiteaError)
        and 400 <= e.status < 500
        and e.status != 429
    )


def _effective_status(payload) -> str | None:
    """Raw status while running; the conclusion once status == completed."""
    if not isinstance(payload, dict):
        return None
    status = payload.get("status")
    conclusion = payload.get("conclusion")
    if status == "completed" and conclusion:
        return conclusion
    return status


async def _emit_progress(ctx, progress: float, total, message: str) -> None:
    """Best-effort progress emit - never breaks polling on transport errors."""
    if ctx is None:
        return
    try:
        await ctx.report_progress(progress=progress, total=total, message=message)
    except Exception:  # noqa: BLE001 - progress is best-effort, never fatal
        # no-report: MCP progress is decoration, logged at debug; must not abort the wait
        _log_wait.debug("report_progress failed", exc_info=True)


async def _emit_log(ctx, level: str, message: str) -> None:
    """Best-effort log emit - never breaks polling on transport errors."""
    if ctx is None:
        return
    try:
        await ctx.log(level=level, message=message)
    except Exception:  # noqa: BLE001 - log notifications are best-effort
        # no-report: MCP log notification is decoration, logged at debug; must not abort the wait
        _log_wait.debug("ctx.log failed", exc_info=True)


def _fetch_run_slim(owner: str, repo: str, run_id: int) -> dict:
    return _slim_workflow_run(
        _get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}")
    )


def _fetch_job_slim(owner: str, repo: str, job_id: int) -> dict:
    return _slim_job(
        _get_client().get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}")
    )


def _fetch_run_jobs_slim(owner: str, repo: str, run_id: int) -> list:
    jobs = _slim_jobs(
        _get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
    )
    return jobs if isinstance(jobs, list) else []


def _job_log_tail(owner: str, repo: str, job_id: int, tail: int) -> dict:
    """Trailing job log with truncation metadata."""
    text = _get_client().get_text(f"/repos/{owner}/{repo}/actions/jobs/{job_id}/logs")
    lines = text.splitlines()
    total = len(lines)
    if tail and tail > 0:
        lines = lines[-tail:]
    return {
        "text": "\n".join(lines),
        "total_lines": total,
        "tail": tail,
        "truncated": total > len(lines),
    }


def _job_failed(job: dict) -> bool:
    return (job.get("conclusion") or job.get("status")) == "failure"


def _fetch_failed_job_logs(owner: str, repo: str, jobs: list, log_tail: int) -> dict:
    """Trace tails per failed job; per-job errors absorbed into the entry."""
    out: dict = {}
    for j in jobs:
        if not isinstance(j, dict) or not _job_failed(j):
            continue
        jid = j.get("id")
        if jid is None:
            continue
        try:
            out[jid] = _job_log_tail(owner, repo, jid, log_tail)
        except Exception as e:  # noqa: BLE001 - surface as content, not abort
            # no-report: the error is returned as this job's log content
            out[jid] = {"error": f"failed to fetch log: {e}"}
    return out


@_op(gitea_read)
async def workflow_runs_wait(
    owner: str,
    repo: str,
    run_id: int,
    timeout: Annotated[float, Field(description="Max seconds to wait for a terminal status.")] = 600.0,
    interval: Annotated[float, Field(description="Seconds between polls. Lower = faster reaction, more API calls.")] = 5.0,
    max_poll_failures: Annotated[int, Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated before the wait fails. Other 4xx errors fail immediately.")] = _MAX_POLL_FAILURES_DEFAULT,
    include_jobs: Annotated[bool, Field(description="When terminated, include the run's jobs in the response.")] = True,
    include_failed_logs: Annotated[bool, Field(description="When include_jobs is true, also attach the trailing log of every failed job.")] = True,
    log_tail: Annotated[int, Field(description="Number of trailing log lines to attach per failed job.")] = 100,
    ctx=None,
):
    """Block until a workflow run reaches a terminal status.

    Holds the MCP tool call open for the whole wait (up to `timeout`
    seconds). If the agent should stay free to do other work during a long
    CI run, use `workflow_runs_wait_start` + `workflow_runs_wait_poll
    (max_block=...)` instead - same data, no long-held call.

    Polls the run every `interval` seconds. Status reported is the
    "effective" one: Gitea's `conclusion` once the run completes, the raw
    `status` before that. Terminal: success, failure, cancelled, skipped,
    completed, blocked (blocked = approval gate; it will not change without
    an external approval). Transient poll failures (network errors, 5xx,
    429) are tolerated up to `max_poll_failures` consecutive misses; other
    4xx errors raise immediately. HTTP runs in a worker thread, so
    concurrent tool calls are not stalled by a slow Gitea response.

    Returns a dict:
      run               slim workflow-run payload at the last poll
      status            effective terminal status (or last seen on timeout)
      terminated        True if a terminal status was reached
      timed_out         True if `timeout` expired first
      elapsed_seconds   wall-clock duration of the wait
      polls             number of API calls made (incl. failed)
      poll_failures     present when > 0: count of failed polls
      last_poll_error   present alongside poll_failures: last failure text
      jobs              list (when include_jobs=True) of slim jobs
      failed_logs       dict[job_id, log] (when include_failed_logs=True)
      enrichment_error  present if the post-wait jobs/log fetch failed
    """
    _blocking_wait_validate(timeout, interval, max_poll_failures, log_tail)

    start = time.monotonic()
    previous_status: str | None = None
    run: dict = {}
    status: str | None = None
    polls = 0
    poll_failures = 0
    consecutive_failures = 0
    last_poll_error: str | None = None
    terminated = False

    while True:
        elapsed = time.monotonic() - start
        try:
            run = await asyncio.to_thread(_fetch_run_slim, owner, repo, run_id)
        except Exception as e:
            # no-report: budgeted transient retry; fatal or budget-exhausted re-raises below
            polls += 1
            poll_failures += 1
            consecutive_failures += 1
            last_poll_error = str(e)
            if _poll_error_is_fatal(e) or consecutive_failures >= max_poll_failures:
                raise
            await _emit_log(
                ctx, "warning",
                f"run #{run_id}: poll failed "
                f"({consecutive_failures}/{max_poll_failures} consecutive), retrying: {e}",
            )
            if elapsed + interval >= timeout:
                break
            await asyncio.sleep(interval)
            continue
        polls += 1
        consecutive_failures = 0
        status = _effective_status(run)

        if status != previous_status:
            await _emit_progress(
                ctx, progress=elapsed, total=timeout,
                message=f"run #{run_id} status: {status}",
            )
            if previous_status is None:
                await _emit_log(
                    ctx, "info", f"run #{run_id}: starting wait (status={status})",
                )
            else:
                await _emit_log(
                    ctx, "info", f"run #{run_id}: {previous_status} -> {status}",
                )
            previous_status = status

        if status in _WAIT_TERMINAL:
            terminated = True
            break

        if elapsed + interval >= timeout:
            break

        await asyncio.sleep(interval)

    elapsed_final = time.monotonic() - start
    result = _wait_result(
        "run", run, status, terminated, elapsed_final,
        polls, poll_failures, last_poll_error,
    )
    await _emit_wait_summary(
        ctx, f"run #{run_id}", status, terminated, timeout, polls, elapsed_final,
    )

    if include_jobs:
        # A blip here must not discard a wait that already completed -
        # absorb and report instead of raising away minutes of progress.
        try:
            jobs = await asyncio.to_thread(_fetch_run_jobs_slim, owner, repo, run_id)
        except Exception as e:  # noqa: BLE001 - surface as content, not abort
            # no-report: returned to the caller as enrichment_error on a finished wait
            result["enrichment_error"] = f"failed to fetch jobs: {e}"
            jobs = None
        if jobs is not None:
            result["jobs"] = jobs
            if include_failed_logs:
                failed_logs = await asyncio.to_thread(
                    _fetch_failed_job_logs, owner, repo, jobs, log_tail
                )
                result["failed_logs"] = failed_logs
                if failed_logs:
                    await _emit_log(
                        ctx, "error",
                        f"run #{run_id}: {len(failed_logs)} failed job(s); "
                        f"trailing log attached (tail={log_tail})",
                    )

    return result


@_op(gitea_read)
async def workflow_jobs_wait(
    owner: str,
    repo: str,
    job_id: int,
    timeout: Annotated[float, Field(description="Max seconds to wait for a terminal status.")] = 600.0,
    interval: Annotated[float, Field(description="Seconds between polls. Lower = faster reaction, more API calls.")] = 5.0,
    max_poll_failures: Annotated[int, Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated before the wait fails. Other 4xx errors fail immediately.")] = _MAX_POLL_FAILURES_DEFAULT,
    include_log: Annotated[bool, Field(description="Include the job's trailing log in the response when terminated.")] = True,
    log_tail: Annotated[int, Field(description="Number of trailing log lines to attach (used when include_log is true).")] = 100,
    ctx=None,
):
    """Block until a workflow job reaches a terminal status.

    Holds the MCP tool call open for the whole wait (up to `timeout`
    seconds). If the agent should stay free to do other work, use
    `workflow_jobs_wait_start` + `workflow_jobs_wait_poll(max_block=...)`.

    Same semantics as workflow_runs_wait (effective status, terminal set,
    transient-failure budget); see its docstring. Returns a dict with `job`
    instead of `run` and, when include_log=True, a structured `log`
    ({text, total_lines, tail, truncated}).
    """
    _blocking_wait_validate(timeout, interval, max_poll_failures, log_tail)

    start = time.monotonic()
    previous_status: str | None = None
    job: dict = {}
    status: str | None = None
    polls = 0
    poll_failures = 0
    consecutive_failures = 0
    last_poll_error: str | None = None
    terminated = False

    while True:
        elapsed = time.monotonic() - start
        try:
            job = await asyncio.to_thread(_fetch_job_slim, owner, repo, job_id)
        except Exception as e:
            # no-report: budgeted transient retry; fatal or budget-exhausted re-raises below
            polls += 1
            poll_failures += 1
            consecutive_failures += 1
            last_poll_error = str(e)
            if _poll_error_is_fatal(e) or consecutive_failures >= max_poll_failures:
                raise
            await _emit_log(
                ctx, "warning",
                f"job #{job_id}: poll failed "
                f"({consecutive_failures}/{max_poll_failures} consecutive), retrying: {e}",
            )
            if elapsed + interval >= timeout:
                break
            await asyncio.sleep(interval)
            continue
        polls += 1
        consecutive_failures = 0
        status = _effective_status(job)

        if status != previous_status:
            await _emit_progress(
                ctx, progress=elapsed, total=timeout,
                message=f"job #{job_id} status: {status}",
            )
            if previous_status is None:
                await _emit_log(
                    ctx, "info", f"job #{job_id}: starting wait (status={status})",
                )
            else:
                await _emit_log(
                    ctx, "info", f"job #{job_id}: {previous_status} -> {status}",
                )
            previous_status = status

        if status in _WAIT_TERMINAL:
            terminated = True
            break

        if elapsed + interval >= timeout:
            break

        await asyncio.sleep(interval)

    elapsed_final = time.monotonic() - start
    result = _wait_result(
        "job", job, status, terminated, elapsed_final,
        polls, poll_failures, last_poll_error,
    )
    await _emit_wait_summary(
        ctx, f"job #{job_id}", status, terminated, timeout, polls, elapsed_final,
    )

    if include_log:
        try:
            result["log"] = await asyncio.to_thread(
                _job_log_tail, owner, repo, job_id, log_tail
            )
        except Exception as e:  # noqa: BLE001 - surface as content, not abort
            # no-report: the error is returned as the log content of a finished wait
            result["log"] = {"error": f"failed to fetch log: {e}"}

    return result


# ── Non-blocking wait tools (start / poll / cancel) ──────────────────────────
#
# start returns wait_id immediately (background task), poll reads the
# snapshot (max_block waits on an event), cancel stops the polling task
# only. Each wait is also an MCP resource at gitea://waits/{wait_id}.


async def _do_run_poll(handle: _WaitHandle) -> bool:
    """One run poll; True if terminal. HTTP in a worker thread, handle
    mutated back on the loop (single-writer)."""
    payload = await asyncio.to_thread(
        _fetch_run_slim, handle.owner, handle.repo, handle.target_id
    )
    handle.polls += 1
    handle.last_payload = payload
    handle.record_transition(_effective_status(payload))
    return handle.status in _WAIT_TERMINAL


async def _do_job_poll(handle: _WaitHandle) -> bool:
    """One job poll. Updates handle, returns True if terminal."""
    payload = await asyncio.to_thread(
        _fetch_job_slim, handle.owner, handle.repo, handle.target_id
    )
    handle.polls += 1
    handle.last_payload = payload
    handle.record_transition(_effective_status(payload))
    return handle.status in _WAIT_TERMINAL


async def _enrich_run_final(handle: _WaitHandle) -> None:
    """Attach jobs (+ failed-job logs) to final_extras after terminal."""
    opts = handle.options
    if not opts.get("include_jobs", True):
        return
    jobs = await asyncio.to_thread(
        _fetch_run_jobs_slim, handle.owner, handle.repo, handle.target_id
    )
    handle.final_extras["jobs"] = jobs
    if opts.get("include_failed_logs", True):
        handle.final_extras["failed_logs"] = await asyncio.to_thread(
            _fetch_failed_job_logs,
            handle.owner, handle.repo, jobs, opts.get("log_tail", 100),
        )


async def _enrich_job_final(handle: _WaitHandle) -> None:
    """Attach trailing job log to final_extras when include_log is set."""
    opts = handle.options
    if not opts.get("include_log", True):
        return
    try:
        handle.final_extras["log"] = await asyncio.to_thread(
            _job_log_tail,
            handle.owner, handle.repo, handle.target_id, opts.get("log_tail", 100),
        )
    except Exception as e:  # noqa: BLE001 - surface as content, not abort
        # no-report: the error is returned as the log content of the final snapshot
        handle.final_extras["log"] = {"error": f"failed to fetch log: {e}"}


async def _wait_loop(handle: _WaitHandle, do_poll, enrich_final) -> None:
    """Shared background loop: budgeted transient failures, max_lifetime
    cap, enrichment once terminal."""
    interval = handle.options["interval"]
    max_failures = handle.options.get("max_poll_failures", _MAX_POLL_FAILURES_DEFAULT)
    max_lifetime = handle.options.get("max_lifetime", _MAX_LIFETIME_DEFAULT)
    consecutive_failures = 0
    try:
        while True:
            await asyncio.sleep(interval)
            if max_lifetime > 0 and (time.time() - handle.started_at) >= max_lifetime:
                handle.mark_timed_out(
                    f"exceeded max_lifetime {max_lifetime:g}s without reaching "
                    f"a terminal status (last status={handle.status})"
                )
                return
            try:
                terminal = await do_poll(handle)
            except Exception as e:  # noqa: BLE001 - classified below
                # no-report: budgeted transient retry; record_poll_failure puts it in the snapshot
                consecutive_failures += 1
                handle.record_poll_failure(str(e))
                if _poll_error_is_fatal(e) or consecutive_failures >= max_failures:
                    suffix = (
                        f" ({consecutive_failures} consecutive failures)"
                        if consecutive_failures > 1 else ""
                    )
                    handle.mark_terminated(error=f"poll failed: {e}{suffix}")
                    return
                continue
            consecutive_failures = 0
            if terminal:
                try:
                    await enrich_final(handle)
                except Exception as e:  # noqa: BLE001 - enrichment is best-effort
                    # no-report: recorded in the snapshot as enrichment_error
                    handle.final_extras["enrichment_error"] = str(e)
                handle.mark_terminated()
                return
    except asyncio.CancelledError:
        # no-report: our own cancel path, re-raised below after recording it
        handle.mark_terminated(error="cancelled")
        raise


async def _wait_start_snapshot(handle: _WaitHandle, do_poll, enrich_final, loop_fn):
    """First poll inline, then either finish or spawn the background loop.

    An already-terminal target never gets a task, so `wait_start` on a finished
    run answers with the full enriched snapshot straight away.
    """
    try:
        terminal = await do_poll(handle)
    except Exception as e:  # noqa: BLE001 - reported via snapshot
        # no-report: reported to the caller in snapshot["error"] instead of raising
        handle.mark_terminated(error=f"initial poll failed: {e}")
        return handle.snapshot()

    if terminal:
        try:
            await enrich_final(handle)
        except Exception as e:  # noqa: BLE001 - enrichment is best-effort
            # no-report: recorded in the snapshot as enrichment_error
            handle.final_extras["enrichment_error"] = str(e)
        handle.mark_terminated()
        return handle.snapshot()

    handle.task = asyncio.create_task(loop_fn(handle))
    return handle.snapshot()


async def _await_terminal_or_timeout(handle: _WaitHandle, max_block: float) -> dict:
    """Snapshot, optionally after blocking up to `max_block` for terminal.

    `asyncio.wait` rather than `wait_for` so exhausting `max_block` is a normal
    return with timed_out=True instead of an exception on the read path.
    """
    if max_block > 0 and not handle.done_event.is_set():
        waiter = asyncio.ensure_future(handle.done_event.wait())
        done, pending = await asyncio.wait({waiter}, timeout=max_block)
        for task in pending:
            task.cancel()
        if not done:
            snap = handle.snapshot()
            snap["timed_out"] = True
            return snap
    return handle.snapshot()


async def _run_loop(handle: _WaitHandle) -> None:
    await _wait_loop(handle, _do_run_poll, _enrich_run_final)


async def _job_loop(handle: _WaitHandle) -> None:
    await _wait_loop(handle, _do_job_poll, _enrich_job_final)


async def _cancel_handle(handle: _WaitHandle) -> None:
    """Cancel the polling task and make sure the handle ends up terminal.

    The loop's own CancelledError handler normally records the cancel, but a
    cancel can land before its first await; and the task may already be dying
    of a real error, which must be reported as that error rather than being
    relabelled "cancelled".
    """
    task = handle.task
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            # no-report: the expected outcome of the cancel we just issued
            pass
        except Exception as e:  # noqa: BLE001 - the task died of its own error
            # no-report: recorded on the handle so the snapshot names the real failure
            if not handle.done_event.is_set():
                handle.mark_terminated(error=f"wait task failed: {e}")
            return
    if not handle.done_event.is_set():
        handle.mark_terminated(error="cancelled")


def _require_handle(wait_id: str, expected_kind: str) -> _WaitHandle:
    handle = _WAIT_REGISTRY.get(wait_id)
    if handle is None:
        raise ValueError(
            f"Unknown wait_id: {wait_id!r}. Use WaitsList to enumerate "
            "active or recently-finished waits."
        )
    if handle.kind != expected_kind:
        raise ValueError(
            f"wait_id {wait_id!r} is a {handle.kind} wait, not {expected_kind}. "
            f"Use the matching *_wait_poll / *_wait_cancel operation."
        )
    return handle


def _poll_options_validate(interval, max_poll_failures, log_tail):
    if interval <= 0:
        raise ValueError(f"interval must be > 0, got {interval}")
    if max_poll_failures < 1:
        raise ValueError(f"max_poll_failures must be >= 1, got {max_poll_failures}")
    if log_tail < 0:
        raise ValueError(f"log_tail must be >= 0, got {log_tail}")


def _start_options_validate(interval, max_poll_failures, max_lifetime, log_tail):
    _poll_options_validate(interval, max_poll_failures, log_tail)
    if max_lifetime < 0:
        raise ValueError(f"max_lifetime must be >= 0, got {max_lifetime}")


def _blocking_wait_validate(timeout, interval, max_poll_failures, log_tail):
    if timeout <= 0:
        raise ValueError(f"timeout must be > 0, got {timeout}")
    _poll_options_validate(interval, max_poll_failures, log_tail)


@_op(gitea_read)
async def workflow_runs_wait_start(
    owner: str,
    repo: str,
    run_id: int,
    interval: Annotated[float, Field(description="Seconds between background polls. Lower = faster reaction, more API calls.")] = 5.0,
    max_poll_failures: Annotated[int, Field(description="Consecutive transient poll failures (network errors, 5xx, 429) tolerated by the background loop before the wait errors out. Other 4xx errors fail immediately.")] = _MAX_POLL_FAILURES_DEFAULT,
    max_lifetime: Annotated[float, Field(description="Hard cap in seconds on the background wait's total runtime; when exceeded the wait stops with timed_out=True. 0 disables the cap.")] = _MAX_LIFETIME_DEFAULT,
    include_jobs: Annotated[bool, Field(description="When the run terminates, attach the slim jobs list to the final snapshot.")] = True,
    include_failed_logs: Annotated[bool, Field(description="When include_jobs is true, also attach trailing logs of failed jobs.")] = True,
    log_tail: Annotated[int, Field(description="Number of trailing log lines to attach per failed job.")] = 100,
):
    """Start a non-blocking wait for a workflow run to reach a terminal status.

    Returns a `wait_id` + snapshot immediately so the agent stays
    unblocked. The first poll runs inline so the snapshot carries real
    status (and fails fast on a wrong ID / no access); if the run is
    already terminal, no background task is spawned and the snapshot
    includes full enrichment.

    Observe with `workflow_runs_wait_poll(wait_id, max_block=...)` or read
    the resource at `gitea://waits/{wait_id}`. Stop with
    `workflow_runs_wait_cancel(wait_id)` - that stops the polling task,
    NOT the workflow run.

    Returns the same snapshot shape as `workflow_runs_wait_poll`.
    """
    _start_options_validate(interval, max_poll_failures, max_lifetime, log_tail)
    _WAIT_REGISTRY.reap_old()

    options = {
        "interval": interval,
        "max_poll_failures": max_poll_failures,
        "max_lifetime": max_lifetime,
        "include_jobs": include_jobs,
        "include_failed_logs": include_failed_logs,
        "log_tail": log_tail,
    }
    handle = _WAIT_REGISTRY.new_handle("run", owner, repo, run_id, options)
    return await _wait_start_snapshot(
        handle, _do_run_poll, _enrich_run_final, _run_loop,
    )


@_op(gitea_read)
async def workflow_runs_wait_poll(
    wait_id: str,
    max_block: Annotated[float, Field(description="If > 0 and the wait is still in flight, block up to this many seconds waiting for the terminal event. 0 (default) returns the current snapshot immediately.")] = 0.0,
):
    """Read the current snapshot of a workflow-run wait.

    With `max_block=0` (default) this is non-blocking. With `max_block > 0`
    it waits up to that many seconds for the wait to terminate, using an
    asyncio.Event under the hood so the caller doesn't spin.

    Snapshot fields: wait_id, resource_uri, kind, owner, repo, run_id,
    status (effective: conclusion once completed), terminated, timed_out
    (True if this poll's max_block elapsed before terminal, or the wait
    gave up after max_lifetime - then `error` is set too), polls,
    poll_failures + last_poll_error (when failures happened), transitions,
    run (latest slim payload), started_at / ended_at / elapsed_seconds,
    jobs / failed_logs (only when terminated), error.
    """
    if max_block < 0:
        raise ValueError(f"max_block must be >= 0, got {max_block}")
    handle = _require_handle(wait_id, expected_kind="run")
    return await _await_terminal_or_timeout(handle, max_block)


@_op(gitea_read)
async def workflow_runs_wait_cancel(wait_id: str):
    """Cancel a workflow-run wait. The snapshot remains readable; error="cancelled".

    Idempotent on an already-terminal wait. Cancellation only stops the
    background polling task; it does NOT cancel the workflow run itself.
    """
    handle = _require_handle(wait_id, expected_kind="run")
    if handle.done_event.is_set():
        return handle.snapshot()
    await _cancel_handle(handle)
    return handle.snapshot()


@_op(gitea_read)
async def workflow_jobs_wait_start(
    owner: str,
    repo: str,
    job_id: int,
    interval: Annotated[float, Field(description="Seconds between background polls.")] = 5.0,
    max_poll_failures: Annotated[int, Field(description="Consecutive transient poll failures tolerated by the background loop before the wait errors out. Other 4xx errors fail immediately.")] = _MAX_POLL_FAILURES_DEFAULT,
    max_lifetime: Annotated[float, Field(description="Hard cap in seconds on the background wait's total runtime; when exceeded the wait stops with timed_out=True. 0 disables the cap.")] = _MAX_LIFETIME_DEFAULT,
    include_log: Annotated[bool, Field(description="On termination, attach the job's trailing log to the final snapshot.")] = True,
    log_tail: Annotated[int, Field(description="Number of trailing log lines to attach.")] = 100,
):
    """Start a non-blocking wait for a workflow job to reach a terminal status.

    Returns a handle immediately. See `workflow_runs_wait_start` for the
    same pattern (including `max_poll_failures` / `max_lifetime`); observe
    with `workflow_jobs_wait_poll(wait_id, max_block=...)` or read the
    resource at `gitea://waits/{wait_id}`.
    """
    _start_options_validate(interval, max_poll_failures, max_lifetime, log_tail)
    _WAIT_REGISTRY.reap_old()

    options = {
        "interval": interval,
        "max_poll_failures": max_poll_failures,
        "max_lifetime": max_lifetime,
        "include_log": include_log,
        "log_tail": log_tail,
    }
    handle = _WAIT_REGISTRY.new_handle("job", owner, repo, job_id, options)
    return await _wait_start_snapshot(
        handle, _do_job_poll, _enrich_job_final, _job_loop,
    )


@_op(gitea_read)
async def workflow_jobs_wait_poll(
    wait_id: str,
    max_block: Annotated[float, Field(description="If > 0, block up to this many seconds waiting for terminal. 0 returns the current snapshot immediately.")] = 0.0,
):
    """Read the current snapshot of a workflow-job wait. Mirrors `workflow_runs_wait_poll`."""
    if max_block < 0:
        raise ValueError(f"max_block must be >= 0, got {max_block}")
    handle = _require_handle(wait_id, expected_kind="job")
    return await _await_terminal_or_timeout(handle, max_block)


@_op(gitea_read)
async def workflow_jobs_wait_cancel(wait_id: str):
    """Cancel a workflow-job wait. Mirrors `workflow_runs_wait_cancel`."""
    handle = _require_handle(wait_id, expected_kind="job")
    if handle.done_event.is_set():
        return handle.snapshot()
    await _cancel_handle(handle)
    return handle.snapshot()


@_op(gitea_read)
def waits_list(
    kind: Annotated[str | None, Field(description="Filter by kind: 'run' or 'job'. Omit to list both.")] = None,
    terminated: Annotated[bool | None, Field(description="Filter by termination state. Omit to list all.")] = None,
):
    """List active and recently-terminal waits known to this server.

    Returns compact dicts (no payload, no jobs, no logs) so the agent can
    recover after losing a wait_id: wait_id, resource_uri, kind, owner,
    repo, target_id, status, terminated, timed_out, polls,
    elapsed_seconds, started_at, ended_at, error.

    The registry has a TTL (1 hour after termination); after that entries
    are reaped and no longer listed.
    """
    if kind is not None and kind not in ("run", "job"):
        raise ValueError(f"kind must be 'run' or 'job' or None, got {kind!r}")
    out: list = []
    for handle in _WAIT_REGISTRY.all_handles():
        if kind is not None and handle.kind != kind:
            continue
        if terminated is not None and handle.terminated != terminated:
            continue
        target_key = "run_id" if handle.kind == "run" else "job_id"
        out.append({
            "wait_id": handle.wait_id,
            "resource_uri": f"gitea://waits/{handle.wait_id}",
            "kind": handle.kind,
            "owner": handle.owner,
            "repo": handle.repo,
            target_key: handle.target_id,
            "status": handle.status,
            "terminated": handle.terminated,
            "timed_out": handle.timed_out,
            "polls": handle.polls,
            "elapsed_seconds": handle.elapsed_seconds,
            "started_at": handle.started_at,
            "ended_at": handle.ended_at,
            "error": handle.error,
        })
    return out
