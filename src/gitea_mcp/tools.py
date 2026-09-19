"""Gitea tool operations. All public functions are auto-registered as MCP tools."""

import asyncio
import base64
import json
import logging
import re
import time
from contextvars import ContextVar
from importlib.metadata import version as _pkg_version
from typing import Annotated, Literal

import httpx
from pydantic import Field

from .client import GiteaClient, GiteaError
from .config import get_settings
from .prepare import (
    _body,
    _commit_author,
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

# Set by a host that serves several Gitea instances from one process; the
# module singleton is the single-instance path.
client_var: ContextVar[GiteaClient | None] = ContextVar("gitea_client", default=None)
_client: GiteaClient | None = None


def _get_client() -> GiteaClient:
    global _client
    if (bound := client_var.get()) is not None:
        return bound
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

@_op(gitea_read)
def get_current_access_token():
    """Get metadata for the access token this server is authenticating with.

    Returns id, name, scopes, created_at, last_used_at and the owning user —
    the token secret itself is never echoed back. Use it to find out what the
    configured GITEA_TOKEN is actually allowed to do before attempting a
    write, instead of discovering the missing scope as a 403.
    """
    return _ok(_get_client().get("/token"))

@_op(gitea_delete)
def delete_current_access_token():
    """Revoke the access token this server is authenticating with.

    Irreversible and self-destructive: every subsequent call from this server
    fails with 401 until GITEA_TOKEN is replaced with a fresh token. Use
    delete_user_token-style ops to revoke someone else's token instead.
    """
    return _ok(_get_client().delete("/token"))

@_op(gitea_read)
def get_api_settings():
    """Get the instance's global API settings.

    Returns the paging limits this instance enforces (default_paging_num,
    max_response_items, default_git_trees_per_page, default_max_blob_size,
    default_max_response_size) — i.e. the real ceiling on any `limit` a list
    op can ask for.
    """
    return _ok(_get_client().get("/settings/api"))

@_op(gitea_read)
def get_attachment_settings():
    """Get the instance's global attachment settings.

    Returns whether attachments are enabled at all, plus allowed_types (a
    comma-separated MIME/extension list), max_size (MiB) and max_files —
    check these before uploading issue or release attachments.
    """
    return _ok(_get_client().get("/settings/attachment"))

@_op(gitea_read)
def get_repository_settings():
    """Get the instance's global repository settings.

    Returns which repo features the admin has switched off instance-wide:
    http_git_disabled, lfs_disabled, migrations_disabled, mirrors_disabled,
    stars_disabled, time_tracking_disabled. A feature disabled here fails for
    every repo regardless of per-repo configuration.
    """
    return _ok(_get_client().get("/settings/repository"))

@_op(gitea_read)
def get_ui_settings():
    """Get the instance's global UI settings.

    Returns default_theme, custom_emojis, and allowed_reactions — the latter
    is the exact set of reaction strings create_issue_reaction and friends
    will accept on this instance.
    """
    return _ok(_get_client().get("/settings/ui"))

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

@_op(gitea_read)
def get_ssh_key(
    key_id: Annotated[int, Field(description="Numeric key ID (int64) from the `id` field of list_ssh_keys — NOT the title and NOT the fingerprint.")],
):
    """Get one of the current user's SSH keys by ID."""
    return _ok(_get_client().get(f"/user/keys/{key_id}"))

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
def list_user_ssh_keys(
    username: Annotated[str, Field(description="USERNAME of the user whose public keys to list (NOT a user ID, NOT a display name).")],
    fingerprint: Annotated[str | None, Field(description="Optional filter: return only the key with this fingerprint, e.g. 'SHA256:qu9M...'. Omit to list every public key the user has.")] = None,
):
    """List another user's public SSH keys.

    Public keys are world-readable in Gitea; this works for any username,
    not just the authenticated user."""
    params = _body(locals(), exclude=("username",))
    return _ok(_get_client().paginate(f"/users/{username}/keys", params=params or None))

@_op(gitea_read)
def list_gpg_keys():
    """List the current user's GPG keys."""
    return _ok(_get_client().paginate("/user/gpg_keys"))

@_op(gitea_read)
def get_gpg_key(
    key_id: Annotated[int, Field(description="Numeric key ID (int64) from the `id` field of list_gpg_keys — NOT the hex `key_id` field and NOT the fingerprint.")],
):
    """Get one of the current user's GPG keys by ID."""
    return _ok(_get_client().get(f"/user/gpg_keys/{key_id}"))

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

@_op(gitea_read)
def list_user_gpg_keys(
    username: Annotated[str, Field(description="USERNAME of the user whose GPG keys to list (NOT a user ID, NOT a display name).")],
):
    """List another user's GPG keys.

    GPG keys are world-readable in Gitea; this works for any username,
    not just the authenticated user."""
    return _ok(_get_client().paginate(f"/users/{username}/gpg_keys"))

@_op(gitea_read)
def get_gpg_key_token():
    """Get the one-time challenge token used to prove GPG key ownership.

    Returns a plain-text token string (not JSON), tied to the authenticated
    user. Verification flow:

      1. Call this op to get the token, e.g. 'gitea-abc123...'.
      2. Sign that exact token text with the secret half of the GPG key,
         producing an ASCII-armored *detached* signature:
         `echo -n "<token>" | gpg --armor --detach-sign -u <KEYID>`
         (no trailing newline — sign the token bytes verbatim).
      3. Pass the key's hex `key_id` and the armored signature block to
         verify_gpg_key.

    The key must already be registered via create_gpg_key. Until verified,
    Gitea reports commits signed with it as unverified."""
    return _get_client().get_text("/user/gpg_key_token")

@_op(gitea_write)
def verify_gpg_key(
    key_id: Annotated[str, Field(description="Hex GPG key ID from the `key_id` field of list_gpg_keys (e.g. '3E5A8B1C9D4F2601') — NOT the numeric `id` and NOT the fingerprint.")],
    armored_signature: Annotated[str, Field(description="ASCII-armored *detached* OpenPGP signature over the exact token text returned by get_gpg_key_token. Starts with '-----BEGIN PGP SIGNATURE-----' and ends with '-----END PGP SIGNATURE-----', newlines included. Produce with: echo -n \"<token>\" | gpg --armor --detach-sign -u <key_id>.")],
):
    """Verify a GPG key by submitting a signature over the challenge token.

    Call get_gpg_key_token first, sign that token with the key's secret
    half, then pass the key's hex key_id plus the armored signature here.
    On success Gitea returns the key with `verified: true` and starts
    marking commits signed by it as verified. A signature over anything
    other than the current token returns 422."""
    return _call("POST", "/user/gpg_key_verify", locals())

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

@_op(gitea_execute)
def migrate_repo(
    clone_addr: Annotated[str, Field(description="URL of the SOURCE repository to import from, e.g. 'https://github.com/owner/name.git', 'https://gitlab.com/owner/name' or 'git@host:owner/name.git'. This is the remote being read — not the repo being created here.")],
    repo_name: Annotated[str, Field(description="Name of the NEW repository created on this Gitea instance — the slug only (e.g. 'name'), not 'owner/name'.")],
    repo_owner: Annotated[str | None, Field(description="Username or organization login that will own the new repo. Defaults to the authenticated user.")] = None,
    service: Annotated[Literal["git", "github", "gitea", "gitlab", "gogs", "onedev", "gitbucket", "codebase", "codecommit"] | None, Field(description="Type of the source service. 'git' (the default) is a plain git clone: ONLY commits/branches/tags are copied and the issues/labels/milestones/pull_requests/releases flags below are ignored. Any other value makes Gitea talk to that service's API, which is what enables importing those non-git units.")] = None,
    auth_token: Annotated[str | None, Field(description="Personal access token for the SOURCE service, used for its API and for private source repos. Required whenever service is not 'git' and you ask for issues/labels/milestones/pull_requests/releases. Use this instead of auth_username/auth_password when the source supports tokens.")] = None,
    auth_username: Annotated[str | None, Field(description="Username for HTTP basic auth against the source repo — use with auth_password when the source has no token auth.")] = None,
    auth_password: Annotated[str | None, Field(description="Password or app-password paired with auth_username for HTTP basic auth against the source repo.")] = None,
    aws_access_key_id: Annotated[str | None, Field(description="AWS access key id — only for service='codecommit'.")] = None,
    aws_secret_access_key: Annotated[str | None, Field(description="AWS secret access key — only for service='codecommit'.")] = None,
    mirror: Annotated[bool | None, Field(description="True = keep the new repo as a PULL MIRROR that keeps re-fetching from clone_addr; False/omitted = one-off import with no ongoing link to the source.")] = None,
    mirror_interval: Annotated[str | None, Field(description="Go duration between automatic mirror syncs, e.g. '8h0m0s'. Only meaningful with mirror=True; '0' disables scheduled syncing (sync manually with SyncRepoMirror).")] = None,
    lfs: Annotated[bool | None, Field(description="True = also fetch Git LFS objects from the source.")] = None,
    lfs_endpoint: Annotated[str | None, Field(description="LFS server URL to fetch objects from, when the source's LFS lives elsewhere than clone_addr.")] = None,
    private: Annotated[bool | None, Field(description="True = create the migrated repo as private.")] = None,
    description: str | None = None,
    issues: Annotated[bool | None, Field(description="True = also import the source repo's ISSUES (with their comments). Needs service != 'git' and credentials that can read issues there; silently ignored for service='git'.")] = None,
    labels: Annotated[bool | None, Field(description="True = also import the source repo's LABELS. Needs service != 'git'; ignored otherwise.")] = None,
    milestones: Annotated[bool | None, Field(description="True = also import the source repo's MILESTONES. Needs service != 'git'; ignored otherwise.")] = None,
    pull_requests: Annotated[bool | None, Field(description="True = also import the source repo's PULL REQUESTS (with their review comments). Needs service != 'git'; ignored otherwise.")] = None,
    releases: Annotated[bool | None, Field(description="True = also import the source repo's RELEASES and their uploaded attachments. Needs service != 'git'; ignored otherwise.")] = None,
    wiki: Annotated[bool | None, Field(description="True = also import the source repo's WIKI (a separate git repository on the source). Needs the source to expose a wiki; ignored for plain 'git' clones without one.")] = None,
):
    """Migrate (import) a remote repository into this Gitea instance.

    Starts a real import: Gitea clones `clone_addr` and, for a non-'git'
    `service`, calls that service's API for the units enabled below. For big
    repos the request can take a long time and the repo appears part-filled
    while it runs. The response is the newly created Repository object."""
    private = _enforce_private(private)
    return _call("POST", "/repos/migrate", locals())

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

@_op(gitea_read)
def list_repo_stargazers(owner: str, repo: str):
    """List the users who starred a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/stargazers"))

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
def check_repo_subscription(owner: str, repo: str):
    """Check whether the CURRENT user is watching a repository.

    Returns the watch info (subscribed, ignored, reason, created_at) when the
    user watches it; Gitea answers 404 (raised as an error) when they do not.
    This is the per-repo check — ListMySubscriptions lists every watched repo."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/subscription"))

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

