"""A legacy-only stdio MCP server with the mistakes real servers ship with.

* prints a banner to stdout (stdout is reserved for JSON-RPC)
* never answers ``server/discover``, so dual-era clients wait for a timeout
* a tool without ``inputSchema``, a tool listed twice, a name with a space
* answers an unknown method with an empty result instead of -32601
"""

import json
import sys

print("notes-mcp v0.9 listening on stdio")  # the classic bug: stdout, not stderr
sys.stdout.flush()

TOOLS = [
    {"name": "add_note", "description": "Save a note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}}},
    {"name": "search notes", "description": "Full-text search", "inputSchema": {"type": "object", "properties": {"q": {"type": "string"}}}},
    {"name": "delete_note", "description": ""},
    {"name": "add_note", "description": "Save a note (v2)", "inputSchema": {"type": "object"}},
]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None or method == "server/discover":
        continue  # silently ignored
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "notes-mcp", "version": "0.9.0"}}})
    elif method == "ping":
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"tools": TOOLS}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "result": {}})
