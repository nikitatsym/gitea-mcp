"""Gitea tool operations. All public functions are auto-registered as MCP tools."""

import base64
import re
from typing import Optional

from .client import GiteaClient
from .prepare import (
    _ok, _validate_brief, _enforce_private, _enforce_visibility,
    _slim_issues, _slim_repos, _slim_notifications, _slim_comments,
    _slim_commits, _slim_workflow_run, _slim_workflow_runs, _slim_job, _slim_jobs,
)
from .registry import Group, _op

_client: Optional[GiteaClient] = None


def _get_client() -> GiteaClient:
    global _client
    if _client is None:
        _client = GiteaClient()
    return _client


# ── Groups ────────────────────────────────────────────────────────────────────

gitea_read = Group("gitea_read", "Gitea read operations (GET). operation=\"help\" to list.")
gitea_create = Group("gitea_create", "Gitea create operations (POST). operation=\"help\" to list.")
gitea_update = Group("gitea_update", "Gitea update operations (PUT/PATCH). operation=\"help\" to list.")
gitea_delete = Group("gitea_delete", "Gitea delete operations (DELETE). operation=\"help\" to list.")
gitea_admin_read = Group("gitea_admin_read", "Gitea admin read operations (GET /admin/*). operation=\"help\" to list.")
gitea_admin_write = Group("gitea_admin_write", "Gitea admin write operations (POST/PUT/PATCH/DELETE /admin/*). operation=\"help\" to list.")

# ── General ──────────────────────────────────────────────────────────────────


@_op(gitea_read)
def get_version():
    """Get the Gitea server version."""
    return _ok(_get_client().get("/version"))

@_op(gitea_read)
def get_current_user():
    """Get the currently authenticated user."""
    return _ok(_get_client().get("/user"))

# ── Users ────────────────────────────────────────────────────────────────────


@_op(gitea_read)
def search_users(
    query: str,
    limit: Optional[int] = None,
    page: Optional[int] = None,
):
    """Search for users by keyword."""
    params: dict = {"q": query}
    if limit is not None:
        params["limit"] = limit
    if page is not None:
        params["page"] = page
    return _ok(_get_client().get("/users/search", params=params))

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

@_op(gitea_update)
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

@_op(gitea_create)
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

@_op(gitea_create)
def create_oauth2_app(
    name: str,
    redirect_uris: list[str],
    confidential_client: Optional[bool] = None,
):
    """Create an OAuth2 application for the current user."""
    body: dict = {"name": name, "redirect_uris": redirect_uris}
    if confidential_client is not None:
        body["confidential_client"] = confidential_client
    return _ok(_get_client().post("/user/applications/oauth2", json=body))

@_op(gitea_read)
def get_oauth2_app(app_id: int):
    """Get an OAuth2 application by ID."""
    return _ok(_get_client().get(f"/user/applications/oauth2/{app_id}"))

@_op(gitea_update)
def edit_oauth2_app(
    app_id: int,
    name: Optional[str] = None,
    redirect_uris: Optional[list[str]] = None,
    confidential_client: Optional[bool] = None,
):
    """Edit an OAuth2 application."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if redirect_uris is not None:
        body["redirect_uris"] = redirect_uris
    if confidential_client is not None:
        body["confidential_client"] = confidential_client
    return _ok(
        _get_client().patch(f"/user/applications/oauth2/{app_id}", json=body)
    )

@_op(gitea_delete)
def delete_oauth2_app(app_id: int):
    """Delete an OAuth2 application."""
    return _ok(_get_client().delete(f"/user/applications/oauth2/{app_id}"))

@_op(gitea_read)
def list_blocked_users():
    """List users blocked by the current user."""
    return _ok(_get_client().paginate("/user/blocks"))

@_op(gitea_update)
def block_user(username: str):
    """Block a user."""
    return _ok(_get_client().put(f"/user/blocks/{username}"))

@_op(gitea_delete)
def unblock_user(username: str):
    """Unblock a user."""
    return _ok(_get_client().delete(f"/user/blocks/{username}"))

@_op(gitea_update)
def update_user_settings(
    description: Optional[str] = None,
    full_name: Optional[str] = None,
    location: Optional[str] = None,
    website: Optional[str] = None,
    language: Optional[str] = None,
    hide_email: Optional[bool] = None,
    hide_activity: Optional[bool] = None,
    theme: Optional[str] = None,
    diff_view_style: Optional[str] = None,
):
    """Update the current user's settings."""
    body: dict = {}
    if description is not None:
        body["description"] = description
    if full_name is not None:
        body["full_name"] = full_name
    if location is not None:
        body["location"] = location
    if website is not None:
        body["website"] = website
    if language is not None:
        body["language"] = language
    if hide_email is not None:
        body["hide_email"] = hide_email
    if hide_activity is not None:
        body["hide_activity"] = hide_activity
    if theme is not None:
        body["theme"] = theme
    if diff_view_style is not None:
        body["diff_view_style"] = diff_view_style
    return _ok(_get_client().patch("/user/settings", json=body))

# ── SSH / GPG Keys ──────────────────────────────────────────────────────────


@_op(gitea_read)
def list_ssh_keys():
    """List the current user's SSH keys."""
    return _ok(_get_client().paginate("/user/keys"))

@_op(gitea_create)
def create_ssh_key(title: str, key: str):
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

@_op(gitea_create)
def create_gpg_key(armored_public_key: str):
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
    topic: Optional[bool] = None,
    sort: Optional[str] = None,
    order: Optional[str] = None,
    limit: Optional[int] = 20,
    page: Optional[int] = None,
    brief: bool = True,
):
    """Search for repositories by keyword.

    brief (default True): compact view — full_name, description, language,
    stars, issues count, default_branch, updated_at.
    Set brief=False for full Gitea API response objects."""
    params: dict = {"q": query, "limit": limit}
    if topic is not None:
        params["topic"] = topic
    if sort is not None:
        params["sort"] = sort
    if order is not None:
        params["order"] = order
    if page is not None:
        params["page"] = page
    data = _get_client().get("/repos/search", params=params)
    if isinstance(data, dict) and "ok" in data and "data" in data:
        data = data["data"]
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_create)
def create_repo(
    name: str,
    description: Optional[str] = None,
    private: Optional[bool] = None,
    auto_init: Optional[bool] = None,
    gitignores: Optional[str] = None,
    license: Optional[str] = None,
    readme: Optional[str] = None,
    default_branch: Optional[str] = None,
):
    """Create a new repository for the authenticated user."""
    private = _enforce_private(private)
    body: dict = {"name": name}
    if description is not None:
        body["description"] = description
    if private is not None:
        body["private"] = private
    if auto_init is not None:
        body["auto_init"] = auto_init
    if gitignores is not None:
        body["gitignores"] = gitignores
    if license is not None:
        body["license"] = license
    if readme is not None:
        body["readme"] = readme
    if default_branch is not None:
        body["default_branch"] = default_branch
    return _ok(_get_client().post("/user/repos", json=body))

@_op(gitea_read)
def get_repo(owner: str, repo: str):
    """Get a repository by owner and name."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}"))

@_op(gitea_update)
def edit_repo(
    owner: str,
    repo: str,
    name: Optional[str] = None,
    description: Optional[str] = None,
    website: Optional[str] = None,
    private: Optional[bool] = None,
    has_issues: Optional[bool] = None,
    has_wiki: Optional[bool] = None,
    has_pull_requests: Optional[bool] = None,
    default_branch: Optional[str] = None,
    archived: Optional[bool] = None,
):
    """Edit a repository's properties."""
    private = _enforce_private(private)
    body: dict = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if website is not None:
        body["website"] = website
    if private is not None:
        body["private"] = private
    if has_issues is not None:
        body["has_issues"] = has_issues
    if has_wiki is not None:
        body["has_wiki"] = has_wiki
    if has_pull_requests is not None:
        body["has_pull_requests"] = has_pull_requests
    if default_branch is not None:
        body["default_branch"] = default_branch
    if archived is not None:
        body["archived"] = archived
    return _ok(_get_client().patch(f"/repos/{owner}/{repo}", json=body))