@_op(gitea_write)
def update_repo_avatar(
    owner: str,
    repo: str,
    image: Annotated[str, Field(description="Base64-encoded image file content (PNG/JPEG/GIF), NOT a URL and NOT a file path. Sent as a JSON body field — this endpoint is not a multipart upload.")],
):
    """Set a repository's avatar from a base64-encoded image."""
    return _call("POST", "/repos/{owner}/{repo}/avatar", locals())

@_op(gitea_delete)
def delete_repo_avatar(owner: str, repo: str):
    """Clear a repository's avatar, reverting it to the generated default."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/avatar"))

@_op(gitea_read)
def get_repo_licenses(owner: str, repo: str):
    """List the license names Gitea detected in a repository's files.

    Returns a list of SPDX-ish license names (e.g. ['MIT']) detected from the
    repo's LICENSE files — not the instance's license templates (ListLicenses)."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/licenses"))

@_op(gitea_read)
def check_new_issue_pins_allowed(owner: str, repo: str):
    """Check whether new issue/PR pins are still allowed in a repository.

    Returns {'issues': bool, 'pull_requests': bool} — False means the repo hit
    Gitea's pin limit and PinIssue would fail for that kind."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/new_pin_allowed"))

@_op(gitea_read)
def get_repo_signing_key(owner: str, repo: str):
    """Get the GPG public key Gitea signs this repository's commits with.

    Returns ASCII-armored key text ('-----BEGIN PGP PUBLIC KEY BLOCK-----'),
    or an empty string when the instance has no GPG signing key for the repo."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/signing-key.gpg")

@_op(gitea_read)
def get_repo_signing_key_ssh(owner: str, repo: str):
    """Get the SSH public key Gitea signs this repository's commits with.

    Returns one OpenSSH public-key line (e.g. 'ssh-ed25519 AAAA... gitea'), or
    an empty string when the instance signs with GPG instead of SSH."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/signing-key.pub")

@_op(gitea_read)
def list_repo_push_mirrors(owner: str, repo: str):
    """List a repository's push mirrors — the remotes Gitea pushes this repo to.

    Each entry carries remote_name (the handle other push-mirror ops take),
    remote_address, interval, sync_on_commit, last_update and last_error."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/push_mirrors"))

@_op(gitea_read)
def get_repo_push_mirror(
    owner: str,
    repo: str,
    name: Annotated[str, Field(description="Remote name of the push mirror — the `remote_name` field from ListRepoPushMirrors (a git remote handle like 'mirror-1'), NOT the remote URL.")],
):
    """Get one push mirror of a repository by its remote name."""
    return _call("GET", "/repos/{owner}/{repo}/push_mirrors/{name}", locals())

@_op(gitea_write)
def add_repo_push_mirror(
    owner: str,
    repo: str,
    remote_address: Annotated[str, Field(description="URL of the DESTINATION repository Gitea should push to, e.g. 'https://github.com/owner/name.git'. Gitea derives the mirror's remote_name itself.")],
    remote_username: Annotated[str | None, Field(description="Username for authenticating against the destination remote.")] = None,
    remote_password: Annotated[str | None, Field(description="Password or access token paired with remote_username for the destination remote.")] = None,
    interval: Annotated[str | None, Field(description="Go duration between automatic pushes, e.g. '8h0m0s'. '0' disables scheduled pushing, leaving SyncRepoPushMirrors / sync_on_commit as the triggers.")] = None,
    sync_on_commit: Annotated[bool | None, Field(description="True = push to the mirror on every new commit, in addition to the `interval` schedule.")] = None,
):
    """Add a push mirror: a remote this repository is continuously pushed to."""
    return _call("POST", "/repos/{owner}/{repo}/push_mirrors", locals())

@_op(gitea_delete)
def delete_repo_push_mirror(
    owner: str,
    repo: str,
    name: Annotated[str, Field(description="Remote name of the push mirror to remove — the `remote_name` field from ListRepoPushMirrors, NOT the remote URL.")],
):
    """Delete a push mirror from a repository by its remote name.

    Removes the mirroring configuration only; the destination repository and
    anything already pushed there are untouched."""
    return _call("DELETE", "/repos/{owner}/{repo}/push_mirrors/{name}", locals())

@_op(gitea_execute)
def sync_repo_push_mirrors(owner: str, repo: str):
    """Trigger an immediate push to ALL of a repository's push mirrors.

    Pushes the current refs out to every configured remote now, instead of
    waiting for each mirror's interval. Check last_update/last_error via
    ListRepoPushMirrors afterwards — the sync runs in the background."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/push_mirrors-sync"))

@_op(gitea_execute)
def sync_repo_mirror(owner: str, repo: str):
    """Trigger an immediate pull-mirror sync: re-fetch this repo from its source.

    Only valid for a repo created as a mirror (MigrateRepo with mirror=True);
    it overwrites local refs with the upstream's. The opposite direction from
    SyncRepoPushMirrors, which pushes this repo out to its push mirrors."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/mirror-sync"))

@_op(gitea_execute)
def merge_upstream(
    owner: str,
    repo: str,
    branch: Annotated[str | None, Field(description="Branch in THIS fork to update, and the same-named branch of the upstream repo to take commits from (e.g. 'main').")] = None,
    ff_only: Annotated[bool | None, Field(description="True = refuse the sync unless the branch can fast-forward, leaving it untouched rather than creating a merge commit.")] = None,
):
    """Sync a fork's branch with the same branch of its upstream repository.

    Writes to the branch: Gitea fast-forwards it, or merges upstream into it
    when ff_only is not set. Returns {'merge_type': ...} saying which happened."""
    return _call("POST", "/repos/{owner}/{repo}/merge-upstream", locals())

@_op(gitea_execute)
def accept_repo_transfer(owner: str, repo: str):
    """Accept a pending transfer of a repository to you or your organization.

    `owner`/`repo` name the repo as it is addressed TODAY (still under the old
    owner); accepting completes TransferRepo and moves it to the new owner."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/transfer/accept"))

@_op(gitea_execute)
def reject_repo_transfer(owner: str, repo: str):
    """Reject a pending transfer of a repository to you or your organization.

    `owner`/`repo` name the repo as it is addressed today; rejecting cancels
    the pending TransferRepo and leaves the repo with its current owner."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/transfer/reject"))

@_op(gitea_read)
def check_repo_assignee(
    owner: str,
    repo: str,
    assignee: Annotated[str, Field(description="USERNAME to test (NOT a user ID / display name).")],
):
    """Check whether a user can be assigned to issues in a repository.

    Returns {'status': 'ok'} when the user is assignable; Gitea answers 404
    (raised as an error) when they are not. ListRepoAssignees returns the
    whole set in one call."""
    return _call("GET", "/repos/{owner}/{repo}/assignees/{assignee}", locals())

def _hook_body(hook_type: str, config: dict, events: list, active: bool) -> dict:
    return {"type": hook_type, "config": config, "events": events, "active": active}


# ── Webhooks ─────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_webhooks(owner: str, repo: str):
    """List a repository's webhooks."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/hooks"))

@_op(gitea_read)
def get_repo_webhook(owner: str, repo: str, hook_id: int):
    """Get one repository webhook by ID."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/hooks/{hook_id}"))

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

# Server-side Git hooks, as opposed to the HTTP webhooks above: scripts the
# repository runs on push. Gitea addresses them by name, not by numeric ID,
# and supports exactly the three names below.
_GitHookName = Annotated[Literal["pre-receive", "update", "post-receive"], Field(description="Name of the server-side Git hook — Gitea supports only 'pre-receive', 'update' and 'post-receive'. This is the hook's identity; it is NOT a numeric ID, and any other value is a 404.")]


@_op(gitea_read)
def list_repo_git_hooks(owner: str, repo: str):
    """List a repository's server-side Git hooks and whether each one is active."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/hooks/git"))

@_op(gitea_read)
def get_repo_git_hook(owner: str, repo: str, hook_name: _GitHookName):
    """Get one server-side Git hook of a repository, including its script content."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/hooks/git/{hook_name}"))

@_op(gitea_write)
def edit_repo_git_hook(
    owner: str,
    repo: str,
    hook_name: _GitHookName,
    content: Annotated[str, Field(description="Full replacement script for the hook, e.g. '#!/bin/sh\\nexit 0'. Written verbatim and run by the server on push; an empty string deactivates the hook.")],
):
    """Set the script content of a repository's server-side Git hook."""
    return _call("PATCH", "/repos/{owner}/{repo}/hooks/git/{hook_name}", locals())

@_op(gitea_delete)
def delete_repo_git_hook(owner: str, repo: str, hook_name: _GitHookName):
    """Delete a repository's server-side Git hook — clears its script and deactivates it."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/hooks/git/{hook_name}"))

# Webhooks owned by the authenticated user's own account, rather than by a
# repository or an organization. Same Hook payloads as the two pairs above.


@_op(gitea_read)
def list_user_webhooks():
    """List the authenticated user's webhooks."""
    return _ok(_get_client().paginate("/user/hooks"))

@_op(gitea_read)
def get_user_webhook(hook_id: int):
    """Get one of the authenticated user's webhooks by ID."""
    return _ok(_get_client().get(f"/user/hooks/{hook_id}"))

@_op(gitea_write)
def create_user_webhook(
    config: _HookConfig,
    events: _HookEvents,
    hook_type: _HookType = "gitea",
    active: bool = True,
):
    """Create a webhook on the authenticated user's account."""
    return _ok(_get_client().post(
        "/user/hooks", json=_hook_body(hook_type, config, events, active),
    ))

@_op(gitea_write)
def edit_user_webhook(
    hook_id: int,
    config: _HookConfigPatch = None,
    events: _HookEventsPatch = None,
    active: bool | None = None,
):
    """Edit one of the authenticated user's webhooks."""
    return _call("PATCH", "/user/hooks/{hook_id}", locals())

@_op(gitea_delete)
def delete_user_webhook(hook_id: int):
    """Delete one of the authenticated user's webhooks."""
    return _ok(_get_client().delete(f"/user/hooks/{hook_id}"))

