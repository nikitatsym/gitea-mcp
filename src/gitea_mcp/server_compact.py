"""Compact mode: 6 meta-tools with auto-generated help."""

import base64
import json
import re
from urllib.parse import urlparse, parse_qs

from mcp.server.fastmcp import FastMCP

from .client import GiteaClient

mcp = FastMCP("gitea")

# ── Endpoint map: tool_name -> (METHOD, path_template) ───────────────
# Organized by category for help text generation.
# Validated against server.py tools at import time.
_ENDPOINTS = {
    "General": {
        "get_version": ("GET", "/version"),
        "get_current_user": ("GET", "/user"),
    },
    "Users": {
        "search_users": ("GET", "/users/search"),
        "get_user": ("GET", "/users/{username}"),
        "list_user_repos": ("GET", "/users/{username}/repos"),
        "list_followers": ("GET", "/users/{username}/followers"),
        "list_following": ("GET", "/users/{username}/following"),
        "follow_user": ("PUT", "/user/following/{username}"),
        "unfollow_user": ("DELETE", "/user/following/{username}"),
        "list_user_heatmap": ("GET", "/users/{username}/heatmap"),
        "get_user_settings": ("GET", "/user/settings"),
        "check_user_following": ("GET", "/users/{username}/following/{target}"),
        "list_user_emails": ("GET", "/user/emails"),
        "add_user_email": ("POST", "/user/emails"),
        "delete_user_email": ("DELETE", "/user/emails"),
        "list_user_teams": ("GET", "/user/teams"),
        "list_oauth2_apps": ("GET", "/user/applications/oauth2"),
        "create_oauth2_app": ("POST", "/user/applications/oauth2"),
        "get_oauth2_app": ("GET", "/user/applications/oauth2/{app_id}"),
        "edit_oauth2_app": ("PATCH", "/user/applications/oauth2/{app_id}"),
        "delete_oauth2_app": ("DELETE", "/user/applications/oauth2/{app_id}"),
        "list_blocked_users": ("GET", "/user/blocks"),
        "block_user": ("PUT", "/user/blocks/{username}"),
        "unblock_user": ("DELETE", "/user/blocks/{username}"),
        "update_user_settings": ("PATCH", "/user/settings"),
    },
    "SSH / GPG Keys": {
        "list_ssh_keys": ("GET", "/user/keys"),
        "create_ssh_key": ("POST", "/user/keys"),
        "delete_ssh_key": ("DELETE", "/user/keys/{key_id}"),
        "list_gpg_keys": ("GET", "/user/gpg_keys"),
        "create_gpg_key": ("POST", "/user/gpg_keys"),
        "delete_gpg_key": ("DELETE", "/user/gpg_keys/{key_id}"),
    },
    "Repositories": {
        "search_repos": ("GET", "/repos/search"),
        "create_repo": ("POST", "/user/repos"),
        "get_repo": ("GET", "/repos/{owner}/{repo}"),
        "edit_repo": ("PATCH", "/repos/{owner}/{repo}"),
        "delete_repo": ("DELETE", "/repos/{owner}/{repo}"),
        "fork_repo": ("POST", "/repos/{owner}/{repo}/forks"),
        "list_forks": ("GET", "/repos/{owner}/{repo}/forks"),
        "list_repo_topics": ("GET", "/repos/{owner}/{repo}/topics"),
        "set_repo_topics": ("PUT", "/repos/{owner}/{repo}/topics"),
        "list_repo_collaborators": ("GET", "/repos/{owner}/{repo}/collaborators"),
        "add_repo_collaborator": ("PUT", "/repos/{owner}/{repo}/collaborators/{collaborator}"),
        "remove_repo_collaborator": ("DELETE", "/repos/{owner}/{repo}/collaborators/{collaborator}"),
        "star_repo": ("PUT", "/user/starred/{owner}/{repo}"),
        "unstar_repo": ("DELETE", "/user/starred/{owner}/{repo}"),
        "list_my_starred_repos": ("GET", "/user/starred"),
        "add_repo_topic": ("PUT", "/repos/{owner}/{repo}/topics/{topic}"),
        "delete_repo_topic": ("DELETE", "/repos/{owner}/{repo}/topics/{topic}"),
        "list_repo_watchers": ("GET", "/repos/{owner}/{repo}/subscribers"),
        "list_my_subscriptions": ("GET", "/user/subscriptions"),
        "watch_repo": ("PUT", "/repos/{owner}/{repo}/subscription"),
        "unwatch_repo": ("DELETE", "/repos/{owner}/{repo}/subscription"),
        "list_repo_teams": ("GET", "/repos/{owner}/{repo}/teams"),
        "check_repo_collaborator": ("GET", "/repos/{owner}/{repo}/collaborators/{collaborator}"),
        "get_repo_collaborator_permission": ("GET", "/repos/{owner}/{repo}/collaborators/{collaborator}/permission"),
    },
    "Webhooks": {
        "list_repo_webhooks": ("GET", "/repos/{owner}/{repo}/hooks"),
        "create_repo_webhook": ("POST", "/repos/{owner}/{repo}/hooks"),
        "edit_repo_webhook": ("PATCH", "/repos/{owner}/{repo}/hooks/{hook_id}"),
        "delete_repo_webhook": ("DELETE", "/repos/{owner}/{repo}/hooks/{hook_id}"),
        "test_repo_webhook": ("POST", "/repos/{owner}/{repo}/hooks/{hook_id}/tests"),
    },
    "Org Webhooks": {
        "list_org_webhooks": ("GET", "/orgs/{org}/hooks"),
        "create_org_webhook": ("POST", "/orgs/{org}/hooks"),
        "edit_org_webhook": ("PATCH", "/orgs/{org}/hooks/{hook_id}"),
        "delete_org_webhook": ("DELETE", "/orgs/{org}/hooks/{hook_id}"),
    },
    "Deploy Keys": {
        "list_deploy_keys": ("GET", "/repos/{owner}/{repo}/keys"),
        "create_deploy_key": ("POST", "/repos/{owner}/{repo}/keys"),
        "get_deploy_key": ("GET", "/repos/{owner}/{repo}/keys/{key_id}"),
        "delete_deploy_key": ("DELETE", "/repos/{owner}/{repo}/keys/{key_id}"),
    },
    "Files and Content": {
        "get_file_content": ("GET", "/repos/{owner}/{repo}/contents/{filepath}"),
        "create_file": ("POST", "/repos/{owner}/{repo}/contents/{filepath}"),
        "update_file": ("PUT", "/repos/{owner}/{repo}/contents/{filepath}"),
        "delete_file": ("DELETE", "/repos/{owner}/{repo}/contents/{filepath}"),
        "get_directory_content": ("GET", "/repos/{owner}/{repo}/contents/{dirpath}"),
        "get_raw_file": ("GET", "/repos/{owner}/{repo}/raw/{filepath}"),
    },
    "Branches": {
        "list_branches": ("GET", "/repos/{owner}/{repo}/branches"),
        "get_branch": ("GET", "/repos/{owner}/{repo}/branches/{branch}"),
        "create_branch": ("POST", "/repos/{owner}/{repo}/branches"),
        "delete_branch": ("DELETE", "/repos/{owner}/{repo}/branches/{branch}"),
        "list_branch_protections": ("GET", "/repos/{owner}/{repo}/branch_protections"),
        "create_branch_protection": ("POST", "/repos/{owner}/{repo}/branch_protections"),
        "get_branch_protection": ("GET", "/repos/{owner}/{repo}/branch_protections/{name}"),
        "edit_branch_protection": ("PATCH", "/repos/{owner}/{repo}/branch_protections/{name}"),
        "delete_branch_protection": ("DELETE", "/repos/{owner}/{repo}/branch_protections/{name}"),
    },
    "Tag Protections": {
        "list_tag_protections": ("GET", "/repos/{owner}/{repo}/tag_protections"),
        "create_tag_protection": ("POST", "/repos/{owner}/{repo}/tag_protections"),
        "get_tag_protection": ("GET", "/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"),
        "edit_tag_protection": ("PATCH", "/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"),
        "delete_tag_protection": ("DELETE", "/repos/{owner}/{repo}/tag_protections/{tag_protection_id}"),
    },
    "Commits and Statuses": {
        "list_commits": ("GET", "/repos/{owner}/{repo}/commits"),
        "get_commit": ("GET", "/repos/{owner}/{repo}/git/commits/{sha}"),
        "get_commit_diff": ("GET", "/repos/{owner}/{repo}/git/commits/{sha}.diff"),
        "compare_commits": ("GET", "/repos/{owner}/{repo}/compare/{base}...{head}"),
        "list_commit_statuses": ("GET", "/repos/{owner}/{repo}/statuses/{sha}"),
        "create_commit_status": ("POST", "/repos/{owner}/{repo}/statuses/{sha}"),
        "get_combined_commit_status": ("GET", "/repos/{owner}/{repo}/commits/{ref}/status"),
    },
    "Tags and Releases": {
        "list_tags": ("GET", "/repos/{owner}/{repo}/tags"),
        "create_tag": ("POST", "/repos/{owner}/{repo}/tags"),
        "delete_tag": ("DELETE", "/repos/{owner}/{repo}/tags/{tag}"),
        "list_releases": ("GET", "/repos/{owner}/{repo}/releases"),
        "get_release": ("GET", "/repos/{owner}/{repo}/releases/{release_id}"),
        "create_release": ("POST", "/repos/{owner}/{repo}/releases"),
        "edit_release": ("PATCH", "/repos/{owner}/{repo}/releases/{release_id}"),
        "delete_release": ("DELETE", "/repos/{owner}/{repo}/releases/{release_id}"),
    },
    "Labels": {
        "list_repo_labels": ("GET", "/repos/{owner}/{repo}/labels"),
        "create_repo_label": ("POST", "/repos/{owner}/{repo}/labels"),
        "edit_repo_label": ("PATCH", "/repos/{owner}/{repo}/labels/{label_id}"),
        "delete_repo_label": ("DELETE", "/repos/{owner}/{repo}/labels/{label_id}"),
    },
    "Milestones": {
        "list_milestones": ("GET", "/repos/{owner}/{repo}/milestones"),
        "get_milestone": ("GET", "/repos/{owner}/{repo}/milestones/{milestone_id}"),
        "create_milestone": ("POST", "/repos/{owner}/{repo}/milestones"),
        "edit_milestone": ("PATCH", "/repos/{owner}/{repo}/milestones/{milestone_id}"),
        "delete_milestone": ("DELETE", "/repos/{owner}/{repo}/milestones/{milestone_id}"),
    },
    "Issues": {
        "list_issues": ("GET", "/repos/{owner}/{repo}/issues"),
        "search_issues": ("GET", "/repos/issues/search"),
        "get_issue": ("GET", "/repos/{owner}/{repo}/issues/{index}"),
        "create_issue": ("POST", "/repos/{owner}/{repo}/issues"),
        "edit_issue": ("PATCH", "/repos/{owner}/{repo}/issues/{index}"),
        "list_issue_comments": ("GET", "/repos/{owner}/{repo}/issues/{index}/comments"),
        "create_issue_comment": ("POST", "/repos/{owner}/{repo}/issues/{index}/comments"),
        "edit_issue_comment": ("PATCH", "/repos/{owner}/{repo}/issues/comments/{comment_id}"),
        "delete_issue_comment": ("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}"),
        "list_issue_labels": ("GET", "/repos/{owner}/{repo}/issues/{index}/labels"),
        "add_issue_labels": ("POST", "/repos/{owner}/{repo}/issues/{index}/labels"),
        "remove_issue_label": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/labels/{label_id}"),
        "replace_issue_labels": ("PUT", "/repos/{owner}/{repo}/issues/{index}/labels"),
        "set_issue_deadline": ("POST", "/repos/{owner}/{repo}/issues/{index}/deadline"),
        "delete_issue_deadline": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/deadline"),
        "clear_issue_labels": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/labels"),
        "get_issue_timeline": ("GET", "/repos/{owner}/{repo}/issues/{index}/timeline"),
        "list_repo_issue_comments": ("GET", "/repos/{owner}/{repo}/issues/comments"),
        "delete_stopwatch": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/stopwatch/delete"),
    },
    "Issue Extended": {
        "list_issue_dependencies": ("GET", "/repos/{owner}/{repo}/issues/{index}/dependencies"),
        "add_issue_dependency": ("POST", "/repos/{owner}/{repo}/issues/{index}/dependencies"),
        "remove_issue_dependency": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/dependencies"),
        "pin_issue": ("POST", "/repos/{owner}/{repo}/issues/{index}/pin"),
        "unpin_issue": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/pin"),
        "lock_issue": ("PUT", "/repos/{owner}/{repo}/issues/{index}/lock"),
        "unlock_issue": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/lock"),
        "list_issue_subscriptions": ("GET", "/repos/{owner}/{repo}/issues/{index}/subscriptions"),
        "subscribe_to_issue": ("PUT", "/repos/{owner}/{repo}/issues/{index}/subscriptions/{user}"),
        "unsubscribe_from_issue": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/subscriptions/{user}"),
    },
    "Reactions": {
        "list_issue_reactions": ("GET", "/repos/{owner}/{repo}/issues/{index}/reactions"),
        "add_issue_reaction": ("POST", "/repos/{owner}/{repo}/issues/{index}/reactions"),
        "remove_issue_reaction": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/reactions"),
        "list_comment_reactions": ("GET", "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"),
        "add_comment_reaction": ("POST", "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"),
        "remove_comment_reaction": ("DELETE", "/repos/{owner}/{repo}/issues/comments/{comment_id}/reactions"),
    },
    "Time Tracking": {
        "list_tracked_times": ("GET", "/repos/{owner}/{repo}/issues/{index}/times"),
        "add_tracked_time": ("POST", "/repos/{owner}/{repo}/issues/{index}/times"),
        "delete_tracked_time": ("DELETE", "/repos/{owner}/{repo}/issues/{index}/times/{time_id}"),
        "start_stopwatch": ("POST", "/repos/{owner}/{repo}/issues/{index}/stopwatch/start"),
        "stop_stopwatch": ("POST", "/repos/{owner}/{repo}/issues/{index}/stopwatch/stop"),
    },
    "Pull Requests": {
        "list_pull_requests": ("GET", "/repos/{owner}/{repo}/pulls"),
        "get_pull_request": ("GET", "/repos/{owner}/{repo}/pulls/{index}"),
        "create_pull_request": ("POST", "/repos/{owner}/{repo}/pulls"),
        "edit_pull_request": ("PATCH", "/repos/{owner}/{repo}/pulls/{index}"),
        "merge_pull_request": ("POST", "/repos/{owner}/{repo}/pulls/{index}/merge"),
        "get_pull_request_diff": ("GET", "/repos/{owner}/{repo}/pulls/{index}.diff"),
        "get_pull_request_files": ("GET", "/repos/{owner}/{repo}/pulls/{index}/files"),
        "get_pull_request_commits": ("GET", "/repos/{owner}/{repo}/pulls/{index}/commits"),
        "update_pull_request_branch": ("POST", "/repos/{owner}/{repo}/pulls/{index}/update"),
        "list_pull_reviews": ("GET", "/repos/{owner}/{repo}/pulls/{index}/reviews"),
        "create_pull_review": ("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews"),
        "submit_pull_review": ("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}"),
        "request_pull_reviewers": ("POST", "/repos/{owner}/{repo}/pulls/{index}/requested_reviewers"),
        "dismiss_pull_review": ("POST", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/dismissals"),
    },
    "Actions / CI": {
        "list_workflows": ("GET", "/repos/{owner}/{repo}/actions/workflows"),
        "get_workflow": ("GET", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}"),
        "dispatch_workflow": ("POST", "/repos/{owner}/{repo}/actions/workflows/{workflow_id}/dispatches"),
        "get_workflow_run": ("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}"),
        "list_workflow_run_jobs": ("GET", "/repos/{owner}/{repo}/actions/runs/{run_id}/jobs"),
        "get_workflow_job": ("GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}"),
        "get_workflow_job_logs": ("GET", "/repos/{owner}/{repo}/actions/jobs/{job_id}/logs"),
        "list_action_secrets": ("GET", "/repos/{owner}/{repo}/actions/secrets"),
        "create_action_secret": ("PUT", "/repos/{owner}/{repo}/actions/secrets/{secret_name}"),
        "delete_action_secret": ("DELETE", "/repos/{owner}/{repo}/actions/secrets/{secret_name}"),
        "list_action_variables": ("GET", "/repos/{owner}/{repo}/actions/variables"),
        "get_action_variable": ("GET", "/repos/{owner}/{repo}/actions/variables/{variable_name}"),
        "create_action_variable": ("POST", "/repos/{owner}/{repo}/actions/variables/{variable_name}"),
        "update_action_variable": ("PUT", "/repos/{owner}/{repo}/actions/variables/{variable_name}"),
        "delete_action_variable": ("DELETE", "/repos/{owner}/{repo}/actions/variables/{variable_name}"),
    },
    "Organizations": {
        "list_orgs": ("GET", "/user/orgs"),
        "get_org": ("GET", "/orgs/{org}"),
        "create_org": ("POST", "/orgs"),
        "edit_org": ("PATCH", "/orgs/{org}"),
        "delete_org": ("DELETE", "/orgs/{org}"),
        "list_org_repos": ("GET", "/orgs/{org}/repos"),
        "list_org_members": ("GET", "/orgs/{org}/members"),
        "check_org_membership": ("GET", "/orgs/{org}/members/{username}"),
        "remove_org_member": ("DELETE", "/orgs/{org}/members/{username}"),
        "list_org_public_members": ("GET", "/orgs/{org}/public_members"),
        "check_org_public_member": ("GET", "/orgs/{org}/public_members/{username}"),
        "set_org_public_member": ("PUT", "/orgs/{org}/public_members/{username}"),
        "remove_org_public_member": ("DELETE", "/orgs/{org}/public_members/{username}"),
        "create_org_repo": ("POST", "/orgs/{org}/repos"),
        "list_user_orgs": ("GET", "/users/{username}/orgs"),
    },
    "Teams": {
        "list_org_teams": ("GET", "/orgs/{org}/teams"),
        "get_team": ("GET", "/teams/{team_id}"),
        "create_team": ("POST", "/orgs/{org}/teams"),
        "edit_team": ("PATCH", "/teams/{team_id}"),
        "delete_team": ("DELETE", "/teams/{team_id}"),
        "list_team_members": ("GET", "/teams/{team_id}/members"),
        "add_team_member": ("PUT", "/teams/{team_id}/members/{username}"),
        "remove_team_member": ("DELETE", "/teams/{team_id}/members/{username}"),
        "list_team_repos": ("GET", "/teams/{team_id}/repos"),
        "add_team_repo": ("PUT", "/teams/{team_id}/repos/{org}/{repo}"),
        "remove_team_repo": ("DELETE", "/teams/{team_id}/repos/{org}/{repo}"),
        "check_team_repo": ("GET", "/teams/{team_id}/repos/{org}/{repo}"),
    },
    "Org Labels": {
        "list_org_labels": ("GET", "/orgs/{org}/labels"),
        "create_org_label": ("POST", "/orgs/{org}/labels"),
        "edit_org_label": ("PATCH", "/orgs/{org}/labels/{label_id}"),
        "delete_org_label": ("DELETE", "/orgs/{org}/labels/{label_id}"),
    },
    "Notifications": {
        "list_notifications": ("GET", "/notifications"),
        "mark_notifications_read": ("PUT", "/notifications"),
        "get_notification_thread": ("GET", "/notifications/threads/{thread_id}"),
        "mark_notification_read": ("PATCH", "/notifications/threads/{thread_id}"),
        "list_repo_notifications": ("GET", "/repos/{owner}/{repo}/notifications"),
        "mark_repo_notifications_read": ("PUT", "/repos/{owner}/{repo}/notifications"),
        "get_new_notification_count": ("GET", "/notifications/new"),
    },
    "Wiki": {
        "list_wiki_pages": ("GET", "/repos/{owner}/{repo}/wiki/pages"),
        "get_wiki_page": ("GET", "/repos/{owner}/{repo}/wiki/page/{page_name}"),
        "create_wiki_page": ("POST", "/repos/{owner}/{repo}/wiki/new"),
        "edit_wiki_page": ("PATCH", "/repos/{owner}/{repo}/wiki/page/{page_name}"),
        "delete_wiki_page": ("DELETE", "/repos/{owner}/{repo}/wiki/page/{page_name}"),
    },
    "Packages": {
        "list_packages": ("GET", "/packages/{owner}"),
        "get_package": ("GET", "/packages/{owner}/{type}/{name}/{version}"),
        "delete_package": ("DELETE", "/packages/{owner}/{type}/{name}/{version}"),
        "list_package_files": ("GET", "/packages/{owner}/{type}/{name}/{version}/files"),
    },
    "Admin": {
        "admin_list_users": ("GET", "/admin/users"),
        "admin_create_user": ("POST", "/admin/users"),
        "admin_edit_user": ("PATCH", "/admin/users/{username}"),
        "admin_delete_user": ("DELETE", "/admin/users/{username}"),
        "admin_list_orgs": ("GET", "/admin/orgs"),
        "admin_list_cron_jobs": ("GET", "/admin/cron"),
        "admin_run_cron_job": ("POST", "/admin/cron/{task_name}"),
        "admin_list_repos": ("GET", "/admin/repos"),
        "admin_create_org": ("POST", "/admin/users/{owner_name}/orgs"),
        "admin_create_repo_for_user": ("POST", "/admin/users/{username}/repos"),
        "admin_rename_user": ("POST", "/admin/users/{username}/rename"),
        "admin_create_user_public_key": ("POST", "/admin/users/{username}/keys"),
        "admin_delete_user_public_key": ("DELETE", "/admin/users/{username}/keys/{key_id}"),
        "admin_list_unadopted_repos": ("GET", "/admin/unadopted"),
        "admin_adopt_repo": ("POST", "/admin/unadopted/{owner}/{repo}"),
        "admin_delete_unadopted_repo": ("DELETE", "/admin/unadopted/{owner}/{repo}"),
        "admin_list_emails": ("GET", "/admin/emails"),
        "admin_search_emails": ("GET", "/admin/emails/search"),
    },
    "Actions Runners": {
        "list_repo_runners": ("GET", "/repos/{owner}/{repo}/actions/runners"),
        "get_repo_runner": ("GET", "/repos/{owner}/{repo}/actions/runners/{runner_id}"),
        "delete_repo_runner": ("DELETE", "/repos/{owner}/{repo}/actions/runners/{runner_id}"),
        "list_org_runners": ("GET", "/orgs/{org}/actions/runners"),
        "get_org_runner": ("GET", "/orgs/{org}/actions/runners/{runner_id}"),
        "delete_org_runner": ("DELETE", "/orgs/{org}/actions/runners/{runner_id}"),
        "list_admin_runners": ("GET", "/admin/actions/runners"),
        "get_admin_runner": ("GET", "/admin/actions/runners/{runner_id}"),
        "delete_admin_runner": ("DELETE", "/admin/actions/runners/{runner_id}"),
        "create_admin_runner_token": ("POST", "/admin/actions/runners/registration-token"),
        "list_user_runners": ("GET", "/user/actions/runners"),
        "get_user_runner": ("GET", "/user/actions/runners/{runner_id}"),
        "delete_user_runner": ("DELETE", "/user/actions/runners/{runner_id}"),
        "create_user_runner_token": ("POST", "/user/actions/runners/registration-token"),
        "create_repo_runner_token": ("POST", "/repos/{owner}/{repo}/actions/runners/registration-token"),
        "create_org_runner_token": ("POST", "/orgs/{org}/actions/runners/registration-token"),
    },
    "Actions - Org Secrets/Variables": {
        "list_org_action_secrets": ("GET", "/orgs/{org}/actions/secrets"),
        "create_org_action_secret": ("PUT", "/orgs/{org}/actions/secrets/{secret_name}"),
        "delete_org_action_secret": ("DELETE", "/orgs/{org}/actions/secrets/{secret_name}"),
        "list_org_action_variables": ("GET", "/orgs/{org}/actions/variables"),
        "get_org_action_variable": ("GET", "/orgs/{org}/actions/variables/{variable_name}"),
        "create_org_action_variable": ("POST", "/orgs/{org}/actions/variables/{variable_name}"),
        "update_org_action_variable": ("PUT", "/orgs/{org}/actions/variables/{variable_name}"),
        "delete_org_action_variable": ("DELETE", "/orgs/{org}/actions/variables/{variable_name}"),
    },
    "Actions - User Secrets/Variables": {
        "list_user_action_secrets": ("GET", "/user/actions/secrets"),
        "create_user_action_secret": ("PUT", "/user/actions/secrets/{secret_name}"),
        "delete_user_action_secret": ("DELETE", "/user/actions/secrets/{secret_name}"),
        "list_user_action_variables": ("GET", "/user/actions/variables"),
        "get_user_action_variable": ("GET", "/user/actions/variables/{variable_name}"),
        "create_user_action_variable": ("POST", "/user/actions/variables/{variable_name}"),
        "update_user_action_variable": ("PUT", "/user/actions/variables/{variable_name}"),
        "delete_user_action_variable": ("DELETE", "/user/actions/variables/{variable_name}"),
    },
    "Misc": {
        "render_markdown": ("POST", "/markdown"),
        "search_topics": ("GET", "/topics/search"),
        "list_gitignore_templates": ("GET", "/gitignore/templates"),
        "list_license_templates": ("GET", "/licenses"),
        "get_signing_key": ("GET", "/signing-key.gpg"),
        "get_nodeinfo": ("GET", "/nodeinfo"),
        "get_gitignore_template": ("GET", "/gitignore/templates/{name}"),
        "get_license_template": ("GET", "/licenses/{name}"),
        "list_package_versions": ("GET", "/packages/{owner}/{type}/{name}"),
        "get_repo_languages": ("GET", "/repos/{owner}/{repo}/languages"),
        "list_repo_activities": ("GET", "/repos/{owner}/{repo}/activities/feeds"),
        "get_repo_git_notes": ("GET", "/repos/{owner}/{repo}/git/notes/{sha}"),
        "get_repo_archive": ("GET", "/repos/{owner}/{repo}/archive/{archive}"),
        "list_repo_refs": ("GET", "/repos/{owner}/{repo}/git/refs"),
        "get_git_tree": ("GET", "/repos/{owner}/{repo}/git/trees/{sha}"),
        "transfer_repo": ("POST", "/repos/{owner}/{repo}/transfer"),
        "create_repo_from_template": ("POST", "/repos/{template_owner}/{template_repo}/generate"),
        "list_repo_assignees": ("GET", "/repos/{owner}/{repo}/assignees"),
        "list_repo_reviewers": ("GET", "/repos/{owner}/{repo}/reviewers"),
        "get_pull_review_comments": ("GET", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}/comments"),
        "delete_pull_review": ("DELETE", "/repos/{owner}/{repo}/pulls/{index}/reviews/{review_id}"),
        "remove_pull_reviewers": ("DELETE", "/repos/{owner}/{repo}/pulls/{index}/requested_reviewers"),
    },
}

# ── Import server.py for introspection & validation ───────────────────
from . import server as _srv
from .config import get_settings
from .server import (
    _slim_issues,
    _slim_repos,
    _slim_notifications,
    _slim_comments,
    _slim_commits,
    _validate_brief,
    _enforce_private,
    _enforce_visibility,
)

_TOOL_DESCS: dict[str, str] = {}
for _name, _tool in _srv.mcp._tool_manager._tools.items():
    _doc = (_tool.fn.__doc__ or "").strip()
    _TOOL_DESCS[_name] = _doc.split("\n")[0] if _doc else ""

# Flatten endpoint map for lookup
_FLAT: dict[str, tuple[str, str]] = {}
for _cat, _ops in _ENDPOINTS.items():
    _FLAT.update(_ops)

# Validate: every server.py tool must be in _ENDPOINTS and vice versa
_missing = set(_TOOL_DESCS) - set(_FLAT)
if _missing:
    raise RuntimeError(
        f"server_compact.py: missing endpoint mapping for: {sorted(_missing)}"
    )
_extra = set(_FLAT) - set(_TOOL_DESCS)
if _extra:
    raise RuntimeError(
        f"server_compact.py: unknown tools in endpoint map: {sorted(_extra)}"
    )


# ── Helpers ───────────────────────────────────────────────────────────
_TEXT_PATTERNS = ("/raw/", ".diff", "/logs", "/signing-key.gpg", "/archive/")


def _is_text_path(path: str) -> bool:
    return any(p in path for p in _TEXT_PATTERNS)


_BRIEF_RULES: list[tuple[re.Pattern, callable]] = [
    # Issues & PRs
    (re.compile(r"^/repos(/[^/]+/[^/]+)?/(issues|pulls)$|^/repos/issues/search$"), _slim_issues),
    # Issue comments (per-issue and per-repo)
    (re.compile(r"^/repos/[^/]+/[^/]+/issues(/\d+)?/comments$"), _slim_comments),
    # Repos lists
    (re.compile(
        r"^/repos/search$"
        r"|^/users/[^/]+/repos$"
        r"|^/orgs/[^/]+/repos$"
        r"|^/repos/[^/]+/[^/]+/forks$"
        r"|^/user/(starred|subscriptions)$"
    ), _slim_repos),
    # Notifications
    (re.compile(r"^/notifications$|^/repos/[^/]+/[^/]+/notifications$"), _slim_notifications),
    # Commits
    (re.compile(r"^/repos/[^/]+/[^/]+/commits$"), _slim_commits),
    # Releases (inline slim)
    (re.compile(r"^/repos/[^/]+/[^/]+/releases$"), lambda data: [
        {"id": r.get("id"), "tag_name": r.get("tag_name"), "name": r.get("name"),
         "draft": r.get("draft"), "prerelease": r.get("prerelease"),
         "published_at": r.get("published_at")}
        for r in data
    ] if isinstance(data, list) else data),
]


def _get_brief_slimmer(path: str):
    """Return slim function for path, or None."""
    for pattern, slimmer in _BRIEF_RULES:
        if pattern.match(path):
            return slimmer
    return None


def _ok(data) -> str:
    if data is None:
        return json.dumps({"status": "ok"})
    if isinstance(data, dict) and "ok" in data and "data" in data:
        data = data["data"]
    return json.dumps(data, indent=2, ensure_ascii=False)


_client = None


def _get_client() -> GiteaClient:
    global _client
    if _client is None:
        _client = GiteaClient()
    return _client


def _is_admin(path: str) -> bool:
    return path.startswith("/admin/")


# ── Auto-generate help text ──────────────────────────────────────────
from jinja2 import Environment as _JinjaEnv

_HELP_TEMPLATE = _JinjaEnv(
    keep_trailing_newline=True, lstrip_blocks=True, trim_blocks=True
).from_string("""\
{{ header }}

NOTES:
  - params is a JSON string: query params for GET, body for POST/PUT/PATCH
  - File/wiki content: pass plain text, auto-base64 encoded
  - Pagination: pass {"page":N,"limit":N} in params
  - Brief mode: list endpoints return compact objects by default (brief=true).
    Pass {"brief":false} for full API response objects.
    Supported: issues, pulls, repos, notifications, comments, commits, releases.
    Issues/PRs include brief field from <brief>...</brief> tag in body.
    If brief is null, use get_issue for details or add <brief>summary</brief> to body.
{% if require_brief %}
  - Issues: body MUST contain <brief>summary</brief> tag (max {{ brief_max_length }} chars).
{% endif %}
  - Job logs: GET .../actions/jobs/{job_id}/logs returns last 100 lines
    by default. Pass {"tail":N} to change (0 = full log).
    Pass {"filter":"error|fail"} to grep lines by regex pattern.
    When both set, filter applies first, then tail.

{% for category, endpoints in sections %}
{{ category }}
{% for method, path, desc in endpoints %}
  {{ "%-6s"|format(method) }} {{ path }} -- {{ desc }}
{% endfor %}

{% endfor %}
""")


def _build_help(header: str, filter_fn) -> str:
    """Build help for endpoints where filter_fn(method, path) is True."""
    sections = []
    for category, ops in _ENDPOINTS.items():
        matching = [
            (m, p, _TOOL_DESCS.get(name, ""))
            for name, (m, p) in ops.items()
            if filter_fn(m, p)
        ]
        if matching:
            sections.append((category.upper(), matching))
    s = get_settings()
    return _HELP_TEMPLATE.render(
        header=header,
        sections=sections,
        require_brief=s.gitea_require_brief,
        brief_max_length=s.gitea_brief_max_length,
    ).rstrip()


_HELP_READ = _build_help(
    "gitea_read(path, params) -- GET non-admin endpoints",
    lambda m, p: m == "GET" and not _is_admin(p),
)
_HELP_CREATE = _build_help(
    "gitea_create(path, params) -- POST non-admin endpoints",
    lambda m, p: m == "POST" and not _is_admin(p),
)
_HELP_UPDATE = _build_help(
    "gitea_update(method, path, params) -- PUT/PATCH non-admin endpoints",
    lambda m, p: m in ("PUT", "PATCH") and not _is_admin(p),
)
_HELP_DELETE = _build_help(
    "gitea_delete(path, params) -- DELETE non-admin endpoints",
    lambda m, p: m == "DELETE" and not _is_admin(p),
)
_HELP_ADMIN_READ = _build_help(
    "gitea_admin_read(path, params) -- GET /admin/* endpoints",
    lambda m, p: m == "GET" and _is_admin(p),
)
_HELP_ADMIN_WRITE = _build_help(
    "gitea_admin_write(method, path, params) -- POST/PUT/PATCH/DELETE /admin/*",
    lambda m, p: m != "GET" and _is_admin(p),
)


# ── Shared dispatch ──────────────────────────────────────────────────
def _dispatch(method: str, path: str, params_str: str) -> str:
    m = method.upper()
    c = _get_client()
    p = json.loads(params_str) if params_str and params_str.strip() else {}

    # Split query string from path and merge into params
    if "?" in path:
        parsed = urlparse(path)
        path = parsed.path
        for k, v in parse_qs(parsed.query).items():
            p.setdefault(k, v[0] if len(v) == 1 else v)

    if m == "GET":
        if _is_text_path(path):
            tail = p.pop("tail", 100 if "/logs" in path else 0)
            log_filter = p.pop("filter", None)
            text = c.get_text(path, params=p or None)
            if log_filter or tail:
                lines = text.splitlines()
                if log_filter:
                    pat = re.compile(log_filter, re.IGNORECASE)
                    lines = [l for l in lines if pat.search(l)]
                if tail and tail > 0:
                    lines = lines[-tail:]
                text = "\n".join(lines)
            return text
        # Brief mode for list endpoints
        brief = p.pop("brief", None)
        data = c.get(path, params=p or None)
        if brief is not False:
            slimmer = _get_brief_slimmer(path)
            if slimmer:
                if isinstance(data, dict) and "ok" in data and "data" in data:
                    data = data["data"]
                data = slimmer(data)
        return _ok(data)

    if m == "POST":
        if path == "/markdown":
            return c._text("POST", path, json=p)
        if re.match(r"/repos/[^/]+/[^/]+/issues$", path):
            _validate_brief(p.get("body"))
        # Enforce private repos/orgs on create
        if re.match(
            r"(/user/repos|/orgs/[^/]+/repos|/admin/users/[^/]+/repos|/repos/[^/]+/[^/]+/generate)$",
            path,
        ):
            p["private"] = _enforce_private(p.get("private"), is_create=True)
        if re.match(r"(/orgs|/admin/users/[^/]+/orgs)$", path):
            p["visibility"] = _enforce_visibility(
                p.get("visibility"), is_create=True
            )
        if "/contents/" in path and "content" in p:
            p["content"] = base64.b64encode(p["content"].encode()).decode()
        if "/wiki/" in path and "content" in p:
            p["content_base64"] = base64.b64encode(
                p.pop("content").encode()
            ).decode()
        return _ok(c.post(path, json=p))

    if m == "PUT":
        if "/contents/" in path and "content" in p:
            p["content"] = base64.b64encode(p["content"].encode()).decode()
        return _ok(c.put(path, json=p))

    if m == "PATCH":
        if re.match(r"/repos/[^/]+/[^/]+/issues/\d+$", path) and "body" in p:
            _validate_brief(p.get("body"))
        # Enforce private repos/orgs on edit
        if re.match(r"/repos/[^/]+/[^/]+$", path) and "private" in p:
            p["private"] = _enforce_private(p.get("private"))
        if re.match(r"/orgs/[^/]+$", path) and "visibility" in p:
            p["visibility"] = _enforce_visibility(p.get("visibility"))
        if "/wiki/" in path and "content" in p:
            p["content_base64"] = base64.b64encode(
                p.pop("content").encode()
            ).decode()
        return _ok(c.patch(path, json=p))

    if m == "DELETE":
        if p:
            return _ok(c._json("DELETE", path, json=p))
        return _ok(c.delete(path))

    return json.dumps({"error": f"Unsupported method: {method}"})


# ── Six meta-tools ───────────────────────────────────────────────────
@mcp.tool()
def gitea_read(path: str, params: str = "{}") -> str:
    """Read from Gitea API (GET). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_READ
    if _is_admin(path):
        return json.dumps({"error": "Admin paths require gitea_admin_read"})
    return _dispatch("GET", path, params)


@mcp.tool()
def gitea_create(path: str, params: str = "{}") -> str:
    """Create via Gitea API (POST). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_CREATE
    if _is_admin(path):
        return json.dumps({"error": "Admin paths require gitea_admin_write"})
    return _dispatch("POST", path, params)


@mcp.tool()
def gitea_update(method: str, path: str, params: str = "{}") -> str:
    """Update via Gitea API (PUT/PATCH). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_UPDATE
    m = method.upper()
    if m not in ("PUT", "PATCH"):
        return json.dumps(
            {"error": f"gitea_update only supports PUT/PATCH, got {method}"}
        )
    if _is_admin(path):
        return json.dumps({"error": "Admin paths require gitea_admin_write"})
    return _dispatch(m, path, params)


@mcp.tool()
def gitea_delete(path: str, params: str = "{}") -> str:
    """Delete via Gitea API (DELETE). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_DELETE
    if _is_admin(path):
        return json.dumps({"error": "Admin paths require gitea_admin_write"})
    return _dispatch("DELETE", path, params)


@mcp.tool()
def gitea_admin_read(path: str, params: str = "{}") -> str:
    """Read admin endpoints (GET /admin/*). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_ADMIN_READ
    if not _is_admin(path):
        return json.dumps({"error": "Non-admin paths should use gitea_read"})
    return _dispatch("GET", path, params)


@mcp.tool()
def gitea_admin_write(method: str, path: str, params: str = "{}") -> str:
    """Write to admin endpoints (POST/PUT/PATCH/DELETE /admin/*). path='help' lists endpoints."""
    if path.lower() == "help":
        return _HELP_ADMIN_WRITE
    m = method.upper()
    if m not in ("POST", "PUT", "PATCH", "DELETE"):
        return json.dumps(
            {"error": f"gitea_admin_write requires POST/PUT/PATCH/DELETE, got {method}"}
        )
    if not _is_admin(path):
        return json.dumps(
            {"error": "Non-admin paths should use gitea_create/update/delete"}
        )
    return _dispatch(m, path, params)