@_op(gitea_delete)
def delete_repo(owner: str, repo: str):
    """Delete a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}"))

@_op(gitea_create)
def fork_repo(
    owner: str,
    repo: str,
    organization: Optional[str] = None,
    name: Optional[str] = None,
):
    """Fork a repository."""
    body: dict = {}
    if organization is not None:
        body["organization"] = organization
    if name is not None:
        body["name"] = name
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/forks", json=body))

@_op(gitea_read)
def list_forks(owner: str, repo: str, brief: bool = True):
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

@_op(gitea_update)
def set_repo_topics(owner: str, repo: str, topics: list[str]):
    """Set a repository's topics, replacing all existing ones."""
    return _ok(
        _get_client().put(f"/repos/{owner}/{repo}/topics", json={"topics": topics})
    )

@_op(gitea_read)
def list_repo_collaborators(owner: str, repo: str):
    """List a repository's collaborators."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/collaborators"))

@_op(gitea_update)
def add_repo_collaborator(
    owner: str, repo: str, collaborator: str, permission: str
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

@_op(gitea_update)
def star_repo(owner: str, repo: str):
    """Star a repository."""
    return _ok(_get_client().put(f"/user/starred/{owner}/{repo}"))

@_op(gitea_delete)
def unstar_repo(owner: str, repo: str):
    """Unstar a repository."""
    return _ok(_get_client().delete(f"/user/starred/{owner}/{repo}"))

@_op(gitea_read)
def list_my_starred_repos(brief: bool = True):
    """List repositories starred by the current user.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate("/user/starred")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_update)
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
def list_my_subscriptions(brief: bool = True):
    """List repositories watched by the current user.

    brief (default True): compact view. Set brief=False for full objects."""
    data = _get_client().paginate("/user/subscriptions")
    if brief:
        data = _slim_repos(data)
    return _ok(data)

@_op(gitea_update)
def watch_repo(owner: str, repo: str):
    """Watch a repository."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/subscription", json={"subscribed": True}
        )
    )

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

# ── Webhooks ─────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_webhooks(owner: str, repo: str):
    """List a repository's webhooks."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/hooks"))

@_op(gitea_create)
def create_repo_webhook(
    owner: str,
    repo: str,
    config: dict,
    events: list[str],
    hook_type: str = "gitea",
    active: bool = True,
):
    """Create a webhook for a repository."""
    body: dict = {
        "type": hook_type,
        "config": config,
        "events": events,
        "active": active,
    }
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/hooks", json=body))

@_op(gitea_update)
def edit_repo_webhook(
    owner: str,
    repo: str,
    hook_id: int,
    config: Optional[dict] = None,
    events: Optional[list[str]] = None,
    active: Optional[bool] = None,
):
    """Edit a repository webhook."""
    body: dict = {}
    if config is not None:
        body["config"] = config
    if events is not None:
        body["events"] = events
    if active is not None:
        body["active"] = active
    return _ok(
        _get_client().patch(f"/repos/{owner}/{repo}/hooks/{hook_id}", json=body)
    )

@_op(gitea_delete)
def delete_repo_webhook(owner: str, repo: str, hook_id: int):
    """Delete a repository webhook."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/hooks/{hook_id}"))

@_op(gitea_create)
def test_repo_webhook(owner: str, repo: str, hook_id: int):
    """Test a repository webhook."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/hooks/{hook_id}/tests"))

# ── Org Webhooks ─────────────────────────────────────────────────────────


@_op(gitea_read)
def list_org_webhooks(org: str):
    """List webhooks for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/hooks"))

@_op(gitea_create)
def create_org_webhook(
    org: str,
    config: dict,
    events: list[str],
    hook_type: str = "gitea",
    active: bool = True,
):
    """Create a webhook for an organization."""
    body: dict = {
        "type": hook_type,
        "config": config,
        "events": events,
        "active": active,
    }
    return _ok(_get_client().post(f"/orgs/{org}/hooks", json=body))

@_op(gitea_update)
def edit_org_webhook(
    org: str,
    hook_id: int,
    config: Optional[dict] = None,
    events: Optional[list[str]] = None,
    active: Optional[bool] = None,
):
    """Edit an organization webhook."""
    body: dict = {}
    if config is not None:
        body["config"] = config
    if events is not None:
        body["events"] = events
    if active is not None:
        body["active"] = active
    return _ok(_get_client().patch(f"/orgs/{org}/hooks/{hook_id}", json=body))

@_op(gitea_delete)
def delete_org_webhook(org: str, hook_id: int):
    """Delete an organization webhook."""
    return _ok(_get_client().delete(f"/orgs/{org}/hooks/{hook_id}"))

# ── Deploy Keys ──────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_deploy_keys(owner: str, repo: str):
    """List a repository's deploy keys."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/keys"))

@_op(gitea_create)
def create_deploy_key(
    owner: str,
    repo: str,
    title: str,
    key: str,
    read_only: bool = True,
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
    ref: Optional[str] = None,
):
    """Get the metadata and content of a file in a repository."""
    params: dict = {}
    if ref is not None:
        params["ref"] = ref
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/contents/{filepath}", params=params or None
        )
    )

@_op(gitea_create)
def create_file(
    owner: str,
    repo: str,
    filepath: str,
    content: str,
    message: str,
    branch: Optional[str] = None,
    new_branch: Optional[str] = None,
    author_name: Optional[str] = None,
    author_email: Optional[str] = None,
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

@_op(gitea_update)
def update_file(
    owner: str,
    repo: str,
    filepath: str,
    content: str,
    message: str,
    sha: str,
    branch: Optional[str] = None,
    new_branch: Optional[str] = None,
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
    message: str,
    sha: str,
    branch: Optional[str] = None,
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
    dirpath: str = "",
    ref: Optional[str] = None,
):
    """Get the contents of a directory in a repository."""
    params: dict = {}
    if ref is not None:
        params["ref"] = ref
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/contents/{dirpath}", params=params or None
        )
    )

@_op(gitea_read)
def get_raw_file(
    owner: str,
    repo: str,
    filepath: str,
    ref: Optional[str] = None,
):
    """Get the raw content of a file in a repository."""
    params: dict = {}
    if ref is not None:
        params["ref"] = ref
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

@_op(gitea_create)
def create_branch(
    owner: str,
    repo: str,
    new_branch_name: str,
    old_branch_name: Optional[str] = None,
    old_ref_name: Optional[str] = None,
):
    """Create a new branch in a repository."""
    body: dict = {"new_branch_name": new_branch_name}
    if old_branch_name is not None:
        body["old_branch_name"] = old_branch_name
    if old_ref_name is not None:
        body["old_ref_name"] = old_ref_name
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/branches", json=body))

@_op(gitea_delete)
def delete_branch(owner: str, repo: str, branch: str):
    """Delete a branch from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/branches/{branch}"))

@_op(gitea_read)
def list_branch_protections(owner: str, repo: str):
    """List branch protections for a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/branch_protections"))