# ── Org Webhooks ─────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_webhooks(org: str):
    """List webhooks for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/hooks"))

@_op(gitea_read)
def get_org_webhook(org: str, hook_id: int):
    """Get one organization webhook by ID."""
    return _ok(_get_client().get(f"/orgs/{org}/hooks/{hook_id}"))

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
    _commit_author(body, author_name, author_email)
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

@_op(gitea_read)
def list_repo_root_contents(
    owner: str,
    repo: str,
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to list from. Defaults to the repo's default branch.")] = None,
):
    """Get the metadata of all the entries of the repository's root directory."""
    return _call("GET", "/repos/{owner}/{repo}/contents", locals())

@_op(gitea_read)
def get_contents_ext(
    owner: str,
    repo: str,
    filepath: Annotated[str, Field(description="Path of the dir, file, symlink or submodule inside the repo. Empty string or a single dot ('.') = repo root.")] = "",
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
    includes: Annotated[str | None, Field(description="Comma-separated extra fields to fetch beyond metadata: 'file_content', 'lfs_metadata', 'commit_metadata', 'commit_message' (e.g. 'file_content,commit_message'). Omit for metadata only.")] = None,
):
    """The extended "contents" API — file metadata and/or content, or a directory listing.

    A file answers in `file_contents`, a directory in `dir_contents`. File bodies
    are only present when `includes` asks for 'file_content', and Gitea returns
    them base64-encoded (the entry's `encoding` field says so)."""
    return _call("GET", "/repos/{owner}/{repo}/contents-ext/{filepath}", locals())

def _encoded_change_files(files: list[dict]) -> list[dict]:
    """Base64-encode the plaintext `content` of each change_files operation."""
    encoded = []
    for entry in files:
        item = dict(entry)
        if item.get("content") is not None:
            item["content"] = base64.b64encode(str(item["content"]).encode()).decode()
        encoded.append(item)
    return encoded

@_op(gitea_write)
def change_files(
    owner: str,
    repo: str,
    files: Annotated[list[dict], Field(description=(
        "File operations to apply as ONE commit. Each item is a dict with keys: "
        "`operation` (required) — one of 'create', 'update', 'upload', 'rename', 'delete'; "
        "`path` (required) — path of the existing or new file; "
        "`content` — file body as PLAINTEXT for create/update/upload; the tool base64-encodes it, do NOT pre-encode; "
        "`sha` — blob SHA of the file that already exists, required for update/delete (from get_file_content); "
        "`from_path` — the old path, required for 'rename'."
    ))],
    message: Annotated[str | None, Field(description="Git commit message for the whole batch. Gitea generates one if omitted.")] = None,
    branch: Annotated[str | None, Field(description="Branch to commit on (HEAD advances to the new commit). Defaults to the repo's default branch.")] = None,
    new_branch: Annotated[str | None, Field(description="If set, create this new branch from `branch` and commit there (PR-style flow). The base `branch` is left untouched.")] = None,
    author_name: Annotated[str | None, Field(description="Override the git author name for this commit.")] = None,
    author_email: Annotated[str | None, Field(description="Override the git author email for this commit.")] = None,
    signoff: Annotated[bool | None, Field(description="True = append a Signed-off-by trailer by the committer to the commit message.")] = None,
):
    """Modify multiple files in a repository in a single commit. File contents are provided as plain text and will be base64-encoded automatically."""
    body: dict = {"files": _encoded_change_files(files)}
    if message is not None:
        body["message"] = message
    if branch is not None:
        body["branch"] = branch
    if new_branch is not None:
        body["new_branch"] = new_branch
    if signoff is not None:
        body["signoff"] = signoff
    _commit_author(body, author_name, author_email)
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/contents", json=body))

@_op(gitea_execute)
def apply_diff_patch(
    owner: str,
    repo: str,
    content: Annotated[str, Field(description="The patch to apply, as PLAINTEXT unified diff (`git diff` / `git format-patch` output). Sent raw to `git apply` — do NOT base64-encode it.")],
    message: Annotated[str | None, Field(description="Git commit message for the applied patch. Gitea generates one if omitted.")] = None,
    branch: Annotated[str | None, Field(description="Base branch the patch applies to. Defaults to the repo's default branch.")] = None,
    new_branch: Annotated[str | None, Field(description="If set, commit the result on this new branch created from `branch` (PR-style flow).")] = None,
    force_push: Annotated[bool | None, Field(description="True = force-push if `new_branch` already exists.")] = None,
    signoff: Annotated[bool | None, Field(description="True = append a Signed-off-by trailer by the committer to the commit message.")] = None,
):
    """Apply a diff patch to a repository and commit the result. Fails if the patch does not apply cleanly."""
    return _call("POST", "/repos/{owner}/{repo}/diffpatch", locals())

@_op(gitea_read)
def get_editorconfig(
    owner: str,
    repo: str,
    filepath: Annotated[str, Field(description="Repo-relative path of the file whose EditorConfig rules you want (e.g. 'src/main.py').")],
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA whose .editorconfig files are consulted. Defaults to the repo's default branch.")] = None,
):
    """Get the EditorConfig definitions resolved for a file in a repository (indent_style, indent_size, charset, ...)."""
    return _call("GET", "/repos/{owner}/{repo}/editorconfig/{filepath}", locals())

