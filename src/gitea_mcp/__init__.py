from .config import get_settings


def main():
    if get_settings().gitea_compact:
        from gitea_mcp.server_compact import mcp
    else:
        from gitea_mcp.server import mcp
    mcp.run(transport="stdio")
