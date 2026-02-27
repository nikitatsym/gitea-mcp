# gitea-mcp

MCP server for Gitea -- full API coverage for autonomous AI agents.

## Features

- **186 tools** covering the entire Gitea API surface
- Repositories, issues, pull requests, releases, labels, milestones
- File content management (create, read, update, delete)
- Branches, tags, commits, and status checks
- Actions / CI workflows and artifacts
- Organizations, teams, and user management
- Webhooks, deploy keys, notifications, wiki, packages
- Admin endpoints for instance-level operations
- Zero-config install via `uvx`

## Quick Start

Add the following to your MCP client configuration (Claude Desktop, Cursor, Claude Code, etc.):

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