@_op(gitea_read)
def get_files_contents(
    owner: str,
    repo: str,
    files: Annotated[list[str], Field(description="Repo-relative file paths to fetch in one round trip, e.g. ['README.md', 'src/main.py'].")],
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Get the metadata and contents of several requested files at once.

    A read, despite the POST — the path list travels in the request body.
    Entries that could not be retrieved come back null; a file too large for
    the response has `encoding` and `content` null and must be fetched
    singly via its `download_url`. Present bodies are base64-encoded."""
    params = _body(locals(), exclude=("owner", "repo", "files"))
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/file-contents",
            json={"files": files},
            params=params or None,
        )
    )

@_op(gitea_read)
def get_files_contents_query(
    owner: str,
    repo: str,
    files: Annotated[list[str], Field(description="Repo-relative file paths to fetch in one round trip, e.g. ['README.md', 'src/main.py'].")],
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Same batch read as get_files_contents, with the file list JSON-encoded into the query string instead of the body.

    Prefer get_files_contents; this GET variant exists for callers that cannot
    send a body, and a long path list can overflow the URL."""

    params = {"body": json.dumps({"files": files})}
    if ref is not None:
        params["ref"] = ref
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/file-contents", params=params))

@_op(gitea_read)
def get_blob(
    owner: str,
    repo: str,
    sha: Annotated[str, Field(description="Blob SHA — NOT a commit SHA. Take it from get_file_content's `sha`, or a tree entry's `sha`.")],
):
    """Get the blob of a repository. Gitea returns `content` base64-encoded, with `encoding` naming the encoding used."""
    return _call("GET", "/repos/{owner}/{repo}/git/blobs/{sha}", locals())

@_op(gitea_read)
def get_media_file(
    owner: str,
    repo: str,
    filepath: Annotated[str, Field(description="Path of the file to get, optionally prefixed with a ref as '{ref}/{filepath}'. Slashes are part of the path and are passed through as-is.")],
    ref: Annotated[str | None, Field(description="Branch / tag / commit SHA to read from. Defaults to the repo's default branch.")] = None,
):
    """Get a file, or its LFS object, from a repository.

    This endpoint serves application/octet-stream, so the bytes are returned
    BASE64-ENCODED as a string — an MCP result cannot carry raw bytes. Decode
    before use. For text files prefer get_raw_file, which returns plain text."""
    params = _body(locals(), exclude=("owner", "repo", "filepath"))
    data = _get_client().get_bytes(
        f"/repos/{owner}/{repo}/media/{filepath}", params=params or None
    )
    return base64.b64encode(data).decode()

@_op(gitea_read)
def get_wiki_page_revisions(
    owner: str,
    repo: str,
    page_name: Annotated[str, Field(description="Wiki page name as listed by list_wiki_pages (its `title`), e.g. 'Home'.")],
    page: Annotated[int | None, Field(description="1-based page number of the revision list.")] = None,
):
    """Get the commit revisions of a wiki page."""
    return _call("GET", "/repos/{owner}/{repo}/wiki/revisions/{page_name}", locals())

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

@_op(gitea_write)
def rename_branch(
    owner: str,
    repo: str,
    branch: Annotated[str, Field(description="Current name of the branch to rename.")],
    new_name: Annotated[str, Field(description="New branch name. Renaming the default branch needs repo-admin rights; a protected branch refuses the rename.")],
):
    """Rename a branch. Open pull requests and the repo's default-branch setting follow the new name."""
    return _call(
        "PATCH",
        "/repos/{owner}/{repo}/branches/{branch}",
        locals(),
        rename={"new_name": "name"},
    )

@_op(gitea_write)
def update_branch(
    owner: str,
    repo: str,
    branch: Annotated[str, Field(description="Name of the branch to move.")],
    new_commit_id: Annotated[str, Field(description="Commit SHA (or any ref Gitea can resolve) the branch should point to after the update.")],
    old_commit_id: Annotated[str | None, Field(description="Expected current tip SHA of the branch. If given it must match, otherwise the update is rejected — optimistic concurrency against a concurrent push.")] = None,
    force: Annotated[bool | None, Field(description="True = allow an update that is not a fast-forward (rewrites the branch's history).")] = None,
):
    """Update a branch reference to a new commit."""
    return _call("PUT", "/repos/{owner}/{repo}/branches/{branch}", locals())

@_op(gitea_write)
def update_branch_protection_priorities(
    owner: str,
    repo: str,
    ids: Annotated[list[int], Field(description="Branch protection rule IDs (int64) in the order they should be evaluated: the first id gets priority 1, the second 2, and so on. Rules left out of the list keep their current priority. NOTE: Gitea 1.27.3's branch-protection responses carry `priority` but not the rule id, so these ids come from the rule's web-UI URL, not from list_branch_protections.")],
):
    """Update the priorities of branch protections for a repository. The lowest priority wins when several rules match a branch."""
    return _call("POST", "/repos/{owner}/{repo}/branch_protections/priority", locals())

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

@_op(gitea_read)
def get_tag(
    owner: str,
    repo: str,
    tag: Annotated[str, Field(description="Tag NAME as shown by list_tags (e.g. 'v1.2.0') — not a SHA.")],
):
    """Get the tag of a repository by tag name. Works for both lightweight and annotated tags."""
    return _call("GET", "/repos/{owner}/{repo}/tags/{tag}", locals())

@_op(gitea_read)
def get_annotated_tag(
    owner: str,
    repo: str,
    sha: Annotated[str, Field(description="SHA of the TAG OBJECT — the `id` of an annotated tag from get_tag / list_tags. Lightweight tags have no tag object and 404 here.")],
):
    """Get the tag object of an annotated tag (not a lightweight tag), including its message, tagger and signature verification."""
    return _call("GET", "/repos/{owner}/{repo}/git/tags/{sha}", locals())

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

@_op(gitea_read)
def list_ref_commit_statuses(
    owner: str,
    repo: str,
    ref: Annotated[str, Field(description="Branch name, tag name, or commit SHA whose statuses should be listed. Unlike list_commit_statuses (SHA only), a branch/tag name resolves to its current HEAD.")],
    sort: Annotated[Literal["oldest", "recentupdate", "leastupdate", "leastindex", "highestindex"] | None, Field(description="Ordering of the returned statuses. Server default if omitted.")] = None,
    state: Annotated[Literal["pending", "success", "error", "failure", "warning"] | None, Field(description="Only return statuses in this state. Server default (all states) if omitted.")] = None,
):
    """List statuses for a branch, tag, or commit reference.

    Every status ever posted for the ref, newest contexts included — for the
    single rolled-up verdict use get_combined_commit_status instead."""
    params = _body(locals(), exclude=("owner", "repo", "ref"))
    return _ok(
        _get_client().paginate(
            f"/repos/{owner}/{repo}/commits/{ref}/statuses", params=params or None
        )
    )

@_op(gitea_read)
def get_commit_pull_request(
    owner: str,
    repo: str,
    sha: Annotated[str, Field(description="Commit SHA to look up. Returns the pull request whose merge introduced this commit.")],
):
    """Get the merged pull request that introduced a commit.

    404 when the commit did not arrive through a merged PR (direct push, or
    a PR still open)."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/commits/{sha}/pull"))

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

@_op(gitea_read)
def get_latest_release(owner: str, repo: str):
    """Get the most recent published release — the newest by created_at that is neither a draft nor a prerelease."""
    return _call("GET", "/repos/{owner}/{repo}/releases/latest", locals())

@_op(gitea_read)
def get_release_by_tag(
    owner: str,
    repo: str,
    tag: Annotated[str, Field(description="Git tag name of the release (e.g. 'v1.2.0') from list_tags or the tag_name of list_releases — NOT the numeric release id.")],
):
    """Get a release by its git tag name. Returns 404 for a bare tag that has no release attached."""
    return _call("GET", "/repos/{owner}/{repo}/releases/tags/{tag}", locals())

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

@_op(gitea_delete)
def delete_release_by_tag(
    owner: str,
    repo: str,
    tag: Annotated[str, Field(description="Git tag name of the release to delete (e.g. 'v1.2.0'), from list_tags or the tag_name of list_releases — NOT the numeric release id.")],
):
    """Delete a release addressed by its git tag name rather than its id (contrast delete_release, which takes release_id).

    Deletes the release and its attachments but KEEPS the git tag — the tag
    stays listed by list_tags. Use delete_tag to remove the tag itself."""
    return _call("DELETE", "/repos/{owner}/{repo}/releases/tags/{tag}", locals())

@_op(gitea_read)
def list_release_attachments(owner: str, repo: str, release_id: int):
    """List a release's attachments (the downloadable assets under /releases/{id}/assets)."""
    return _call("GET", "/repos/{owner}/{repo}/releases/{release_id}/assets", locals())

@_op(gitea_read)
def get_release_attachment(
    owner: str,
    repo: str,
    release_id: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_release_attachments — NOT the filename and NOT the uuid.")],
):
    """Get one release attachment's metadata (name, size, download count, browser_download_url)."""
    return _call("GET", "/repos/{owner}/{repo}/releases/{release_id}/assets/{attachment_id}", locals())

@_op(gitea_write)
def create_release_attachment(
    owner: str,
    repo: str,
    release_id: int,
    name: Annotated[str, Field(description="Filename the attachment is published under, e.g. 'gitea-mcp-1.0.0.tar.gz'. Gitea may reject extensions its attachment allowlist forbids (400).")],
    content: Annotated[str, Field(description="Base64-encoded file content. An MCP client cannot send raw bytes, so encode the file first; it is decoded here and uploaded as multipart/form-data.")],
):
    """Upload a file as an attachment on a release.

    Sent as multipart/form-data under the `attachment` field, with `name` as
    the published filename. Oversized files are rejected by Gitea with 413."""
    return _ok(
        _get_client().upload(
            f"/repos/{owner}/{repo}/releases/{release_id}/assets",
            "attachment",
            name,
            base64.b64decode(content),
            params={"name": name},
        )
    )

@_op(gitea_write)
def edit_release_attachment(
    owner: str,
    repo: str,
    release_id: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_release_attachments — NOT the filename and NOT the uuid.")],
    name: Annotated[str | None, Field(description="New filename for the attachment. This is the only editable field; the uploaded bytes cannot be replaced — delete and re-upload instead.")] = None,
):
    """Rename a release attachment."""
    return _call("PATCH", "/repos/{owner}/{repo}/releases/{release_id}/assets/{attachment_id}", locals())

@_op(gitea_delete)
def delete_release_attachment(
    owner: str,
    repo: str,
    release_id: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_release_attachments — NOT the filename and NOT the uuid.")],
):
    """Delete a release attachment. The stored file is removed; the release itself is untouched."""
    return _call("DELETE", "/repos/{owner}/{repo}/releases/{release_id}/assets/{attachment_id}", locals())

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

@_op(gitea_read)
def get_repo_label(
    owner: str,
    repo: str,
    label_id: Annotated[int, Field(description="Label ID (int64) from list_repo_labels — NOT the label name.")],
):
    """Get a single repository label by ID."""
    return _call("GET", "/repos/{owner}/{repo}/labels/{label_id}", locals())

@_op(gitea_read)
def list_label_templates():
    """List the names of Gitea's built-in label template sets.

    These are the starter label sets (e.g. 'Advanced', 'Default') an instance
    ships with, not any repository's labels. Feed a name to
    get_label_template to see what is inside one."""
    return _ok(_get_client().get("/label/templates"))

@_op(gitea_read)
def get_label_template(
    name: Annotated[str, Field(description="Template set name as returned by list_label_templates (e.g. 'Default', 'Advanced').")],
):
    """List the labels defined in one built-in label template set.

    Returns name/color/description/exclusive per label. These are templates
    only — nothing is created until you pass the values to
    create_repo_label."""
    return _ok(_get_client().get(f"/label/templates/{name}"))

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
def list_pinned_issues(
    owner: str,
    repo: str,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea issue objects.")] = True,
):
    """List a repository's pinned issues, in pin order.

    Pull requests are pinned separately and are not returned here. The
    endpoint takes no paging: Gitea caps the number of pins per repo.

    brief (default True): compact view — number, title, state, labels,
    assignees, updated_at, and the <brief>...</brief> body summary.
    Set brief=False for full Gitea API response objects."""
    data = _get_client().get(f"/repos/{owner}/{repo}/issues/pinned")
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

@_op(gitea_delete)
def delete_issue(owner: str, repo: str, index: int):
    """Delete an issue outright — the issue and its comments are gone for good.

    This is NOT closing an issue: to close one, use edit_issue with
    state='closed'. Requires repo admin rights; Gitea answers 204 on success."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}", locals())

@_op(gitea_write)
def add_issue_assignees(
    owner: str,
    repo: str,
    index: int,
    assignees: Annotated[list[str], Field(description=(
        "USERNAMES to add as assignees (NOT user IDs / NOT display names). "
        "Added to whoever is already assigned; use edit_issue to replace the "
        "whole set instead. A username that cannot be assigned in this repo "
        "returns 422."
    ))],
):
    """Add assignees to an issue, keeping the existing ones."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/assignees",
            json={"assignees": assignees},
        )
    )

@_op(gitea_delete)
def remove_issue_assignees(
    owner: str,
    repo: str,
    index: int,
    assignees: Annotated[list[str], Field(description=(
        "USERNAMES to unassign (NOT user IDs / NOT display names), from "
        "get_issue's `assignees[].login`. Names that are not assigned are "
        "ignored; the rest of the assignees stay."
    ))],
):
    """Remove assignees from an issue, leaving the others assigned."""
    # Gitea reads the username list from a body on this DELETE, which the
    # client's `delete()` (query-params only) cannot carry.
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{index}/assignees",
            json={"assignees": assignees},
        )
    )

@_op(gitea_read)
def check_issue_assignee(
    owner: str,
    repo: str,
    index: int,
    assignee: Annotated[str, Field(description="USERNAME to test for assignability (NOT a user ID / display name).")],
):
    """Check whether a user may be assigned to an issue.

    Returns {'status': 'ok'} when the user exists and can be assigned (Gitea
    answers 204); raises a 404 GiteaError when the user does not exist or
    lacks read access to the repo. Worth calling before add_issue_assignees,
    which fails the whole request if any one username is unassignable."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/assignees/{assignee}", locals())

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

@_op(gitea_read)
def get_issue_comment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
):
    """Get one issue comment by its ID, with the full (unslimmed) body."""
    return _call("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}", locals())

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

@_op(gitea_write)
def edit_issue_comment_deprecated(
    owner: str,
    repo: str,
    index: Annotated[int, Field(description="Issue index. Gitea ignores it on this route — the comment is found by comment_id alone — but the path requires a value.")],
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
    body: Annotated[str, Field(description="Replacement comment body as markdown text.")],
):
    """Edit a comment through the deprecated issue-scoped route.

    Gitea marks this endpoint deprecated: prefer edit_issue_comment, which
    takes the same comment_id without the ignored index. Kept so the route
    stays reachable on instances whose clients still use it."""
    return _call("PATCH", "/repos/{owner}/{repo}/issues/{index}/comments/{comment_id}", locals())

@_op(gitea_delete)
def delete_issue_comment_deprecated(
    owner: str,
    repo: str,
    index: Annotated[int, Field(description="Issue index. Gitea ignores it on this route — the comment is found by comment_id alone — but the path requires a value.")],
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
):
    """Delete a comment through the deprecated issue-scoped route.

    Gitea marks this endpoint deprecated: prefer delete_issue_comment, which
    takes the same comment_id without the ignored index."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/comments/{comment_id}", locals())

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

