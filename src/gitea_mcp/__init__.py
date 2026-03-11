import sys

from .config import get_settings, set_allow_public


def main():
    if "--allow-public" in sys.argv:
        sys.argv.remove("--allow-public")
        set_allow_public(True)

    if get_settings().gitea_compact:
        from gitea_mcp.server_compact import mcp
    else:
        from gitea_mcp.server import mcp
    mcp.run(transport="stdio")
