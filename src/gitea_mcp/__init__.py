import sys

from .config import set_allow_public


def main():
    if "--allow-public" in sys.argv:
        sys.argv.remove("--allow-public")
        set_allow_public(True)

    from .server import mcp
    mcp.run(transport="stdio")