@_op(gitea_create)
def create_branch_protection(
    owner: str,
    repo: str,
    branch_name: str,
    enable_push: Optional[bool] = None,
    enable_push_whitelist: Optional[bool] = None,
    push_whitelist_usernames: Optional[list[str]] = None,
    enable_merge_whitelist: Optional[bool] = None,
    merge_whitelist_usernames: Optional[list[str]] = None,
    required_approvals: Optional[int] = None,
    enable_status_check: Optional[bool] = None,
    status_check_contexts: Optional[list[str]] = None,
):
    """Create a branch protection rule for a repository."""
    body: dict = {"branch_name": branch_name}
    if enable_push is not None:
        body["enable_push"] = enable_push
    if enable_push_whitelist is not None:
        body["enable_push_whitelist"] = enable_push_whitelist
    if push_whitelist_usernames is not None:
        body["push_whitelist_usernames"] = push_whitelist_usernames
    if enable_merge_whitelist is not None:
        body["enable_merge_whitelist"] = enable_merge_whitelist
    if merge_whitelist_usernames is not None:
        body["merge_whitelist_usernames"] = merge_whitelist_usernames
    if required_approvals is not None:
        body["required_approvals"] = required_approvals
    if enable_status_check is not None:
        body["enable_status_check"] = enable_status_check
    if status_check_contexts is not None:
        body["status_check_contexts"] = status_check_contexts
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/branch_protections", json=body)
    )

@_op(gitea_read)
def get_branch_protection(owner: str, repo: str, name: str):
    """Get a branch protection rule by name."""
    return _ok(
        _get_client().get(f"/repos/{owner}/{repo}/branch_protections/{name}")
    )

@_op(gitea_update)
def edit_branch_protection(
    owner: str,
    repo: str,
    name: str,
    enable_push: Optional[bool] = None,
    enable_push_whitelist: Optional[bool] = None,
    push_whitelist_usernames: Optional[list[str]] = None,
    enable_merge_whitelist: Optional[bool] = None,
    merge_whitelist_usernames: Optional[list[str]] = None,
    required_approvals: Optional[int] = None,
    enable_status_check: Optional[bool] = None,
    status_check_contexts: Optional[list[str]] = None,
):
    """Edit a branch protection rule."""
    body: dict = {}
    if enable_push is not None:
        body["enable_push"] = enable_push
    if enable_push_whitelist is not None:
        body["enable_push_whitelist"] = enable_push_whitelist
    if push_whitelist_usernames is not None:
        body["push_whitelist_usernames"] = push_whitelist_usernames
    if enable_merge_whitelist is not None:
        body["enable_merge_whitelist"] = enable_merge_whitelist
    if merge_whitelist_usernames is not None:
        body["merge_whitelist_usernames"] = merge_whitelist_usernames
    if required_approvals is not None:
        body["required_approvals"] = required_approvals
    if enable_status_check is not None:
        body["enable_status_check"] = enable_status_check
    if status_check_contexts is not None:
        body["status_check_contexts"] = status_check_contexts
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/branch_protections/{name}", json=body
        )
    )

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

@_op(gitea_create)
def create_tag_protection(
    owner: str,
    repo: str,
    name_pattern: str,
    whitelist_usernames: Optional[list[str]] = None,
    whitelist_teams: Optional[list[str]] = None,
):
    """Create a tag protection rule for a repository."""
    body: dict = {"name_pattern": name_pattern}
    if whitelist_usernames is not None:
        body["whitelist_usernames"] = whitelist_usernames
    if whitelist_teams is not None:
        body["whitelist_teams"] = whitelist_teams
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/tag_protections", json=body)
    )

@_op(gitea_read)
def get_tag_protection(owner: str, repo: str, tag_protection_id: int):
    """Get a tag protection rule by ID."""
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"
        )
    )

@_op(gitea_update)
def edit_tag_protection(
    owner: str,
    repo: str,
    tag_protection_id: int,
    name_pattern: Optional[str] = None,
    whitelist_usernames: Optional[list[str]] = None,
    whitelist_teams: Optional[list[str]] = None,
):
    """Edit a tag protection rule."""
    body: dict = {}
    if name_pattern is not None:
        body["name_pattern"] = name_pattern
    if whitelist_usernames is not None:
        body["whitelist_usernames"] = whitelist_usernames
    if whitelist_teams is not None:
        body["whitelist_teams"] = whitelist_teams
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/tag_protections/{tag_protection_id}", json=body
        )
    )

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
    sha: Optional[str] = None,
    path: Optional[str] = None,
    stat: Optional[bool] = None,
    limit: Optional[int] = 20,
    page: Optional[int] = None,
    brief: bool = True,
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

@_op(gitea_create)
def create_commit_status(
    owner: str,
    repo: str,
    sha: str,
    state: str,
    target_url: Optional[str] = None,
    description: Optional[str] = None,
    context: Optional[str] = None,
):
    """Create a commit status. State must be one of: pending, success, error, failure, warning."""
    body: dict = {"state": state}
    if target_url is not None:
        body["target_url"] = target_url
    if description is not None:
        body["description"] = description
    if context is not None:
        body["context"] = context
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/statuses/{sha}", json=body)
    )

@_op(gitea_read)
def get_combined_commit_status(owner: str, repo: str, ref: str):
    """Get the combined status for a commit ref."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/commits/{ref}/status"))

# ── Tags and Releases ───────────────────────────────────────────────────────


@_op(gitea_read)
def list_tags(owner: str, repo: str):
    """List a repository's tags."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/tags"))

@_op(gitea_create)
def create_tag(
    owner: str,
    repo: str,
    tag_name: str,
    target: Optional[str] = None,
    message: Optional[str] = None,
):
    """Create a new tag in a repository."""
    body: dict = {"tag_name": tag_name}
    if target is not None:
        body["target"] = target
    if message is not None:
        body["message"] = message
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/tags", json=body))

@_op(gitea_delete)
def delete_tag(owner: str, repo: str, tag: str):
    """Delete a tag from a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/tags/{tag}"))

@_op(gitea_read)
def list_releases(owner: str, repo: str, brief: bool = True):
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
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/releases/{release_id}"))

@_op(gitea_create)
def create_release(
    owner: str,
    repo: str,
    tag_name: str,
    target_commitish: Optional[str] = None,
    name: Optional[str] = None,
    body: Optional[str] = None,
    draft: Optional[bool] = None,
    prerelease: Optional[bool] = None,
):
    """Create a new release in a repository."""
    payload: dict = {"tag_name": tag_name}
    if target_commitish is not None:
        payload["target_commitish"] = target_commitish
    if name is not None:
        payload["name"] = name
    if body is not None:
        payload["body"] = body
    if draft is not None:
        payload["draft"] = draft
    if prerelease is not None:
        payload["prerelease"] = prerelease
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/releases", json=payload))

@_op(gitea_update)
def edit_release(
    owner: str,
    repo: str,
    release_id: int,
    tag_name: Optional[str] = None,
    target_commitish: Optional[str] = None,
    name: Optional[str] = None,
    body: Optional[str] = None,
    draft: Optional[bool] = None,
    prerelease: Optional[bool] = None,
):
    """Edit a release."""
    payload: dict = {}
    if tag_name is not None:
        payload["tag_name"] = tag_name
    if target_commitish is not None:
        payload["target_commitish"] = target_commitish
    if name is not None:
        payload["name"] = name
    if body is not None:
        payload["body"] = body
    if draft is not None:
        payload["draft"] = draft
    if prerelease is not None:
        payload["prerelease"] = prerelease
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/releases/{release_id}", json=payload
        )
    )

@_op(gitea_delete)
def delete_release(owner: str, repo: str, release_id: int):
    """Delete a release by ID."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/releases/{release_id}"))

