"""A modern-only (MCP 2026-07-28) stdio server with spec mistakes.

* list results without the ``ttlMs`` / ``cacheScope`` cache hints
* a log notification nobody asked for
* ``initialize`` rejected with a bare -32601 that names no supported version
* a tool annotation that is a string instead of a boolean
"""

import json
import sys

MODERN = "2026-07-28"


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


for line in sys.stdin:
    if not line.strip():
        continue
    req = json.loads(line)
    method, rid = req.get("method"), req.get("id")
    if rid is None:
        continue
    if method == "server/discover":
        send({"jsonrpc": "2.0", "method": "notifications/message", "params": {"level": "info", "data": "client connected"}})
        send({"jsonrpc": "2.0", "id": rid, "result": {
            "resultType": "complete", "supportedVersions": [MODERN], "capabilities": {"tools": {}, "resources": {}},
            "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "files-modern", "version": "2.0.0-beta.1"}},
            "ttlMs": 60000, "cacheScope": "public"}})
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", "tools": [
            {"name": "read_file", "description": "Read a file under the workspace root",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]},
             "annotations": {"readOnlyHint": "yes"}},
            {"name": "write_file", "description": "Write a file under the workspace root",
             "inputSchema": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}}},
             "annotations": {"destructiveHint": True}},
        ]}})
    elif method == "resources/list":
        send({"jsonrpc": "2.0", "id": rid, "result": {"resultType": "complete", "resources": [
            {"uri": "file:///workspace/README.md", "name": "README.md"}], "ttlMs": 5000, "cacheScope": "shared"}})
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": "Method not found"}})
