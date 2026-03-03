import os


def main():
    if os.environ.get("GITEA_COMPACT", "").lower() in ("1", "true", "yes"):
        from gitea_mcp.server_compact import mcp
    else:
        from gitea_mcp.server import mcp
    mcp.run(transport="stdio")
