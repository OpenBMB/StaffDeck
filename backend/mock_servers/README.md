# MCP mock server

`mcp_stdio_server.py` is a real line-delimited JSON-RPC MCP mock server over stdio. It supports:

- `initialize`
- `notifications/initialized`
- `tools/list`
- `tools/call`

Available tools:

- `echo`
- `sum`
- `product_lookup`

Example tool config:

```json
{
  "tool_type": "mcp",
  "url": "mcp://stdio/mock/product_lookup",
  "mcp_config": {
    "transport": "stdio",
    "command": "python",
    "args": ["/Users/hm/Documents/StaffDeck/backend/mock_servers/mcp_stdio_server.py"],
    "tool": "product_lookup"
  }
}
```

For a real MCP server, keep the same config shape and replace `command`, `args`, and `tool`.