# ── Labels ───────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_repo_labels(owner: str, repo: str):
    """List a repository's labels."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/labels"))

@_op(gitea_create)
def create_repo_label(
    owner: str,
    repo: str,
    name: str,
    color: str,
    description: Optional[str] = None,
):
    """Create a label in a repository."""
    body: dict = {"name": name, "color": color}
    if description is not None:
        body["description"] = description
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/labels", json=body))

@_op(gitea_update)
def edit_repo_label(
    owner: str,
    repo: str,
    label_id: int,
    name: Optional[str] = None,
    color: Optional[str] = None,
    description: Optional[str] = None,
):
    """Edit a repository label."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if color is not None:
        body["color"] = color
    if description is not None:
        body["description"] = description
    return _ok(
        _get_client().patch(f"/repos/{owner}/{repo}/labels/{label_id}", json=body)
    )

@_op(gitea_delete)
def delete_repo_label(owner: str, repo: str, label_id: int):
    """Delete a repository label."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/labels/{label_id}"))

# ── Milestones ───────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_milestones(
    owner: str,
    repo: str,
    state: Optional[str] = None,
):
    """List a repository's milestones. State can be open, closed, or all."""
    params: dict = {}
    if state is not None:
        params["state"] = state
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/milestones", params=params or None)
    )

@_op(gitea_read)
def get_milestone(owner: str, repo: str, milestone_id: int):
    """Get a milestone by ID."""
    return _ok(
        _get_client().get(f"/repos/{owner}/{repo}/milestones/{milestone_id}")
    )

@_op(gitea_create)
def create_milestone(
    owner: str,
    repo: str,
    title: str,
    description: Optional[str] = None,
    due_on: Optional[str] = None,
    state: Optional[str] = None,
):
    """Create a milestone in a repository."""
    body: dict = {"title": title}
    if description is not None:
        body["description"] = description
    if due_on is not None:
        body["due_on"] = due_on
    if state is not None:
        body["state"] = state
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/milestones", json=body))

@_op(gitea_update)
def edit_milestone(
    owner: str,
    repo: str,
    milestone_id: int,
    title: Optional[str] = None,
    description: Optional[str] = None,
    due_on: Optional[str] = None,
    state: Optional[str] = None,
):
    """Edit a milestone."""
    body: dict = {}
    if title is not None:
        body["title"] = title
    if description is not None:
        body["description"] = description
    if due_on is not None:
        body["due_on"] = due_on
    if state is not None:
        body["state"] = state
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/milestones/{milestone_id}", json=body
        )
    )

@_op(gitea_delete)
def delete_milestone(owner: str, repo: str, milestone_id: int):
    """Delete a milestone."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/milestones/{milestone_id}")
    )

# ── Issues ───────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issues(
    owner: str,
    repo: str,
    state: Optional[str] = None,
    labels: Optional[str] = None,
    milestone: Optional[str] = None,
    assignee: Optional[str] = None,
    type: Optional[str] = None,
    page: Optional[int] = None,
    limit: Optional[int] = 20,
    brief: bool = True,
):
    """List issues in a repository. Type can be 'issues' or 'pulls'.

    brief (default True): compact view — number, title, state, labels, assignee,
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
        params["milestone"] = milestone
    if assignee is not None:
        params["assignee"] = assignee
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
    query: str,
    owner: Optional[str] = None,
    state: Optional[str] = None,
    labels: Optional[str] = None,
    type: Optional[str] = None,
    limit: Optional[int] = 20,
    page: Optional[int] = None,
    brief: bool = True,
):
    """Search issues across repositories.

    brief (default True): compact view — number, title, state, labels, assignee,
    updated_at, and body summary extracted from a <brief>...</brief> tag.
    If brief is null for an issue, use get_issue for full details or edit_issue
    to add <brief>short summary</brief> to its body for convenient list views.
    Set brief=False for full Gitea API response objects."""
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
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/issues/{index}"))

@_op(gitea_create)
def create_issue(
    owner: str,
    repo: str,
    title: str,
    body: Optional[str] = None,
    assignees: Optional[list[str]] = None,
    milestone_id: Optional[int] = None,
    labels: Optional[list[int]] = None,
):
    """Create an issue in a repository. Body must include <brief>summary</brief> tag."""
    _validate_brief(body)
    payload: dict = {"title": title}
    if body is not None:
        payload["body"] = body
    if assignees is not None:
        payload["assignees"] = assignees
    if milestone_id is not None:
        payload["milestone"] = milestone_id
    if labels is not None:
        payload["labels"] = labels
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/issues", json=payload))

@_op(gitea_update)
def edit_issue(
    owner: str,
    repo: str,
    index: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
    assignees: Optional[list[str]] = None,
    milestone: Optional[int] = None,
    labels: Optional[list[int]] = None,
    due_date: Optional[str] = None,
):
    """Edit an issue. State can be 'open' or 'closed'. Body must include <brief>summary</brief> tag."""
    if body is not None:
        _validate_brief(body)
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state
    if assignees is not None:
        payload["assignees"] = assignees
    if milestone is not None:
        payload["milestone"] = milestone
    if labels is not None:
        payload["labels"] = labels
    if due_date is not None:
        payload["due_date"] = due_date
    return _ok(
        _get_client().patch(f"/repos/{owner}/{repo}/issues/{index}", json=payload)
    )

@_op(gitea_read)
def list_issue_comments(owner: str, repo: str, index: int, brief: bool = True):
    """List comments on an issue.

    brief (default True): compact view — id, user login, body, timestamps.
    Set brief=False for full objects."""
    data = _get_client().paginate(f"/repos/{owner}/{repo}/issues/{index}/comments")
    if brief:
        data = _slim_comments(data)
    return _ok(data)

@_op(gitea_create)
def create_issue_comment(owner: str, repo: str, index: int, body: str):
    """Create a comment on an issue."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/comments", json={"body": body}
        )
    )

@_op(gitea_update)
def edit_issue_comment(owner: str, repo: str, comment_id: int, body: str):
    """Edit a comment on an issue."""
    return _ok(
        _get_client().patch(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}", json={"body": body}
        )
    )

@_op(gitea_delete)
def delete_issue_comment(owner: str, repo: str, comment_id: int):
    """Delete a comment on an issue."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/issues/comments/{comment_id}")
    )

@_op(gitea_read)
def list_issue_labels(owner: str, repo: str, index: int):
    """List labels on an issue."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/issues/{index}/labels"))

@_op(gitea_create)
def add_issue_labels(owner: str, repo: str, index: int, labels: list[int]):
    """Add labels to an issue."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/labels", json={"labels": labels}
        )
    )

@_op(gitea_delete)
def remove_issue_label(owner: str, repo: str, index: int, label_id: int):
    """Remove a label from an issue."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/issues/{index}/labels/{label_id}"
        )
    )

@_op(gitea_update)
def replace_issue_labels(
    owner: str, repo: str, index: int, labels: list[int]
):
    """Replace all labels on an issue."""
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/issues/{index}/labels", json={"labels": labels}
        )
    )

@_op(gitea_create)
def set_issue_deadline(owner: str, repo: str, index: int, due_date: str):
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
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/issues/{index}/deadline")
    )

@_op(gitea_delete)
def clear_issue_labels(owner: str, repo: str, index: int):
    """Remove all labels from an issue."""
    return _ok(
        _get_client().delete(f"/repos/{owner}/{repo}/issues/{index}/labels")
    )

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
    since: Optional[str] = None,
    before: Optional[str] = None,
    brief: bool = True,
):
    """List all comments in a repository (across all issues).

    brief (default True): compact view — id, user login, body, timestamps.
    Set brief=False for full objects."""
    params: dict = {}
    if since is not None:
        params["since"] = since
    if before is not None:
        params["before"] = before
    data = _get_client().paginate(
        f"/repos/{owner}/{repo}/issues/comments", params=params or None
    )
    if brief:
        data = _slim_comments(data)
    return _ok(data)

@_op(gitea_delete)
def delete_stopwatch(owner: str, repo: str, index: int):
    """Delete a stopwatch on an issue."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/issues/{index}/stopwatch/delete"
        )
    )

