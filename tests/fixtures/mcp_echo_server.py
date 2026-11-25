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


@server.resource("echo://readme", name="readme", description="Static readme resource", mime_type="text/plain")
def readme() -> str:
    """A static text resource."""
    return "argo echo server readme: ${ECHO_SECRET}"


@server.prompt(name="greet", description="Greet a user by name")
def greet(name: str = "world") -> str:
    return f"Please greet {name} warmly."


if __name__ == "__main__":
    server.run("stdio")
