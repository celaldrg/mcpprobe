"""Read stdio servers out of MCP client config files.

Supported shapes:

* ``{"mcpServers": {name: {command, args, env, cwd}}}`` (Claude Code ``.mcp.json``,
  Claude Desktop, Cursor, Gemini CLI, Windsurf, ...)
* ``{"servers": {name: {type: "stdio", command, args, env}}}`` (VS Code ``mcp.json``)

``${VAR}`` and ``${VAR:-default}`` are expanded from the environment the way
Claude Code expands them. Remote (url) servers are reported and skipped.
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


class ConfigError(Exception):
    pass


class ServerSpec:
    __slots__ = ("name", "argv", "env", "cwd", "skip")

    def __init__(self, name: str, argv: list[str], env: dict[str, str] | None, cwd: str | None, skip: str | None = None) -> None:
        self.name, self.argv, self.env, self.cwd, self.skip = name, argv, env, cwd, skip


def expand(value: str, environ: dict[str, str] | None = None) -> str:
    environ = os.environ if environ is None else environ

    def sub(m: "re.Match[str]") -> str:
        name, default = m.group(1), m.group(2)
        if name in environ:
            return environ[name]
        return default if default is not None else ""

    return VAR_RE.sub(sub, value)


def strip_jsonc(text: str) -> str:
    """Remove // and /* */ comments and trailing commas (VS Code allows them)."""
    out, i, n, in_str = [], 0, len(text), False
    while i < n:
        c = text[i]
        if in_str:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(text[i + 1])
                i += 2
                continue
            if c == '"':
                in_str = False
            i += 1
            continue
        if c == '"':
            in_str = True
            out.append(c)
            i += 1
        elif text.startswith("//", i):
            while i < n and text[i] != "\n":
                i += 1
        elif text.startswith("/*", i):
            end = text.find("*/", i + 2)
            i = n if end < 0 else end + 2
        else:
            out.append(c)
            i += 1
    return re.sub(r",(\s*[}\]])", r"\1", "".join(out))


def load(path: str, environ: dict[str, str] | None = None) -> list[ServerSpec]:
    try:
        with open(path, encoding="utf-8") as fh:
            raw = fh.read()
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc.strerror or exc}") from exc
    try:
        data = json.loads(raw)
    except ValueError:
        try:
            data = json.loads(strip_jsonc(raw))
        except ValueError as exc:
            raise ConfigError(f"{path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: expected a JSON object")
    servers: Any = data.get("mcpServers")
    if servers is None:
        servers = data.get("servers")
    if servers is None and isinstance(data.get("mcp"), dict):  # VS Code settings.json
        servers = data["mcp"].get("servers")
    if not isinstance(servers, dict):
        raise ConfigError(f'{path}: no "mcpServers" or "servers" object found')
    base = os.path.dirname(os.path.abspath(path))
    specs: list[ServerSpec] = []
    for name, entry in servers.items():
        if not isinstance(entry, dict):
            specs.append(ServerSpec(name, [], None, None, "entry is not an object"))
            continue
        kind = entry.get("type")
        if entry.get("url") or entry.get("serverUrl") or kind in ("http", "sse", "streamable-http"):
            specs.append(ServerSpec(name, [], None, None, "remote server (only stdio is probed)"))
            continue
        cmd = entry.get("command")
        if not isinstance(cmd, str) or not cmd:
            specs.append(ServerSpec(name, [], None, None, 'no "command"'))
            continue
        args = entry.get("args") or []
        if not isinstance(args, list):
            specs.append(ServerSpec(name, [], None, None, '"args" is not an array'))
            continue
        argv = [expand(cmd, environ)] + [expand(str(a), environ) for a in args]
        env = None
        if isinstance(entry.get("env"), dict):
            env = {str(k): expand(str(v), environ) for k, v in entry["env"].items()}
        cwd = entry.get("cwd")
        cwd = os.path.join(base, expand(cwd, environ)) if isinstance(cwd, str) and cwd else base
        specs.append(ServerSpec(name, argv, env, cwd))
    return specs
