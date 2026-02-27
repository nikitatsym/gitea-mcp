from gitea_mcp.server import mcp


def main():
    mcp.run(transport="stdio")
