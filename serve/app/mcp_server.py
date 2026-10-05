"""carmind MCP server: the call-desk tools over streamable HTTP on port 7601, path /mcp."""
from mcp.server.fastmcp import FastMCP

from app import desk, kb

server = FastMCP("carmind", host="0.0.0.0", port=7601, stateless_http=True, json_response=True, log_level="WARNING")
for _fn in desk.TOOLS.values():
    server.add_tool(_fn)


def main() -> None:
    kb.indicators()  # validate the knowledge base before serving
    server.run(transport="streamable-http")


if __name__ == "__main__":
    main()