# ── Issue Extended ───────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issue_dependencies(owner: str, repo: str, index: int):
    """List an issue's dependencies."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/issues/{index}/dependencies"))

@_op(gitea_create)
def add_issue_dependency(
    owner: str, repo: str, index: int, depends_on_id: int
):
    """Add a dependency to an issue. depends_on_id is the index of the dependency issue."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/dependencies",
            json={"id": depends_on_id},
        )
    )

@_op(gitea_delete)
def remove_issue_dependency(
    owner: str, repo: str, index: int, depends_on_id: int
):
    """Remove a dependency from an issue."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/issues/{index}/dependencies",
            json={"id": depends_on_id},
        )
    )

@_op(gitea_create)
def pin_issue(owner: str, repo: str, index: int):
    """Pin an issue in a repository."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/issues/{index}/pin"))

@_op(gitea_delete)
def unpin_issue(owner: str, repo: str, index: int):
    """Unpin an issue in a repository."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/issues/{index}/pin"))

@_op(gitea_update)
def lock_issue(owner: str, repo: str, index: int):
    """Lock an issue's conversation."""
    return _ok(_get_client().put(f"/repos/{owner}/{repo}/issues/{index}/lock", json={}))

@_op(gitea_delete)
def unlock_issue(owner: str, repo: str, index: int):
    """Unlock an issue's conversation."""
    return _ok(_get_client().delete(f"/repos/{owner}/{repo}/issues/{index}/lock"))

@_op(gitea_read)
def list_issue_subscriptions(owner: str, repo: str, index: int):
    """List users subscribed to an issue."""
    return _ok(
        _get_client().get(f"/repos/{owner}/{repo}/issues/{index}/subscriptions")
    )

@_op(gitea_update)
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
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/issues/{index}/subscriptions/{user}"
        )
    )

# ── Reactions ────────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_issue_reactions(owner: str, repo: str, index: int):
    """List reactions on an issue."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/issues/{index}/reactions"))

@_op(gitea_create)
def add_issue_reaction(owner: str, repo: str, index: int, reaction: str):
    """Add a reaction to an issue. Reaction can be: +1, -1, laugh, confused, heart, hooray, rocket, eyes."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/reactions",
            json={"content": reaction},
        )
    )

@_op(gitea_delete)
def remove_issue_reaction(owner: str, repo: str, index: int, reaction: str):
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
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"
        )
    )

@_op(gitea_create)
def add_comment_reaction(
    owner: str, repo: str, comment_id: int, reaction: str
):
    """Add a reaction to a comment. Reaction can be: +1, -1, laugh, confused, heart, hooray, rocket, eyes."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions",
            json={"content": reaction},
        )
    )

@_op(gitea_delete)
def remove_comment_reaction(
    owner: str, repo: str, comment_id: int, reaction: str
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

@_op(gitea_create)
def add_tracked_time(
    owner: str,
    repo: str,
    index: int,
    time: int,
    user_name: Optional[str] = None,
    created: Optional[str] = None,
):
    """Add tracked time to an issue. Time is in seconds."""
    body: dict = {"time": time}
    if user_name is not None:
        body["user_name"] = user_name
    if created is not None:
        body["created"] = created
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/issues/{index}/times", json=body
        )
    )

@_op(gitea_delete)
def delete_tracked_time(owner: str, repo: str, index: int, time_id: int):
    """Delete a tracked time entry from an issue."""
    return _ok(
        _get_client().delete(
            f"/repos/{owner}/{repo}/issues/{index}/times/{time_id}"
        )
    )

@_op(gitea_create)
def start_stopwatch(owner: str, repo: str, index: int):
    """Start a stopwatch on an issue."""
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/issues/{index}/stopwatch/start")
    )

@_op(gitea_create)
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
    state: Optional[str] = None,
    sort: Optional[str] = None,
    milestone: Optional[int] = None,
    labels: Optional[list[int]] = None,
    brief: bool = True,
):
    """List pull requests in a repository.

    brief (default True): compact view — number, title, state, labels, assignee,
    updated_at, and body summary extracted from a <brief>...</brief> tag.
    If brief is null for a PR, use get_pull_request for full details or
    edit the PR body to add <brief>short summary</brief> for convenient list views.
    Set brief=False for full Gitea API response objects."""
    params: dict = {}
    if state is not None:
        params["state"] = state
    if sort is not None:
        params["sort"] = sort
    if milestone is not None:
        params["milestone"] = milestone
    if labels is not None:
        params["labels"] = ",".join(str(l) for l in labels)
    data = _get_client().paginate(f"/repos/{owner}/{repo}/pulls", params=params or None)
    if brief:
        data = _slim_issues(data)
    return _ok(data)

@_op(gitea_read)
def get_pull_request(owner: str, repo: str, index: int):
    """Get a pull request by index."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/pulls/{index}"))

@_op(gitea_create)
def create_pull_request(
    owner: str,
    repo: str,
    title: str,
    head: str,
    base: str,
    body: Optional[str] = None,
    assignees: Optional[list[str]] = None,
    milestone_id: Optional[int] = None,
    labels: Optional[list[int]] = None,
):
    """Create a pull request."""
    payload: dict = {"title": title, "head": head, "base": base}
    if body is not None:
        payload["body"] = body
    if assignees is not None:
        payload["assignees"] = assignees
    if milestone_id is not None:
        payload["milestone"] = milestone_id
    if labels is not None:
        payload["labels"] = labels
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/pulls", json=payload))

@_op(gitea_update)
def edit_pull_request(
    owner: str,
    repo: str,
    index: int,
    title: Optional[str] = None,
    body: Optional[str] = None,
    state: Optional[str] = None,
    base: Optional[str] = None,
    assignees: Optional[list[str]] = None,
    milestone: Optional[int] = None,
    labels: Optional[list[int]] = None,
):
    """Edit a pull request."""
    payload: dict = {}
    if title is not None:
        payload["title"] = title
    if body is not None:
        payload["body"] = body
    if state is not None:
        payload["state"] = state
    if base is not None:
        payload["base"] = base
    if assignees is not None:
        payload["assignees"] = assignees
    if milestone is not None:
        payload["milestone"] = milestone
    if labels is not None:
        payload["labels"] = labels
    return _ok(
        _get_client().patch(f"/repos/{owner}/{repo}/pulls/{index}", json=payload)
    )

@_op(gitea_create)
def merge_pull_request(
    owner: str,
    repo: str,
    index: int,
    merge_type: str = "merge",
    merge_message: Optional[str] = None,
    delete_branch_after_merge: Optional[bool] = None,
):
    """Merge a pull request. merge_type can be: merge, rebase, rebase-merge, squash, fast-forward-only."""
    body: dict = {"Do": merge_type}
    if merge_message is not None:
        body["merge_message_field"] = merge_message
    if delete_branch_after_merge is not None:
        body["delete_branch_after_merge"] = delete_branch_after_merge
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/pulls/{index}/merge", json=body)
    )

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

@_op(gitea_create)
def update_pull_request_branch(
    owner: str,
    repo: str,
    index: int,
    style: Optional[str] = None,
):
    """Update a pull request branch. Style can be 'merge' or 'rebase'."""
    body: dict = {}
    if style is not None:
        body["style"] = style
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/update", json=body
        )
    )

