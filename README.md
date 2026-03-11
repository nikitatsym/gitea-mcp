# gitea-mcp

MCP server for Gitea -- full API coverage for autonomous AI agents.

## Features

- **300 tools** covering the entire Gitea API surface
- Repositories, issues, pull requests, releases, labels, milestones
- File content management (create, read, update, delete)
- Branches, tags, commits, and status checks
- Actions / CI workflows and artifacts
- Organizations, teams, and user management
- Webhooks, deploy keys, notifications, wiki, packages
- Admin endpoints for instance-level operations
- **Compact mode** -- collapse all tools into 6 meta-tools via `GITEA_COMPACT=true`
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
| `GITEA_TOKEN` | Yes | Personal access token with appropriate permissions |
| `GITEA_COMPACT` | No | Set to `true` to enable compact mode (see below) |
| `GITEA_REQUIRE_BRIEF` | No | Require `<brief>summary</brief>` tag in issue body on create/edit (default: `true`) |
| `GITEA_BRIEF_MAX_LENGTH` | No | Max character length for brief summary (default: `200`) |
| `GITEA_FORBID_PUBLIC` | No | Block creating public repos/orgs (default: `true`) |

## Compact Mode

By default, gitea-mcp exposes 300 individual tools. Some MCP clients handle large tool counts poorly (slow startup, context bloat, or hard limits).

**Compact mode** collapses all 300 tools into 6 meta-tools for granular permission control. Set `GITEA_COMPACT=true` to enable it:

```json
{
  "mcpServers": {
    "gitea": {
      "command": "uvx",
      "args": ["--refresh", "--extra-index-url", "https://nikitatsym.github.io/gitea-mcp/simple", "gitea-mcp"],
      "env": {
        "GITEA_URL": "https://gitea.example.com",
        "GITEA_TOKEN": "your-api-token",
        "GITEA_COMPACT": "true"
      }
    }
  }
}
```

| Tool | HTTP | Admin? | Signature |
|---|---|---|---|
| `gitea_read` | GET | no | `(path, params)` |
| `gitea_create` | POST | no | `(path, params)` |
| `gitea_update` | PUT/PATCH | no | `(method, path, params)` |
| `gitea_delete` | DELETE | no | `(path, params)` |
| `gitea_admin_read` | GET | yes | `(path, params)` |
| `gitea_admin_write` | POST/PUT/PATCH/DELETE | yes | `(method, path, params)` |

Usage examples:

```
gitea_read("help")                           → list GET endpoints
gitea_read("/version")                       → get server version
gitea_read("/repos/owner/repo")              → get a repository
gitea_create("/user/repos", '{"name":"my-repo","auto_init":true}')
gitea_update("PATCH", "/repos/owner/repo", '{"description":"updated"}')
gitea_delete("/repos/owner/repo")            → delete a repository
gitea_admin_read("/admin/users")             → list all users (admin)
gitea_admin_write("POST", "/admin/users", '{"username":"new","email":"a@b.c","password":"..."}')
```

All tools accept `path="help"` to list their relevant endpoints. File and wiki content is auto-base64 encoded -- pass plain text in the `"content"` field.

## Creating a Gitea API Token

1. Log in to your Gitea instance.
2. Go to **Settings** > **Applications**.
3. Under *Manage Access Tokens*, enter a token name (e.g. `mcp-server`).
4. Select the permissions your agent needs (read/write on repos, issues, etc.).
5. Click **Generate Token** and copy the value immediately -- it is shown only once.

## Running Tests

The test suite runs against a real Gitea instance managed by Docker Compose.

```bash
# Start Gitea
docker compose -f tests/docker-compose.yml up -d

# Wait for Gitea to be ready, then run tests
uv run pytest tests/ -v

# Tear down
docker compose -f tests/docker-compose.yml down -v
```

## License

[MIT](LICENSE)