@_op(gitea_delete)
def reset_issue_tracked_times(owner: str, repo: str, index: int):
    """Delete ALL of the authenticated user's tracked time on an issue.

    Wipes every entry that user logged, not one of them — delete_tracked_time
    removes a single entry by its time_id. Other users' entries are untouched.
    Returns 400 when time tracking is disabled on the repo."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/times", locals())

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

@_op(gitea_read)
def list_issue_blocks(
    owner: str,
    repo: str,
    index: int,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea issue objects.")] = True,
):
    """List the issues this issue blocks — the reverse of list_issue_dependencies.

    Those issues cannot be closed until this one is. Dependencies are the
    other direction: issues that must close before this one can.

    brief (default True): compact view — number, title, state, labels,
    assignees, updated_at, and the <brief>...</brief> body summary.
    Set brief=False for full Gitea API response objects."""
    data = _get_client().paginate(f"/repos/{owner}/{repo}/issues/{index}/blocks")
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_write)
def add_issue_block(
    owner: str,
    repo: str,
    index: int,
    blocked_index: Annotated[int, Field(description=(
        "Issue index of the issue to BLOCK (the per-repo issue number, NOT a "
        "global issue ID). Issue #blocked_index will not be closable until "
        "issue #index closes."
    ))],
    blocked_owner: Annotated[str | None, Field(description="Owner of the blocked issue's repo, for a cross-repo block. Defaults to `owner`; cross-repo needs the instance's cross-repository-dependencies setting enabled.")] = None,
    blocked_repo: Annotated[str | None, Field(description="Repo NAME of the blocked issue, for a cross-repo block. Defaults to `repo`.")] = None,
):
    """Make this issue block another one, so the other cannot close first."""
    # Body is Gitea's IssueMeta {index, owner, repo} naming the BLOCKED issue.
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/blocks",
            json={
                "index": blocked_index,
                "owner": blocked_owner or owner,
                "repo": blocked_repo or repo,
            },
        )
    )

@_op(gitea_delete)
def remove_issue_block(
    owner: str,
    repo: str,
    index: int,
    blocked_index: Annotated[int, Field(description=(
        "Issue index of the issue to UNBLOCK (per-repo issue number, NOT a "
        "global issue ID). Removes the block edge from issue #index to "
        "issue #blocked_index."
    ))],
    blocked_owner: Annotated[str | None, Field(description="Owner of the blocked issue's repo, for a cross-repo block. Defaults to `owner`.")] = None,
    blocked_repo: Annotated[str | None, Field(description="Repo NAME of the blocked issue, for a cross-repo block. Defaults to `repo`.")] = None,
):
    """Stop this issue from blocking another one."""
    # Gitea reads the IssueMeta body on this DELETE, which the client's
    # `delete()` (query-params only) cannot carry.
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{index}/blocks",
            json={
                "index": blocked_index,
                "owner": blocked_owner or owner,
                "repo": blocked_repo or repo,
            },
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
def move_issue_pin(
    owner: str,
    repo: str,
    index: int,
    position: Annotated[int, Field(description=(
        "New 1-based pin position; 1 is first. Must be >= 1 — Gitea rejects "
        "0 or negatives. Other pins shift to close the gap, so a position "
        "past the last pin lands the issue at the end."
    ))],
):
    """Move an already-pinned issue to a different position in the pin order.

    The issue must already be pinned (see pin_issue); this only reorders.
    Use list_pinned_issues to read the current order."""
    return _call("PATCH", "/repos/{owner}/{repo}/issues/{index}/pin/{position}", locals())

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

@_op(gitea_read)
def check_issue_subscription(owner: str, repo: str, index: int):
    """Check whether the AUTHENTICATED user is subscribed to an issue.

    Always reports the caller's own subscription — it takes no username, so
    to inspect someone else's use list_issue_subscriptions. Returns a
    WatchInfo object whose `subscribed` field carries the answer."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/subscriptions/check", locals())

@_op(gitea_read)
def list_issue_attachments(owner: str, repo: str, index: int):
    """List an issue's attachments (id, name, size, download URL).

    Attachments hang off the issue itself; files attached to a comment are
    listed by list_issue_comment_attachments instead."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/assets", locals())

@_op(gitea_read)
def get_issue_attachment(
    owner: str,
    repo: str,
    index: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_attachments — NOT the issue index.")],
):
    """Get one issue attachment's metadata, including its download URL.

    Returns the JSON record, not the file bytes: fetch `browser_download_url`
    to get the content."""
    return _call("GET", "/repos/{owner}/{repo}/issues/{index}/assets/{attachment_id}", locals())

@_op(gitea_write)
def create_issue_attachment(
    owner: str,
    repo: str,
    index: int,
    filename: Annotated[str, Field(description="Filename to send in the multipart part, e.g. 'trace.log'. Used as the display name when `name` is omitted.")],
    content: Annotated[str, Field(description="Base64-encoded file content. An MCP client cannot send raw bytes, so encode first; the op decodes before upload.")],
    name: Annotated[str | None, Field(description="Display name to store the attachment under, overriding `filename`. Sent as a QUERY param, per Gitea's spec.")] = None,
):
    """Attach a file to an issue. Content is sent base64-encoded and decoded here."""
    # `name` is a query param and must vanish when omitted, so the params dict
    # comes from _body(), which drops the None; the file itself rides the
    # multipart part and is not a wire field.
    return _ok(
        _get_client().upload(
            f"/repos/{owner}/{repo}/issues/{index}/assets",
            "attachment",
            filename,
            base64.b64decode(content),
            params=_body(locals(), exclude=("owner", "repo", "index", "filename", "content")),
        )
    )

@_op(gitea_write)
def edit_issue_attachment(
    owner: str,
    repo: str,
    index: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_attachments — NOT the issue index.")],
    name: Annotated[str | None, Field(description="New display filename for the attachment. Renames the record only; the stored bytes are untouched.")] = None,
):
    """Rename an issue attachment. Only the display name can be changed."""
    return _call("PATCH", "/repos/{owner}/{repo}/issues/{index}/assets/{attachment_id}", locals())

@_op(gitea_delete)
def delete_issue_attachment(
    owner: str,
    repo: str,
    index: int,
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_attachments — NOT the issue index.")],
):
    """Delete an issue attachment. The stored file is removed for good."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/{index}/assets/{attachment_id}", locals())

@_op(gitea_read)
def list_issue_comment_attachments(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
):
    """List the attachments on one issue comment (id, name, size, download URL)."""
    return _call("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}/assets", locals())

@_op(gitea_read)
def get_issue_comment_attachment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_comment_attachments.")],
):
    """Get one comment attachment's metadata, including its download URL.

    Returns the JSON record, not the file bytes: fetch `browser_download_url`
    to get the content."""
    return _call("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}/assets/{attachment_id}", locals())

@_op(gitea_write)
def create_issue_comment_attachment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
    filename: Annotated[str, Field(description="Filename to send in the multipart part, e.g. 'trace.log'. Used as the display name when `name` is omitted.")],
    content: Annotated[str, Field(description="Base64-encoded file content. An MCP client cannot send raw bytes, so encode first; the op decodes before upload.")],
    name: Annotated[str | None, Field(description="Display name to store the attachment under, overriding `filename`. Sent as a QUERY param, per Gitea's spec.")] = None,
):
    """Attach a file to an issue comment. Content is sent base64-encoded and decoded here."""
    # `name` is a query param and must vanish when omitted, so the params dict
    # comes from _body(), which drops the None; the file itself rides the
    # multipart part and is not a wire field.
    return _ok(
        _get_client().upload(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/assets",
            "attachment",
            filename,
            base64.b64decode(content),
            params=_body(locals(), exclude=("owner", "repo", "comment_id", "filename", "content")),
        )
    )

@_op(gitea_write)
def edit_issue_comment_attachment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_comment_attachments.")],
    name: Annotated[str | None, Field(description="New display filename for the attachment. Renames the record only; the stored bytes are untouched.")] = None,
):
    """Rename a comment attachment. Only the display name can be changed."""
    return _call("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}/assets/{attachment_id}", locals())

@_op(gitea_delete)
def delete_issue_comment_attachment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Comment ID (int64) from list_issue_comments — NOT the issue index.")],
    attachment_id: Annotated[int, Field(description="Attachment ID (int64) from list_issue_comment_attachments.")],
):
    """Delete a comment attachment. The stored file is removed for good."""
    return _call("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}/assets/{attachment_id}", locals())

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
def list_pinned_pull_requests(
    owner: str,
    repo: str,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea PR objects.")] = True,
):
    """List a repository's pinned pull requests.

    Pinned PRs are the ones maintainers stuck to the top of the repo's PR list;
    the set is small and always returned whole.

    brief (default True): compact view — number, title, state, labels, assignees,
    updated_at, and body summary extracted from a <brief>...</brief> tag.
    Set brief=False for full Gitea API response objects."""
    # Unpaginated endpoint: Gitea ignores page/limit here and returns everything.
    data = _get_client().get(f"/repos/{owner}/{repo}/pulls/pinned")
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_read)
def get_pull_request(owner: str, repo: str, index: int):
    """Get a pull request by index."""
    return _call("GET", "/repos/{owner}/{repo}/pulls/{index}", locals())

@_op(gitea_read)
def get_pull_request_by_base_head(
    owner: str,
    repo: str,
    base: Annotated[str, Field(description="Base branch name — the branch the PR merges INTO, e.g. 'main'. A branch name, never a PR index.")],
    head: Annotated[str, Field(description="Head branch name — the branch the PR merges FROM. For a PR opened from a fork use 'forkOwner:branch' (or 'forkOwner/forkRepo:branch'), the same spelling create_pull_request takes.")],
):
    """Get a pull request by its base and head branch instead of its index.

    Matches the PR whose base_branch and head_branch are exactly these, in any
    state (open, closed or merged); 404 when no PR connects the two branches.
    Use it to find the PR for a branch you just pushed without listing PRs."""
    return _call("GET", "/repos/{owner}/{repo}/pulls/{base}/{head}", locals())

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
def check_pull_request_merged(owner: str, repo: str, index: int):
    """Check whether a pull request has already been merged.

    Gitea answers this one with status only: a body-less 204 when the PR IS
    merged, a 404 when it is not (or does not exist). There is therefore no
    payload to return — a successful call, reported as {"status": "ok"},
    means merged, and a Gitea 404 error means not merged. When you want the
    answer as a value rather than as an error, read the `merged` field of
    get_pull_request instead."""
    return _call("GET", "/repos/{owner}/{repo}/pulls/{index}/merge", locals())

@_op(gitea_execute)
def cancel_scheduled_auto_merge(owner: str, repo: str, index: int):
    """Cancel the auto-merge scheduled for a pull request.

    Undoes a merge scheduled with Gitea's `merge_when_checks_succeed` option:
    the PR stays open and is no longer merged on its own once checks go green.
    Merging already done is not undone by this. 404 when no auto-merge is
    scheduled for the PR."""
    return _call("DELETE", "/repos/{owner}/{repo}/pulls/{index}/merge", locals())

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

@_op(gitea_read)
def get_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: Annotated[int, Field(description="Review ID (int64) from list_pull_reviews — NOT the PR index and NOT a review comment id.")],
):
    """Get one review on a pull request, with its state, body and reviewer."""
    return _call("GET", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}", locals())

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

