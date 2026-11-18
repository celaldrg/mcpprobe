"""A well-behaved stdio MCP server that speaks both eras.

* MCP 2026-07-28 clients call ``server/discover`` and send ``_meta`` on every request.
* MCP 2025-11-25 and earlier clients do the ``initialize`` handshake.

Standard library only. Logs go to stderr, never stdout.
"""

import json
import sys

MODERN = "2026-07-28"
LEGACY = ["2025-11-25", "2025-06-18", "2025-03-26"]
INFO = {"name": "weather-dual", "version": "1.4.0"}
CACHE = {"ttlMs": 300000, "cacheScope": "public"}

TOOLS = [
    {
        "name": "get_forecast",
        "description": "Daily forecast for a city, up to 7 days ahead.",
        "inputSchema": {
            "type": "object",
            "properties": {"city": {"type": "string"}, "days": {"type": "integer", "minimum": 1, "maximum": 7}},
            "required": ["city"],
        },
        "annotations": {"readOnlyHint": True, "openWorldHint": True},
    },
    {
        "name": "get_alerts",
        "description": "Active severe-weather alerts for a region code.",
        "inputSchema": {"type": "object", "properties": {"region": {"type": "string"}}, "required": ["region"]},
        "annotations": {"readOnlyHint": True},
    },
]
PROMPTS = [{"name": "packing_list", "description": "What to pack for a trip", "arguments": [{"name": "city", "required": True}]}]


def send(msg):
    sys.stdout.write(json.dumps(msg) + "\n")
    sys.stdout.flush()


def modern(params):
    meta = (params or {}).get("_meta") or {}
    return meta.get("io.modelcontextprotocol/protocolVersion")


def handle(req):
    method, params, rid = req.get("method"), req.get("params") or {}, req.get("id")
    if rid is None:
        return  # notification
    version = modern(params)
    if version is not None and version != MODERN:
        send({"jsonrpc": "2.0", "id": rid, "error": {
            "code": -32022, "message": "Unsupported protocol version", "data": {"supported": [MODERN]}}})
        return
    extra = dict(CACHE, resultType="complete") if version else {}
    if method == "server/discover":
        result = {"resultType": "complete", "supportedVersions": [MODERN] + LEGACY,
                  "capabilities": {"tools": {}, "prompts": {}},
                  "_meta": {"io.modelcontextprotocol/serverInfo": INFO}, **CACHE}
    elif method == "initialize":
        asked = params.get("protocolVersion")
        result = {"protocolVersion": asked if asked in LEGACY else LEGACY[0],
                  "capabilities": {"tools": {}, "prompts": {}}, "serverInfo": INFO}
    elif method == "ping":
        result = {}
    elif method == "tools/list":
        result = {"tools": TOOLS, **extra}
    elif method == "prompts/list":
        result = {"prompts": PROMPTS, **extra}
    else:
        send({"jsonrpc": "2.0", "id": rid, "error": {"code": -32601, "message": f"Method not found: {method}"}})
        return
    send({"jsonrpc": "2.0", "id": rid, "result": result})


def main():
    print("weather-dual ready", file=sys.stderr, flush=True)
    for line in sys.stdin:
        line = line.strip()
        if line:
            handle(json.loads(line))


if __name__ == "__main__":
    main()
