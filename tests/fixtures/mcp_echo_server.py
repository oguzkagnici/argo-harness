"""Minimal stdio MCP server used by tests (official SDK v2)."""
from mcp.server.mcpserver import MCPServer

server = MCPServer("echo")


@server.tool()
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


@server.tool()
def reverse(text: str) -> str:
    """Reverse a string."""
    return text[::-1]


if __name__ == "__main__":
    server.run("stdio")