@_op(gitea_read)
def list_pull_reviews(owner: str, repo: str, index: int):
    """List reviews on a pull request."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/pulls/{index}/reviews")
    )

@_op(gitea_create)
def create_pull_review(
    owner: str,
    repo: str,
    index: int,
    body: Optional[str] = None,
    event: Optional[str] = None,
    comments: Optional[list[dict]] = None,
):
    """Create a review on a pull request. Event can be: APPROVED, REQUEST_CHANGES, COMMENT, PENDING."""
    payload: dict = {}
    if body is not None:
        payload["body"] = body
    if event is not None:
        payload["event"] = event
    if comments is not None:
        payload["comments"] = comments
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews", json=payload
        )
    )

@_op(gitea_create)
def submit_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: int,
    body: Optional[str] = None,
    event: Optional[str] = None,
):
    """Submit a pending pull request review."""
    payload: dict = {}
    if body is not None:
        payload["body"] = body
    if event is not None:
        payload["event"] = event
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}",
            json=payload,
        )
    )

@_op(gitea_create)
def request_pull_reviewers(
    owner: str, repo: str, index: int, reviewers: list[str]
):
    """Request reviewers for a pull request."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/requested_reviewers",
            json={"reviewers": reviewers},
        )
    )

@_op(gitea_create)
def dismiss_pull_review(
    owner: str,
    repo: str,
    index: int,
    review_id: int,
    message: Optional[str] = None,
):
    """Dismiss a pull request review."""
    body: dict = {}
    if message is not None:
        body["message"] = message
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/dismissals",
            json=body,
        )
    )

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

