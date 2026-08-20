"""Minimal stdio MCP server used by the MCP client test."""

from mcp.server import MCPServer

mcp = MCPServer("tiny")


@mcp.tool()
def lookup_docs(topic: str) -> str:
    """Look up internal documentation for a topic."""
    return f"docs[{topic}]: use the frobnicator"


if __name__ == "__main__":
    mcp.run(transport="stdio")
