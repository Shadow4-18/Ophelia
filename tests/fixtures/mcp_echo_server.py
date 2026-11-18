"""A tiny MCP server for the bridge tests (stdio)."""

import os
import sys

try:  # mcp 2.x
    from mcp.server.mcpserver import MCPServer as Server
except ImportError:  # mcp 1.x
    from mcp.server.fastmcp import FastMCP as Server

server = Server("echo")
CALLS = {"n": 0}


@server.tool()
def echo(text: str) -> str:
    """Repeat the text back."""
    return f"echo: {text}"


@server.tool()
def count() -> str:
    """How many calls this process has served (proves the connection stays open)."""
    CALLS["n"] += 1
    return f"pid {os.getpid()} call {CALLS['n']}"


@server.tool()
def secret() -> str:
    """Read an env var passed in through mcp.json."""
    return os.environ.get("ECHO_SECRET", "unset")


@server.tool()
def die() -> str:
    """Exit the process (to test reconnecting)."""
    sys.stdout.flush()
    os._exit(0)


if __name__ == "__main__":
    server.run()