@_op(gitea_create)
def dispatch_workflow(
    owner: str,
    repo: str,
    workflow_id: str,
    ref: str,
    inputs: Optional[dict] = None,
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
def get_workflow_run(owner: str, repo: str, run_id: int):
    """Get a workflow run by ID."""
    return _ok(_slim_workflow_run(_get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}")))

@_op(gitea_read)
def list_workflow_run_jobs(owner: str, repo: str, run_id: int):
    """List jobs for a workflow run."""
    return _ok(_slim_jobs(
        _get_client().get(f"/repos/{owner}/{repo}/actions/runs/{run_id}/jobs")
    ))

@_op(gitea_read)
def get_workflow_job(owner: str, repo: str, job_id: int):
    """Get a workflow job by ID."""
    return _ok(_slim_job(_get_client().get(f"/repos/{owner}/{repo}/actions/jobs/{job_id}")))

@_op(gitea_read)
def get_workflow_job_logs(
    owner: str,
    repo: str,
    job_id: int,
    tail: Optional[int] = 200,
    filter: Optional[str] = None,
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
        lines = [l for l in lines if pat.search(l)]
    if tail and tail > 0:
        lines = lines[-tail:]
    return "\n".join(lines)

@_op(gitea_read)
def list_action_secrets(owner: str, repo: str):
    """List action secrets for a repository."""
    return _ok(
        _get_client().paginate(f"/repos/{owner}/{repo}/actions/secrets")
    )

@_op(gitea_update)
def create_action_secret(owner: str, repo: str, secret_name: str, data: str):
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

@_op(gitea_create)
def create_action_variable(
    owner: str, repo: str, variable_name: str, value: str
):
    """Create an action variable in a repository."""
    return _ok(
        _get_client().post(
            f"/repos/{owner}/{repo}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_update)
def update_action_variable(
    owner: str, repo: str, variable_name: str, value: str
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

@_op(gitea_create)
def create_org(
    username: str,
    full_name: Optional[str] = None,
    description: Optional[str] = None,
    website: Optional[str] = None,
    visibility: Optional[str] = None,
):
    """Create an organization."""
    visibility = _enforce_visibility(visibility)
    body: dict = {"username": username}
    if full_name is not None:
        body["full_name"] = full_name
    if description is not None:
        body["description"] = description
    if website is not None:
        body["website"] = website
    if visibility is not None:
        body["visibility"] = visibility
    return _ok(_get_client().post("/orgs", json=body))

@_op(gitea_update)
def edit_org(
    org: str,
    full_name: Optional[str] = None,
    description: Optional[str] = None,
    website: Optional[str] = None,
    visibility: Optional[str] = None,
):
    """Edit an organization's properties."""
    visibility = _enforce_visibility(visibility)
    body: dict = {}
    if full_name is not None:
        body["full_name"] = full_name
    if description is not None:
        body["description"] = description
    if website is not None:
        body["website"] = website
    if visibility is not None:
        body["visibility"] = visibility
    return _ok(_get_client().patch(f"/orgs/{org}", json=body))

@_op(gitea_delete)
def delete_org(org: str):
    """Delete an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}"))

@_op(gitea_read)
def list_org_repos(org: str, brief: bool = True):
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

@_op(gitea_update)
def set_org_public_member(org: str, username: str):
    """Publicize a user's membership in an organization."""
    return _ok(_get_client().put(f"/orgs/{org}/public_members/{username}"))

@_op(gitea_delete)
def remove_org_public_member(org: str, username: str):
    """Conceal a user's membership in an organization."""
    return _ok(_get_client().delete(f"/orgs/{org}/public_members/{username}"))

@_op(gitea_create)
def create_org_repo(
    org: str,
    name: str,
    description: Optional[str] = None,
    private: Optional[bool] = None,
    auto_init: Optional[bool] = None,
    gitignores: Optional[str] = None,
    license: Optional[str] = None,
    readme: Optional[str] = None,
    default_branch: Optional[str] = None,
):
    """Create a repository in an organization."""
    private = _enforce_private(private)
    body: dict = {"name": name}
    if description is not None:
        body["description"] = description
    if private is not None:
        body["private"] = private
    if auto_init is not None:
        body["auto_init"] = auto_init
    if gitignores is not None:
        body["gitignores"] = gitignores
    if license is not None:
        body["license"] = license
    if readme is not None:
        body["readme"] = readme
    if default_branch is not None:
        body["default_branch"] = default_branch
    return _ok(_get_client().post(f"/orgs/{org}/repos", json=body))

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

@_op(gitea_create)
def create_team(
    org: str,
    name: str,
    permission: Optional[str] = None,
    units: Optional[list[str]] = None,
    description: Optional[str] = None,
):
    """Create a team in an organization. Permission can be: read, write, admin. Units are like: repo.code, repo.issues, repo.pulls."""
    body: dict = {"name": name}
    if permission is not None:
        body["permission"] = permission
    if units is not None:
        body["units"] = units
    if description is not None:
        body["description"] = description
    return _ok(_get_client().post(f"/orgs/{org}/teams", json=body))

@_op(gitea_update)
def edit_team(
    team_id: int,
    name: Optional[str] = None,
    description: Optional[str] = None,
    permission: Optional[str] = None,
    units: Optional[list[str]] = None,
):
    """Edit a team's properties."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if description is not None:
        body["description"] = description
    if permission is not None:
        body["permission"] = permission
    if units is not None:
        body["units"] = units
    return _ok(_get_client().patch(f"/teams/{team_id}", json=body))

@_op(gitea_delete)
def delete_team(team_id: int):
    """Delete a team."""
    return _ok(_get_client().delete(f"/teams/{team_id}"))

@_op(gitea_read)
def list_team_members(team_id: int):
    """List members of a team."""
    return _ok(_get_client().paginate(f"/teams/{team_id}/members"))

@_op(gitea_update)
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

@_op(gitea_update)
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

@_op(gitea_create)
def create_org_label(
    org: str,
    name: str,
    color: str,
    description: Optional[str] = None,
):
    """Create a label in an organization."""
    body: dict = {"name": name, "color": color}
    if description is not None:
        body["description"] = description
    return _ok(_get_client().post(f"/orgs/{org}/labels", json=body))

@_op(gitea_update)
def edit_org_label(
    org: str,
    label_id: int,
    name: Optional[str] = None,
    color: Optional[str] = None,
    description: Optional[str] = None,
):
    """Edit an organization label."""
    body: dict = {}
    if name is not None:
        body["name"] = name
    if color is not None:
        body["color"] = color
    if description is not None:
        body["description"] = description
    return _ok(_get_client().patch(f"/orgs/{org}/labels/{label_id}", json=body))

@_op(gitea_delete)
def delete_org_label(org: str, label_id: int):
    """Delete an organization label."""
    return _ok(_get_client().delete(f"/orgs/{org}/labels/{label_id}"))

# ── Notifications ────────────────────────────────────────────────────────────


@_op(gitea_read)
def list_notifications(
    all: Optional[bool] = None,
    status_types: Optional[list[str]] = None,
    subject_type: Optional[str] = None,
    brief: bool = True,
):
    """List notifications for the current user.

    brief (default True): compact view — id, repo, subject type/title, unread,
    updated_at. Set brief=False for full objects."""
    params: dict = {}
    if all is not None:
        params["all"] = all
    if status_types is not None:
        params["status-types"] = status_types
    if subject_type is not None:
        params["subject-type"] = subject_type
    data = _get_client().paginate("/notifications", params=params or None)
    if brief:
        data = _slim_notifications(data)
    return _ok(data)

@_op(gitea_update)
def mark_notifications_read(last_read_at: Optional[str] = None):
    """Mark all notifications as read."""
    body: dict = {}
    if last_read_at is not None:
        body["last_read_at"] = last_read_at
    return _ok(_get_client().put("/notifications", json=body))

@_op(gitea_read)
def get_notification_thread(thread_id: int):
    """Get a notification thread by ID."""
    return _ok(_get_client().get(f"/notifications/threads/{thread_id}"))

@_op(gitea_update)
def mark_notification_read(thread_id: int):
    """Mark a notification thread as read."""
    return _ok(_get_client().patch(f"/notifications/threads/{thread_id}"))

@_op(gitea_read)
def list_repo_notifications(
    owner: str,
    repo: str,
    all: Optional[bool] = None,
    status_types: Optional[list[str]] = None,
    brief: bool = True,
):
    """List notifications for a repository.

    brief (default True): compact view. Set brief=False for full objects."""
    params: dict = {}
    if all is not None:
        params["all"] = all
    if status_types is not None:
        params["status-types"] = status_types
    data = _get_client().paginate(
        f"/repos/{owner}/{repo}/notifications", params=params or None
    )
    if brief:
        data = _slim_notifications(data)
    return _ok(data)

@_op(gitea_update)
def mark_repo_notifications_read(
    owner: str,
    repo: str,
    last_read_at: Optional[str] = None,
):
    """Mark all notifications in a repository as read."""
    body: dict = {}
    if last_read_at is not None:
        body["last_read_at"] = last_read_at
    return _ok(
        _get_client().put(
            f"/repos/{owner}/{repo}/notifications", json=body
        )
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

@_op(gitea_create)
def create_wiki_page(
    owner: str,
    repo: str,
    title: str,
    content: str,
    message: Optional[str] = None,
):
    """Create a new wiki page. Content is provided as plain text and will be base64-encoded automatically."""
    encoded = base64.b64encode(content.encode()).decode()
    body: dict = {"title": title, "content_base64": encoded}
    if message is not None:
        body["message"] = message
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/wiki/new", json=body)
    )

@_op(gitea_update)
def edit_wiki_page(
    owner: str,
    repo: str,
    page_name: str,
    title: Optional[str] = None,
    content: Optional[str] = None,
    message: Optional[str] = None,
):
    """Edit a wiki page. Content is provided as plain text and will be base64-encoded automatically."""
    body: dict = {}
    if title is not None:
        body["title"] = title
    if content is not None:
        body["content_base64"] = base64.b64encode(content.encode()).decode()
    if message is not None:
        body["message"] = message
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


@_op(gitea_read)
def list_packages(
    owner: str,
    type: Optional[str] = None,
):
    """List packages for an owner. Type can filter by package type."""
    params: dict = {}
    if type is not None:
        params["type"] = type
    return _ok(
        _get_client().paginate(f"/packages/{owner}", params=params or None)
    )

@_op(gitea_read)
def get_package(owner: str, type: str, name: str, version: str):
    """Get a package by type, name, and version."""
    return _ok(
        _get_client().get(f"/packages/{owner}/{type}/{name}/{version}")
    )

@_op(gitea_delete)
def delete_package(owner: str, type: str, name: str, version: str):
    """Delete a package."""
    return _ok(
        _get_client().delete(f"/packages/{owner}/{type}/{name}/{version}")
    )

@_op(gitea_read)
def list_package_files(owner: str, type: str, name: str, version: str):
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
    username: str,
    email: str,
    password: str,
    must_change_password: Optional[bool] = None,
    login_name: Optional[str] = None,
    send_notify: Optional[bool] = None,
):
    """Create a new user (admin only)."""
    body: dict = {
        "username": username,
        "email": email,
        "password": password,
    }
    if must_change_password is not None:
        body["must_change_password"] = must_change_password
    if login_name is not None:
        body["login_name"] = login_name
    if send_notify is not None:
        body["send_notify"] = send_notify
    return _ok(_get_client().post("/admin/users", json=body))

@_op(gitea_admin_write)
def admin_edit_user(
    username: str,
    email: Optional[str] = None,
    password: Optional[str] = None,
    must_change_password: Optional[bool] = None,
    login_name: Optional[str] = None,
    active: Optional[bool] = None,
    admin: Optional[bool] = None,
    allow_git_hook: Optional[bool] = None,
    max_repo_creation: Optional[int] = None,
    prohibit_login: Optional[bool] = None,
):
    """Edit a user's properties (admin only)."""
    body: dict = {}
    if email is not None:
        body["email"] = email
    if password is not None:
        body["password"] = password
    if must_change_password is not None:
        body["must_change_password"] = must_change_password
    if login_name is not None:
        body["login_name"] = login_name
    if active is not None:
        body["active"] = active
    if admin is not None:
        body["admin"] = admin
    if allow_git_hook is not None:
        body["allow_git_hook"] = allow_git_hook
    if max_repo_creation is not None:
        body["max_repo_creation"] = max_repo_creation
    if prohibit_login is not None:
        body["prohibit_login"] = prohibit_login
    return _ok(_get_client().patch(f"/admin/users/{username}", json=body))

@_op(gitea_admin_write)
def admin_delete_user(username: str, purge: bool = False):
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
def admin_run_cron_job(task_name: str):
    """Run a cron job by name (admin only)."""
    return _ok(_get_client().post(f"/admin/cron/{task_name}"))

@_op(gitea_admin_read)
def admin_list_repos(
    limit: Optional[int] = None,
    page: Optional[int] = None,
):
    """List all repositories (admin only)."""
    params: dict = {}
    if limit is not None:
        params["limit"] = limit
    if page is not None:
        params["page"] = page
    return _ok(_get_client().paginate("/admin/repos", params=params or None))

@_op(gitea_admin_write)
def admin_create_org(
    username: str,
    owner_name: str,
    full_name: Optional[str] = None,
    description: Optional[str] = None,
    website: Optional[str] = None,
    visibility: Optional[str] = None,
):
    """Create an organization (admin only). owner_name is the user who will own the org."""
    visibility = _enforce_visibility(visibility)
    body: dict = {"username": username}
    if full_name is not None:
        body["full_name"] = full_name
    if description is not None:
        body["description"] = description
    if website is not None:
        body["website"] = website
    if visibility is not None:
        body["visibility"] = visibility
    return _ok(_get_client().post(f"/admin/users/{owner_name}/orgs", json=body))

@_op(gitea_admin_write)
def admin_create_repo_for_user(
    username: str,
    name: str,
    description: Optional[str] = None,
    private: Optional[bool] = None,
    auto_init: Optional[bool] = None,
):
    """Create a repository for a user (admin only)."""
    private = _enforce_private(private)
    body: dict = {"name": name}
    if description is not None:
        body["description"] = description
    if private is not None:
        body["private"] = private
    if auto_init is not None:
        body["auto_init"] = auto_init
    return _ok(_get_client().post(f"/admin/users/{username}/repos", json=body))

@_op(gitea_admin_write)
def admin_rename_user(username: str, new_username: str):
    """Rename a user (admin only)."""
    return _ok(
        _get_client().post(
            f"/admin/users/{username}/rename",
            json={"new_username": new_username},
        )
    )

@_op(gitea_admin_write)
def admin_create_user_public_key(username: str, title: str, key: str):
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
    limit: Optional[int] = None,
    page: Optional[int] = None,
):
    """List all emails (admin only)."""
    params: dict = {}
    if limit is not None:
        params["limit"] = limit
    if page is not None:
        params["page"] = page
    return _ok(_get_client().paginate("/admin/emails", params=params or None))

@_op(gitea_admin_read)
def admin_search_emails(query: str):
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

@_op(gitea_create)
def create_user_runner_token():
    """Get a user-level actions runner registration token."""
    return _ok(_get_client().post("/user/actions/runners/registration-token"))

@_op(gitea_create)
def create_repo_runner_token(owner: str, repo: str):
    """Get a repo-level actions runner registration token."""
    return _ok(_get_client().post(f"/repos/{owner}/{repo}/actions/runners/registration-token"))

@_op(gitea_create)
def create_org_runner_token(org: str):
    """Get an org-level actions runner registration token."""
    return _ok(_get_client().post(f"/orgs/{org}/actions/runners/registration-token"))

# ── Actions - Org Secrets/Variables ──────────────────────────────────────


@_op(gitea_read)
def list_org_action_secrets(org: str):
    """List action secrets for an organization."""
    return _ok(_get_client().paginate(f"/orgs/{org}/actions/secrets"))

@_op(gitea_update)
def create_org_action_secret(org: str, secret_name: str, data: str):
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

@_op(gitea_create)
def create_org_action_variable(org: str, variable_name: str, value: str):
    """Create an action variable in an organization."""
    return _ok(
        _get_client().post(
            f"/orgs/{org}/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_update)
def update_org_action_variable(org: str, variable_name: str, value: str):
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


@_op(gitea_read)
def list_user_action_secrets():
    """List action secrets for the current user."""
    return _ok(_get_client().paginate("/user/actions/secrets"))

@_op(gitea_update)
def create_user_action_secret(secret_name: str, data: str):
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

@_op(gitea_create)
def create_user_action_variable(variable_name: str, value: str):
    """Create an action variable for the current user."""
    return _ok(
        _get_client().post(
            f"/user/actions/variables/{variable_name}",
            json={"value": value},
        )
    )

@_op(gitea_update)
def update_user_action_variable(variable_name: str, value: str):
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


@_op(gitea_create)
def render_markdown(
    text: str,
    mode: Optional[str] = None,
    context: Optional[str] = None,
    wiki: Optional[bool] = None,
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
def search_topics(query: str):
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
def get_gitignore_template(name: str):
    """Get a specific .gitignore template by name."""
    return _ok(_get_client().get(f"/gitignore/templates/{name}"))

@_op(gitea_read)
def get_license_template(name: str):
    """Get a specific license template by name."""
    return _ok(_get_client().get(f"/licenses/{name}"))

@_op(gitea_read)
def list_package_versions(owner: str, type: str, name: str):
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
    page: Optional[int] = None,
    limit: Optional[int] = None,
):
    """List activity feeds for a repository."""
    params: dict = {}
    if page is not None:
        params["page"] = page
    if limit is not None:
        params["limit"] = limit
    return _ok(
        _get_client().paginate(
            f"/repos/{owner}/{repo}/activities/feeds", params=params or None
        )
    )

@_op(gitea_read)
def get_repo_git_notes(owner: str, repo: str, sha: str):
    """Get a git note for a commit."""
    return _ok(_get_client().get(f"/repos/{owner}/{repo}/git/notes/{sha}"))

@_op(gitea_read)
def get_repo_archive(owner: str, repo: str, archive: str):
    """Get an archive of a repository. archive should be like 'main.tar.gz' or 'main.zip'."""
    return _get_client().get_text(f"/repos/{owner}/{repo}/archive/{archive}")

@_op(gitea_read)
def list_repo_refs(owner: str, repo: str, ref_type: str = ""):
    """List git references in a repository. ref_type can be empty, 'heads', or 'tags'."""
    path = f"/repos/{owner}/{repo}/git/refs"
    if ref_type:
        path = f"{path}/{ref_type}"
    return _ok(_get_client().get(path))

@_op(gitea_read)
def get_git_tree(
    owner: str,
    repo: str,
    sha: str,
    recursive: Optional[bool] = None,
):
    """Get the tree for a commit SHA."""
    params: dict = {}
    if recursive is not None:
        params["recursive"] = recursive
    return _ok(
        _get_client().get(
            f"/repos/{owner}/{repo}/git/trees/{sha}", params=params or None
        )
    )

@_op(gitea_create)
def transfer_repo(
    owner: str,
    repo: str,
    new_owner: str,
    team_ids: Optional[list[int]] = None,
):
    """Transfer a repository to another owner."""
    body: dict = {"new_owner": new_owner}
    if team_ids is not None:
        body["team_ids"] = team_ids
    return _ok(
        _get_client().post(f"/repos/{owner}/{repo}/transfer", json=body)
    )

@_op(gitea_create)
def create_repo_from_template(
    template_owner: str,
    template_repo: str,
    name: str,
    owner: str,
    description: Optional[str] = None,
    private: Optional[bool] = None,
    git_content: Optional[bool] = None,
    topics: Optional[bool] = None,
    labels: Optional[bool] = None,
):
    """Create a repository from a template."""
    private = _enforce_private(private)
    body: dict = {"name": name, "owner": owner}
    if description is not None:
        body["description"] = description
    if private is not None:
        body["private"] = private
    if git_content is not None:
        body["git_content"] = git_content
    if topics is not None:
        body["topics"] = topics
    if labels is not None:
        body["labels"] = labels
    return _ok(
        _get_client().post(
            f"/repos/{template_owner}/{template_repo}/generate", json=body
        )
    )

@_op(gitea_read)
def list_repo_assignees(owner: str, repo: str):
    """List users who can be assigned to issues in a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/assignees"))

@_op(gitea_read)
def list_repo_reviewers(owner: str, repo: str):
    """List users who can review pull requests in a repository."""
    return _ok(_get_client().paginate(f"/repos/{owner}/{repo}/reviewers"))

@_op(gitea_read)
def get_pull_review_comments(
    owner: str, repo: str, index: int, review_id: int
):
    """List comments on a pull request review."""
    return _ok(
        _get_client().paginate(
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
    owner: str, repo: str, index: int, reviewers: list[str]
):
    """Remove reviewers from a pull request."""
    return _ok(
        _get_client()._json(
            "DELETE",
            f"/repos/{owner}/{repo}/pulls/{index}/requested_reviewers",
            json={"reviewers": reviewers},
        )
    )
