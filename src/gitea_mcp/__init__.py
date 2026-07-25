import sys

from .config import set_allow_public
from .server import mcp


def main():
    if "--allow-public" in sys.argv:
        sys.argv.remove("--allow-public")
        set_allow_public(True)

    mcp.run(transport="stdio")
