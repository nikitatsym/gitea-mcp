# gitea-mcp

MCP server for Gitea -- full API coverage for autonomous AI agents.

## Features

- **~290 operations** covering the entire Gitea API surface
- Repositories, issues, pull requests, releases, labels, milestones
- File content management (create, read, update, delete)
- Branches, tags, commits, and status checks
- Actions / CI workflows and artifacts
- **Long-running waiters** - `workflow_runs_wait` / `workflow_jobs_wait` block until a run or job finishes (streaming progress via MCP notifications); non-blocking `*_wait_start` / `*_wait_poll(max_block=...)` / `*_wait_cancel` + `waits_list` keep the agent responsive. Waits tolerate transient API errors (`max_poll_failures`, default 3 consecutive), background waits self-terminate after `max_lifetime` (default 2h), and all wait ops live in `gitea_read` - they only ever GET
- Organizations, teams, and user management
- Webhooks, deploy keys, notifications, wiki, packages
- Admin endpoints for instance-level operations
- **6 risk-graded meta-tools** (`gitea_read` / `gitea_write` / `gitea_execute` / `gitea_delete` / `gitea_admin_read` / `gitea_admin_write`) — agents pick a tool surface by the kind of side effect, not the HTTP verb
- Per-param help with `operation='help' params={'search': 'foo'}` for substring filtering and cross-group hints
- Zero-config install via `uvx`

## Quick Start

Add the following to your MCP client configuration (Claude Desktop, Cursor, Claude Code, etc.).
For Claude Code global config on macOS: `~/.claude.json` → `"mcpServers"`.

```json
{
  "mcpServers": {
    "gitea": {
      "command": "uvx",
      "args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/gitea-mcp/simple", "gitea-mcp"],
      "env": {
        "GITEA_URL": "https://gitea.example.com",
        "GITEA_TOKEN": "your-api-token"
      }
    }
  }
}
```

Or use the interactive **[Setup Page](https://nikitatsym.github.io/gitea-mcp/)** to generate the config.

## Configuration

| Variable | Required | Description |
|---|---|---|
| `GITEA_URL` | Yes | Base URL of your Gitea instance (e.g. `https://gitea.example.com`) |
| `GITEA_TOKEN` | Yes | Personal access token with appropriate permissions. For `CreateUserAccessToken` self-rotation, must include `write:user` (or `all`) scope. |
| `MCP_GITEA_BRIEF_MAX` | No | Max character length for the `<brief>summary</brief>` tag enforced on issue/PR bodies (default: `100`; `0` disables the requirement) |

By default, creating public repos and orgs is blocked — agents must pass `private=true` explicitly. To allow public repos, add `--allow-public` to the command args:

```json
"args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/gitea-mcp/simple", "gitea-mcp", "--allow-public"]
```

## Tool Groups

All ~290 operations are exposed through 6 risk-graded meta-tools — one tool surface per scope, dispatched via `operation` + `params`. Aligned with the v2 MCP spec so agents pick a tool by the kind of side effect, not the HTTP verb.

| Meta-tool | Scope | Examples |
|---|---|---|
| `gitea_read` | GET, safe / read-only | `ListRepos`, `GetIssue`, `ListPullRequests` |
| `gitea_write` | Create + update (POST/PUT/PATCH) | `CreateRepo`, `EditIssue`, `CreatePullRequest` |
| `gitea_execute` | Action triggers with real-world side effects | `MergePullRequest`, `DispatchWorkflow` |
| `gitea_delete` | Destructive DELETE | `DeleteRepo`, `DeleteBranch` |
| `gitea_admin_read` | Admin-scope GET | `AdminListUsers`, `AdminListRunners` |
| `gitea_admin_write` | Admin-scope writes + admin actions | `AdminCreateUser`, `AdminRunCronJob` |

Each meta-tool takes `operation` (PascalCase op name, or `help` / `schema`) plus `params` (dict):

```
gitea_read(operation="help")                                                # list every op in this group
gitea_read(operation="help", params={"search": "merge"})                    # filter by substring; surfaces cross-group hits
gitea_read(operation="schema", params={"op": "GetRepo"})                    # full JSON Schema for one op
gitea_read(operation="GetRepo", params={"owner": "alice", "repo": "x"})     # invoke

gitea_write(operation="CreateIssue", params={"owner": "alice", "repo": "x", "title": "Bug", "body": "<brief>repro</brief>"})
gitea_execute(operation="MergePullRequest", params={"owner": "alice", "repo": "x", "index": 7, "merge_type": "squash"})
```

Params are validated strictly via Pydantic: unknown keys, wrong types, and
missing required fields return a contextual error result with field-level detail.

## Creating a Gitea API Token

1. Log in to your Gitea instance.
2. Go to **Settings** > **Applications**.
3. Under *Manage Access Tokens*, enter a token name (e.g. `mcp-server`).
4. Select the permissions your agent needs (read/write on repos, issues, etc.).
5. Click **Generate Token** and copy the value immediately -- it is shown only once.

## Development

`dev.py` is the single gate entry point (dev-script contract): `lint`
(ruff + tackbox), `e2e` (boots the dockerized Gitea, runs the integration
suite), `test` (unit + e2e), `check` (lint + test). Pre-commit and CI both
run `./dev.py check` — install the hook once per clone with
`python dev.py hook`, which points `core.hooksPath` at the tracked
`.githooks/`.

npm scripts cover the docker lifecycle around it:

```bash
# unit tests (no docker, fast)
npm test

# bring up Gitea container + bootstrap admin user + write tests/.env
npm run gitea:up

# run integration tests against the live container
npm run test:integration

# tear down
npm run gitea:down

# one-shot: up + integration + down (exits with the pytest status code)
npm run test:integration:full
```

`npm run gitea:bootstrap` is idempotent — re-running against an already-bootstrapped instance no-ops if `tests/.env` carries a still-valid token, otherwise deletes the named token and creates a fresh one. The bootstrap script (`scripts/bootstrap.py`) is also runnable directly via `uv run python scripts/bootstrap.py`.

`tests/.env` schema:

```
GITEA_URL=http://localhost:3000
GITEA_TOKEN=<sha1>
GITEA_ADMIN_USER=testadmin
GITEA_ADMIN_PASSWORD=testadmin1234
```

Integration tests are gated behind `@pytest.mark.integration` and skipped unless `GITEA_URL` + `GITEA_TOKEN` are present — `npm test` will not require docker.

## License

[MIT](LICENSE)