@_op(gitea_write)
def undismiss_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: Annotated[int, Field(description="Review ID (int64) from list_pull_reviews — the dismissed review to restore.")],
):
    """Undo the dismissal of a pull request review.

    Reverse of dismiss_pull_review: the review counts again toward approvals or
    change requests. Returns the restored review."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/undismissals"
        )
    )

@_op(gitea_write)
def reply_to_pull_review_comment(
    owner: str,
    repo: str,
    index: int,
    comment_id: Annotated[int, Field(description="Review COMMENT ID (int64) from get_pull_review_comments — the inline comment being replied to, NOT the review id.")],
    body: Annotated[str, Field(description="Reply text as markdown.")],
):
    """Reply to an inline review comment on a pull request.

    The reply joins the same thread as `comment_id` — same review, same file and
    line — so this is how to answer a reviewer in place rather than opening a new
    conversation. Gitea rejects a comment that is not a code review comment (400)
    and a comment belonging to a different PR than `index` (404)."""
    return _call("POST", "/repos/{owner}/{repo}/pulls/{index}/comments/{comment_id}/replies", locals())

@_op(gitea_write)
def resolve_pull_review_comment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Review COMMENT ID (int64) from get_pull_review_comments. The path carries no PR index — the comment id alone identifies the thread.")],
):
    """Mark a pull request review comment's conversation as resolved.

    Resolving collapses the thread in the UI. Needs permission to mark
    conversations (PR author or repo write access), else 403; 400 when the id
    is not an inline code review comment."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/resolve")
    )

@_op(gitea_write)
def unresolve_pull_review_comment(
    owner: str,
    repo: str,
    comment_id: Annotated[int, Field(description="Review COMMENT ID (int64) from get_pull_review_comments. The path carries no PR index — the comment id alone identifies the thread.")],
):
    """Reopen a resolved pull request review comment's conversation.

    Reverse of resolve_pull_review_comment, and a POST like it rather than a
    delete — nothing is removed, the thread just counts as unresolved again."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/pulls/comments/{comment_id}/unresolve")
    )

# ── Actions / CI ─────────────────────────────────────────────────────────────


_ActionRunStatus = Annotated[Literal["pending", "waiting", "requested", "action_required", "queued", "in_progress", "completed", "failure", "success", "skipped", "neutral", "cancelled", "timed_out"] | None, Field(description="Keep only entries in this state. Several names map onto one internal status: 'pending'/'waiting'/'requested'/'action_required' = blocked, 'queued' = waiting for a runner, 'in_progress' = running, 'skipped'/'neutral' = skipped, 'cancelled'/'timed_out' = cancelled, and 'completed' = any of success/failure/skipped/cancelled. Any other value is rejected with 400.")]

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

@_op(gitea_write)
def enable_workflow(
    owner: str,
    repo: str,
    workflow_id: Annotated[str, Field(description="Workflow file name under .gitea/workflows/ (e.g. 'ci.yml') or its numeric ID — the same value get_workflow takes.")],
):
    """Enable a disabled workflow so its triggers fire again. Returns {'status': 'ok'} (Gitea answers 204)."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/enable"
        )
    )

@_op(gitea_write)
def disable_workflow(
    owner: str,
    repo: str,
    workflow_id: Annotated[str, Field(description="Workflow file name under .gitea/workflows/ (e.g. 'ci.yml') or its numeric ID — the same value get_workflow takes.")],
):
    """Disable a workflow: its triggers stop firing until enable_workflow. Runs already in flight are untouched. Returns {'status': 'ok'} (Gitea answers 204)."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/disable"
        )
    )

@_op(gitea_read)
def list_runs_for_workflow(
    owner: str,
    repo: str,
    workflow_id: Annotated[str, Field(description="Workflow file name under .gitea/workflows/ (e.g. 'ci.yml') or its numeric ID — the same value get_workflow takes.")],
    event: Annotated[str | None, Field(description="Keep only runs triggered by this event name, e.g. 'push', 'pull_request', 'workflow_dispatch', 'schedule'.")] = None,
    branch: Annotated[str | None, Field(description="Keep only runs whose head branch is this branch name (bare name, no 'refs/heads/' prefix).")] = None,
    status: _ActionRunStatus = None,
    actor: Annotated[str | None, Field(description="USERNAME of the user who triggered the run (NOT a display name, NOT a user ID).")] = None,
    head_sha: Annotated[str | None, Field(description="Full commit SHA the run was triggered for (40 hex chars, not abbreviated).")] = None,
    exclude_pull_requests: Annotated[bool | None, Field(description="True empties the `pull_requests` field of every returned run — a smaller payload when the PR links are not needed.")] = None,
    scoped_workflow_source_repo_id: Annotated[int | None, Field(description="For a scoped workflow, the int64 ID of the repository that provides it. Omit (or pass 0) for a workflow defined in this repo.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea workflow-run objects.")] = True,
):
    """List runs of one workflow (list_workflow_runs is the whole-repo equivalent).

    brief (default True): compact view — id, title, status, conclusion,
    event, branch, sha, run_number, path, timestamps.
    Set brief=False for full Gitea API response objects."""
    params = _body(locals(), exclude=("owner", "repo", "workflow_id", "brief"))
    data = _get_client().get(
        f"/repos/{owner}/{repo}/actions/workflows/{workflow_id}/runs",
        params=params or None,
    )
    if brief:
        data = _slim_workflow_runs(data)
    return _ok(data)

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

@_op(gitea_delete)
def delete_workflow_run(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number). The run must already be finished — Gitea returns 400 for a run still in flight.")],
):
    """Delete a finished workflow run and everything hanging off it (jobs, logs, artifacts)."""
    return _call("DELETE", "/repos/{owner}/{repo}/actions/runs/{run_id}", locals())

@_op(gitea_read)
def get_workflow_run_attempt(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
    attempt: Annotated[int, Field(description="Logical attempt number within the run, 1-based: 1 is the original run, 2 the first rerun, and so on. A run's `previous_attempt_url` names the attempt before the current one.")],
):
    """Get one attempt of a workflow run — the state of that run as of that rerun."""
    return _ok(_slim_workflow_run(
        _get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}")
    ))

@_op(gitea_read)
def list_workflow_run_attempt_jobs(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
    attempt: Annotated[int, Field(description="Logical attempt number within the run, 1-based: 1 is the original run, 2 the first rerun, and so on.")],
    status: _ActionRunStatus = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea job objects.")] = True,
):
    """List the jobs of one attempt of a workflow run.

    list_workflow_run_jobs returns the run's current jobs; this one is pinned
    to a single attempt, so an earlier attempt's jobs stay reachable after a
    rerun. brief (default True): id, name, status, conclusion, run_id,
    timestamps, per-step status."""
    params = _body(locals(), exclude=("owner", "repo", "run_id", "attempt", "brief"))
    data = _get_client().get(
        f"/repos/{owner}/{repo}/actions/runs/{run_id}/attempts/{attempt}/jobs",
        params=params or None,
    )
    if brief:
        data = _slim_jobs(data)
    return _ok(data)

@_op(gitea_read)
def list_workflow_jobs(
    owner: str,
    repo: str,
    status: _ActionRunStatus = None,
    sort: Annotated[Literal["id"] | None, Field(description="Sort key. Gitea supports only 'id' here (job creation order); omitted = 'id'.")] = None,
    order: Annotated[Literal["asc", "desc"] | None, Field(description="Sort direction. Omitted = 'asc'.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea job objects.")] = True,
):
    """List jobs across every workflow run in a repository.

    Repo-wide, unlike list_workflow_run_jobs which is scoped to one run — use
    this to find e.g. every failing job without walking the runs first.
    brief (default True): id, name, status, conclusion, run_id, timestamps,
    per-step status."""
    params = _body(locals(), exclude=("owner", "repo", "brief"))
    data = _get_client().get(
        f"/repos/{owner}/{repo}/actions/jobs", params=params or None
    )
    if brief:
        data = _slim_jobs(data)
    return _ok(data)

@_op(gitea_execute)
def rerun_workflow_run(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
):
    """Rerun every job of a workflow run. Starts a new attempt; returns the run it queued."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun"))

@_op(gitea_execute)
def rerun_failed_workflow_jobs(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
):
    """Rerun only the failed jobs of a workflow run.

    Gitea returns 400 when the run has no failed job to rerun. The response
    body is empty, so this returns {'status': 'ok'} — poll get_workflow_run
    for the new attempt."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs")
    )

@_op(gitea_execute)
def rerun_workflow_job(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
    job_id: Annotated[int, Field(description="Job ID from list_workflow_run_jobs — must belong to `run_id`, otherwise Gitea returns 404.")],
):
    """Rerun one job of a workflow run. Returns the rerun job of the new attempt."""
    return _ok(_slim_job(
        _get_client().post(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs/{job_id}/rerun")
    ))

@_op(gitea_read)
def list_action_artifacts(
    owner: str,
    repo: str,
    name: Annotated[str | None, Field(description="Keep only artifacts with exactly this name (the `name:` of the upload-artifact step). Omitted = every artifact in the repo.")] = None,
):
    """List the artifacts of a repository, newest runs included.

    Only v4 (finalized) artifacts are listed; artifacts written by the
    legacy v3 uploader are invisible to this API."""
    return _call("GET", "/repos/{owner}/{repo}/actions/artifacts", locals())

@_op(gitea_read)
def list_workflow_run_artifacts(
    owner: str,
    repo: str,
    run_id: Annotated[int, Field(description="Internal run ID from list_workflow_runs (NOT run_number).")],
    name: Annotated[str | None, Field(description="Keep only artifacts with exactly this name (the `name:` of the upload-artifact step). Omitted = every artifact of the run.")] = None,
):
    """List the artifacts produced by one workflow run."""
    return _call("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts", locals())

@_op(gitea_read)
def get_action_artifact(
    owner: str,
    repo: str,
    artifact_id: Annotated[int, Field(description="Artifact ID from list_action_artifacts / list_workflow_run_artifacts (NOT the artifact name).")],
):
    """Get one artifact's metadata: name, size, expiry, and its download URL."""
    return _call("GET", "/repos/{owner}/{repo}/actions/artifacts/{artifact_id}", locals())

@_op(gitea_read)
def download_action_artifact(
    owner: str,
    repo: str,
    artifact_id: Annotated[int, Field(description="Artifact ID from list_action_artifacts / list_workflow_run_artifacts (NOT the artifact name).")],
):
    """Download an artifact's zip, returned base64-encoded under `zip_base64`.

    Gitea answers this endpoint with a 302 to a short-lived signed URL, which
    may live on a different host (object storage) than the API; the redirect is
    followed and the signature in that URL is what authenticates the second
    hop. An expired artifact still has metadata but no content."""
    data = _get_client().download(
        f"/repos/{owner}/{repo}/actions/artifacts/{artifact_id}/zip"
    )
    return {
        "artifact_id": artifact_id,
        "size_in_bytes": len(data),
        "zip_base64": base64.b64encode(data).decode(),
    }

@_op(gitea_delete)
def delete_action_artifact(
    owner: str,
    repo: str,
    artifact_id: Annotated[int, Field(description="Artifact ID from list_action_artifacts / list_workflow_run_artifacts (NOT the artifact name).")],
):
    """Delete one artifact of a workflow run. The run and its logs are untouched."""
    return _call("DELETE", "/repos/{owner}/{repo}/actions/artifacts/{artifact_id}", locals())

@_op(gitea_read)
def list_action_tasks(
    owner: str,
    repo: str,
    limit: Annotated[int | None, Field(description="Page size. Gitea caps this at 50.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
):
    """List a repository's action tasks — the runner-side units behind its runs.

    Returned under the key `workflow_runs` (Gitea reuses the run wrapper), with
    `total_count`. Not slimmed: a task carries runner and timing fields a run
    does not."""
    return _call("GET", "/repos/{owner}/{repo}/actions/tasks", locals())

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
def list_all_orgs():
    """List every organization visible on the instance.

    Instance-wide, unlike list_orgs (only the orgs the caller belongs to).
    Anonymous callers see public orgs, signed-in callers also see limited
    ones, and site admins also see private ones."""
    return _ok(_get_client().paginate("/orgs"))

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

@_op(gitea_execute)
def rename_org(
    org: str,
    new_name: Annotated[str, Field(description="New org login (the URL slug). Must be unused — no other user or organization may already hold it.")],
):
    """Rename an organization — its URLs change.

    The login is the org's URL segment, so every web link, API path and git
    remote under the old name moves to the new one. Clones pointing at the
    old name must be re-pointed by hand, and the freed old name can be
    claimed by someone else afterwards. Returns no body on success."""
    return _call("POST", "/orgs/{org}/rename", locals())

@_op(gitea_write)
def update_org_avatar(
    org: str,
    image: Annotated[str, Field(description="Base64-encoded image file content (PNG/JPEG/GIF) — the bytes of the file, base64-encoded into a string. NOT a URL and NOT a file path; Gitea decodes it server-side.")],
):
    """Set an organization's avatar from a base64-encoded image.

    The image travels as a base64 string inside the JSON body, not as a
    multipart file upload. Returns no body on success."""
    return _call("POST", "/orgs/{org}/avatar", locals())

@_op(gitea_delete)
def delete_org_avatar(org: str):
    """Clear an organization's custom avatar, reverting it to the generated default."""
    return _ok(_get_client().delete(f"/orgs/{org}/avatar"))

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

@_op(gitea_delete)
def delete_org_repos(org: str):
    """Delete EVERY repository owned by an organization. Irreversible.

    One call wipes the org's whole repo list — no per-repo confirmation and
    no undo. Gitea answers 202 and finishes the deletion in a background
    task, so list_org_repos can still report repos for a while afterwards.
    To remove a single repository use delete_repo instead."""
    return _ok(_get_client().delete(f"/orgs/{org}/repos"))

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

@_op(gitea_read)
def list_org_blocked_users(org: str):
    """List the users an organization has blocked."""
    return _ok(_get_client().paginate(f"/orgs/{org}/blocks"))

@_op(gitea_read)
def check_user_blocked_by_org(org: str, username: str):
    """Check whether an organization blocks a user.

    A membership-style check that answers with no body: blocked is 204
    (returned here as {"status": "ok"}), not blocked is 404, which surfaces
    as a GiteaError. A 404 from this op is the negative answer, not a
    missing org or user."""
    return _ok(_get_client().get(f"/orgs/{org}/blocks/{username}"))

@_op(gitea_write)
def block_user_from_org(
    org: str,
    username: str,
    note: Annotated[str | None, Field(description="Free-text reason recorded alongside the block. Sent as a query param, not a body field.")] = None,
):
    """Block a user on behalf of an organization.

    Blocking also unfollows, unstars, unwatches, unassigns and removes the
    user as a collaborator across the org's repos, and cancels pending repo
    transfers between them. Gitea refuses with 422 when the target is a
    member of the org or is itself an organization."""
    params = _body(locals(), exclude=("org", "username"))
    return _ok(_get_client().put(f"/orgs/{org}/blocks/{username}", params=params or None))

@_op(gitea_delete)
def unblock_user_from_org(org: str, username: str):
    """Unblock a user previously blocked by an organization.

    Only lifts the block; the follows, stars, watches and collaborations
    that blocking removed are not restored."""
    return _ok(_get_client().delete(f"/orgs/{org}/blocks/{username}"))

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

@_op(gitea_write)
def create_org_repo_deprecated(
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
    """Create a repository in an organization via Gitea's deprecated singular-'org' path.

    Same handler and same options as create_org_repo, which uses the current
    /orgs/{org}/repos route — prefer that one. This op exists only to keep
    the legacy /org/{org}/repos path reachable."""
    private = _enforce_private(private)
    return _call("POST", "/org/{org}/repos", locals())

@_op(gitea_read)
def list_org_activities(
    org: str,
    date: Annotated[str | None, Field(description="Restrict the feed to a single day, as 'YYYY-MM-DD'. Omit for the recent feed across all days.")] = None,
):
    """List an organization's activity feed — pushes, repo creations, issue and PR events."""
    params = _body(locals(), exclude=("org",))
    return _ok(_get_client().paginate(f"/orgs/{org}/activities/feeds", params=params or None))

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
def search_org_teams(
    org: str,
    query: Annotated[str | None, Field(description="Search keyword (substring match against team name).")] = None,
    include_desc: Annotated[bool | None, Field(description="True (Gitea's default) = also match `query` against team descriptions; False = match team names only.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = None,
    page: Annotated[int | None, Field(description="1-based page number.")] = None,
):
    """Search for teams within an organization by keyword.

    Unlike list_org_teams this returns ONE page, not every team — use
    `page`/`limit` to walk the results."""
    return _call("GET", "/orgs/{org}/teams/search", locals(), rename={"query": "q"})

@_op(gitea_read)
def get_team(team_id: int):
    """Get a team by ID."""
    return _ok(_get_client().get(f"/teams/{team_id}"))

@_op(gitea_read)
def list_team_activities(
    team_id: int,
    date: Annotated[str | None, Field(description="Single calendar day to report, as 'YYYY-MM-DD'. Omit for the most recent activity.")] = None,
):
    """List a team's activity feeds."""
    params = _body(locals(), exclude=("team_id",))
    return _ok(
        _get_client().paginate(f"/teams/{team_id}/activities/feeds", params=params or None)
    )

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

@_op(gitea_read)
def get_team_member(team_id: int, username: str):
    """Get one member of a team. Returns the user, or 404 if they are not on the team."""
    return _ok(_get_client().get(f"/teams/{team_id}/members/{username}"))

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

@_op(gitea_write)
def add_repo_team(
    owner: str,
    repo: str,
    team: Annotated[str, Field(description="Team NAME (e.g. 'Owners') from list_org_teams — this repo-side endpoint takes the name, NOT the numeric team ID that add_team_repo takes.")],
):
    """Grant a team access to a repository, addressed by team name.

    Repo-side twin of add_team_repo; the team must belong to the repo's org."""
    return _ok(_get_client().put(f"/repos/{owner}/{repo}/teams/{team}"))

@_op(gitea_delete)
def remove_repo_team(
    owner: str,
    repo: str,
    team: Annotated[str, Field(description="Team NAME (e.g. 'Owners') from list_org_teams — this repo-side endpoint takes the name, NOT the numeric team ID that remove_team_repo takes.")],
):
    """Revoke a team's access to a repository, addressed by team name.

    Repo-side twin of remove_team_repo."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/teams/{team}"))

@_op(gitea_read)
def check_repo_team(
    owner: str,
    repo: str,
    team: Annotated[str, Field(description="Team NAME (e.g. 'Owners') from list_org_teams — this repo-side endpoint takes the name, NOT the numeric team ID that check_team_repo takes.")],
):
    """Check whether a team has access to a repository. Returns the team, or 404 if not assigned."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/teams/{team}"))

# ── Org Labels ───────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_labels(org: str):
    """List labels for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/labels"))

@_op(gitea_read)
def get_org_label(
    org: str,
    label_id: Annotated[int, Field(description="Label ID (int64) from list_org_labels — NOT the label name.")],
):
    """Get a single organization label by ID."""
    return _ok(_get_client().get(f"/orgs/{org}/labels/{label_id}"))

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

@_op(gitea_read)
def get_latest_package_version(
    owner: str,
    type: _PackageType,
    name: _PackageName,
):
    """Get the newest version of a package without knowing its version string.

    The `-` in Gitea's `/packages/{owner}/{type}/{name}/-/latest` route is a
    literal path segment of the API's grammar, not a placeholder — nothing is
    substituted for it. Returns the same object as get_package; 404 when the
    package has no versions.
    """
    return _ok(
        _get_client().get(f"/packages/{owner}/{type}/{name}/-/latest")
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

@_op(gitea_delete)
def delete_package_all_versions(
    owner: str,
    type: _PackageType,
    name: _PackageName,
):
    """Delete a package and every version of it.

    Wider than delete_package, which removes a single version: this drops the
    package itself together with all its versions and files. Irreversible —
    call list_package_versions first if the blast radius matters.
    """
    return _ok(
        _get_client().delete(f"/packages/{owner}/{type}/{name}")
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

@_op(gitea_write)
def link_package(
    owner: str,
    type: _PackageType,
    name: _PackageName,
    repo_name: Annotated[str, Field(description="Repository SHORT name (e.g. 'my-repo', NOT 'owner/my-repo'). Gitea resolves it under `owner`, so a repository owned by anyone else is a 404.")],
):
    """Link a package to a repository so it appears on that repo's packages tab.

    The link lives on the package, not on a version, so it covers every
    version at once. A package carries at most one link; calling this again
    repoints it. Takes no request body.
    """
    return _ok(
        _get_client().post(f"/packages/{owner}/{type}/{name}/-/link/{repo_name}")
    )

@_op(gitea_write)
def unlink_package(
    owner: str,
    type: _PackageType,
    name: _PackageName,
):
    """Remove a package's repository link, leaving the package itself intact.

    Reverses link_package. No repository is named because a package carries at
    most one link. The `-` in `/packages/{owner}/{type}/{name}/-/unlink` is a
    literal path segment, not a placeholder. Takes no request body and deletes
    no package data.
    """
    return _ok(
        _get_client().post(f"/packages/{owner}/{type}/{name}/-/unlink")
    )

# ── Admin ────────────────────────────────────────────────────────────────────

_BadgeSlugs = Annotated[list[str], Field(description="Badge SLUGS (the badge's short string key, e.g. ['contributor', 'early-adopter']) — NOT badge IDs or display names. Gitea wraps them in a {'badge_slugs': [...]} body; pass the bare list here. Existing slugs for a user come from admin_list_user_badges.")]
_WorkflowStatusFilter = Annotated[Literal["pending", "queued", "in_progress", "failure", "success", "skipped"] | None, Field(description="Filter by run/job status. Omit for every status.")]


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

@_op(gitea_admin_read)
def admin_list_hooks(
    type: Annotated[Literal["system", "default", "all"] | None, Field(description="Which instance-level webhooks to list. 'system' (server default) = hooks that fire for every repository; 'default' = the template hooks copied into each newly created repository; 'all' = both kinds.")] = None,
):
    """List the instance's system webhooks (admin only).

    These are server-wide hooks configured in site administration, not the
    per-repo hooks of list_repo_webhooks or the per-org ones of
    list_org_webhooks."""
    params = _body(locals())
    return _ok(_get_client().paginate("/admin/hooks", params=params or None))

@_op(gitea_admin_write)
def admin_create_hook(
    config: _HookConfig,
    events: _HookEvents,
    hook_type: _HookType = "gitea",
    active: bool = True,
    name: Annotated[str | None, Field(description="Human-readable label for the hook, shown in site administration. Free text; Gitea generates one when omitted.")] = None,
    branch_filter: Annotated[str | None, Field(description="Glob matched against the pushed branch name — only matching branches deliver (e.g. 'main', 'release/*', '*'). Omitted = every branch.")] = None,
    authorization_header: Annotated[str | None, Field(description="Verbatim value Gitea sends as the request's Authorization header (e.g. 'Bearer abc123'). Omit unless the receiver requires one.")] = None,
):
    """Create a system webhook that fires for every repository (admin only)."""
    return _call("POST", "/admin/hooks", locals(), rename={"hook_type": "type"})

@_op(gitea_admin_read)
def admin_get_hook(
    hook_id: Annotated[int, Field(description="System webhook ID (int64) from admin_list_hooks.")],
):
    """Get one system webhook by ID (admin only)."""
    return _ok(_get_client().get(f"/admin/hooks/{hook_id}"))

@_op(gitea_admin_write)
def admin_edit_hook(
    hook_id: Annotated[int, Field(description="System webhook ID (int64) from admin_list_hooks.")],
    config: _HookConfigPatch = None,
    events: _HookEventsPatch = None,
    active: bool | None = None,
    name: Annotated[str | None, Field(description="Replacement human-readable label for the hook.")] = None,
    branch_filter: Annotated[str | None, Field(description="Replacement branch glob (e.g. 'main', 'release/*', '*').")] = None,
    authorization_header: Annotated[str | None, Field(description="Replacement Authorization header value Gitea sends with each delivery (e.g. 'Bearer abc123').")] = None,
):
    """Update a system webhook (admin only). The hook's type cannot be changed."""
    return _call("PATCH", "/admin/hooks/{hook_id}", locals())

@_op(gitea_admin_write)
def admin_delete_hook(
    hook_id: Annotated[int, Field(description="System webhook ID (int64) from admin_list_hooks.")],
):
    """Delete a system webhook (admin only)."""
    return _ok(_get_client().delete(f"/admin/hooks/{hook_id}"))

@_op(gitea_admin_read)
def admin_list_user_badges(username: str):
    """List the badges granted to a user (admin only)."""
    return _ok(_get_client().get(f"/admin/users/{username}/badges"))

@_op(gitea_admin_write)
def admin_add_user_badges(
    username: str,
    badge_slugs: _BadgeSlugs,
):
    """Grant badges to a user (admin only)."""
    return _ok(
        _get_client().post(
            f"/admin/users/{username}/badges",
            json={"badge_slugs": badge_slugs},
        )
    )

@_op(gitea_admin_write)
def admin_delete_user_badges(
    username: str,
    badge_slugs: _BadgeSlugs,
):
    """Revoke badges from a user (admin only).

    Unlinks the badges from the user; the badge definitions themselves stay
    on the instance. Gitea takes the list in a request body, not the query."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/admin/users/{username}/badges",
            json={"badge_slugs": badge_slugs},
        )
    )

@_op(gitea_admin_write)
def admin_update_runner(
    runner_id: Annotated[int, Field(description="Global action-runner ID (int64) from list_admin_runners.")],
    disabled: Annotated[bool, Field(description="True = stop scheduling jobs onto this runner (it stays registered); False = re-enable it. Required — Gitea rejects a body without it.")],
):
    """Enable or disable a global action runner (admin only)."""
    return _call("PATCH", "/admin/actions/runners/{runner_id}", locals())

@_op(gitea_admin_read)
def admin_list_workflow_runs(
    event: Annotated[str | None, Field(description="Filter by the event that triggered the run, as named in the workflow's `on:` block (e.g. 'push', 'pull_request', 'workflow_dispatch', 'schedule').")] = None,
    branch: Annotated[str | None, Field(description="Filter by the run's head branch name (no 'refs/heads/' prefix), e.g. 'main'.")] = None,
    status: _WorkflowStatusFilter = None,
    actor: Annotated[str | None, Field(description="Filter by the USERNAME that triggered the run (not a display name or user ID).")] = None,
    head_sha: Annotated[str | None, Field(description="Filter by the full 40-character commit SHA the run was triggered on.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea workflow-run objects.")] = True,
):
    """List workflow runs across every repository on the instance (admin only).

    brief (default True): compact view — id, title, status, conclusion,
    event, branch, sha, run_number, path, timestamps.
    Set brief=False for full Gitea API response objects."""
    params = _body(locals(), exclude=("brief",))
    data = _get_client().get("/admin/actions/runs", params=params or None)
    if brief:
        data = _slim_workflow_runs(data)
    return _ok(data)

@_op(gitea_admin_read)
def admin_list_workflow_jobs(
    status: _WorkflowStatusFilter = None,
    sort: Annotated[Literal["id"] | None, Field(description="Sort field. 'id' is the only value Gitea supports here, and is also the default.")] = None,
    order: Annotated[Literal["asc", "desc"] | None, Field(description="Sort direction. Defaults to 'asc' — pass 'desc' for the most recent jobs first.")] = None,
    limit: Annotated[int | None, Field(description="Page size. Server default if omitted.")] = 20,
    page: Annotated[int | None, Field(description="1-based page number.")] = 1,
    brief: Annotated[bool, Field(description="True (default) = compact slim view; False = full Gitea workflow-job objects.")] = True,
):
    """List workflow jobs across every repository on the instance (admin only).

    brief (default True): compact view — id, name, status, conclusion,
    run_id, runner, timestamps. Set brief=False for full Gitea objects."""
    params = _body(locals(), exclude=("brief",))
    data = _get_client().get("/admin/actions/jobs", params=params or None)
    if brief:
        data = _slim_jobs(data)
    return _ok(data)

# ── Actions Runners ──────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_runners(owner: str, repo: str):
    """List action runners for a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/actions/runners"))

@_op(gitea_read)
def get_repo_runner(owner: str, repo: str, runner_id: int):
    """Get an action runner for a repository."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/actions/runners/{runner_id}"))

@_op(gitea_write)
def update_repo_runner(
    owner: str,
    repo: str,
    runner_id: Annotated[int, Field(description="Runner ID from list_repo_runners (NOT the runner name).")],
    disabled: Annotated[bool, Field(description="True takes the runner out of rotation — it keeps its registration but is handed no new jobs. False puts it back.")],
):
    """Enable or disable a repo-level action runner."""
    return _call("PATCH", "/repos/{owner}/{repo}/actions/runners/{runner_id}", locals())

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

@_op(gitea_write)
def render_markdown_raw(
    text: Annotated[str, Field(description="Raw Markdown source, sent verbatim as a text/plain request body.")],
):
    """Render a raw markdown document. Returns HTML text.

    The bare-body sibling of render_markdown: no mode and no repo context, so
    relative links and #N issue references are not resolved. Use
    render_markdown when either matters."""
    return _get_client().post_text("/markdown/raw", text, "text/plain")

@_op(gitea_write)
def render_markup(
    text: Annotated[str, Field(description="Raw markup source to render.")],
    mode: Annotated[Literal["markdown", "comment", "wiki", "file"] | None, Field(description="Markup format / render context. 'file' picks the renderer from `file_path`'s extension, which is how non-Markdown markup (Org, AsciiDoc, ...) is rendered.")] = None,
    context: Annotated[str | None, Field(description="URL path used to resolve relative links and media, e.g. '/owner/repo/src/branch/main'.")] = None,
    file_path: Annotated[str | None, Field(description="File path whose extension selects the renderer when mode='file', e.g. 'docs/README.org'.")] = None,
    wiki: Annotated[bool | None, Field(description="Deprecated in the Gitea API — use mode='wiki' instead. True = treat input as a wiki page.")] = None,
):
    """Render a markup document. Returns HTML text.

    Unlike render_markdown this can render any markup format the instance
    supports, chosen by `mode` (or by `file_path`'s extension with
    mode='file')."""
    body: dict = {"Text": text}
    if mode is not None:
        body["Mode"] = mode
    if context is not None:
        body["Context"] = context
    if file_path is not None:
        body["FilePath"] = file_path
    if wiki is not None:
        body["Wiki"] = wiki
    return _get_client()._text("POST", "/markup", json=body)

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
def get_signing_key_ssh():
    """Get the instance's default SSH signing key. Returns text/plain.

    The OpenSSH public key Gitea signs commits with when SSH signing is
    configured; get_signing_key is the GPG counterpart. An instance with no
    SSH signing key configured answers 404 or an empty body."""
    return _get_client().get_text("/signing-key.pub")

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
def get_repo_by_id(
    repo_id: Annotated[int, Field(description="Numeric repository ID (int64), as it appears in the `id` field of any repo object — NOT 'owner/repo'.")],
):
    """Get a repository by its numeric ID.

    The lookup for when you hold an ID from a webhook payload, notification,
    or search result and do not know the owner/name pair get_repo needs."""
    return _call("GET", "/repositories/{repo_id}", locals())

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
    client = _get_client()
    if ref_type:
        return _ok(client.get(f"/repos/{owner}/{repo}/git/refs/{ref_type}"))
    return _ok(client.get(f"/repos/{owner}/{repo}/git/refs"))

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
