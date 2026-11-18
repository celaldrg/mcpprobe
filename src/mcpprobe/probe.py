"""Probe a stdio MCP server the way modern and legacy clients connect to it.

Two fresh processes are started:

* the *modern* session sends ``server/discover`` with per-request ``_meta``
  (MCP 2026-07-28 and later, no handshake);
* the *legacy* session sends ``initialize`` / ``notifications/initialized``
  (MCP 2025-11-25 and earlier).

Each session then lists tools, prompts and resources (never calls them), sends
one unknown method, closes stdin and watches the process exit. Everything the
server writes is checked against the stdio transport rules.
"""

from __future__ import annotations

import re
from typing import Any

from . import __version__
from .transport import Reply, StdioServer

MODERN_VERSIONS = ["2026-07-28"]
LEGACY_VERSIONS = ["2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05"]
CLIENT_INFO = {"name": "mcpprobe", "version": __version__}
UNKNOWN_METHOD = "mcpprobe/no-such-method"
METHOD_NOT_FOUND = -32601
UNSUPPORTED_PROTOCOL_VERSION = -32022
TOOL_NAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{1,128}$")
DATE_RE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
MAX_PAGES = 50
LIST_KINDS = (("tools", "tools/list"), ("prompts", "prompts/list"), ("resources", "resources/list"))

ERROR, WARN, INFO = "error", "warn", "info"


class Finding:
    __slots__ = ("level", "code", "message", "era")

    def __init__(self, level: str, code: str, message: str, era: str | None = None) -> None:
        self.level, self.code, self.message, self.era = level, code, message, era

    def to_dict(self) -> dict[str, Any]:
        d = {"level": self.level, "code": self.code, "message": self.message}
        if self.era:
            d["era"] = self.era
        return d


class Report:
    def __init__(self, name: str, argv: list[str]) -> None:
        self.name = name
        self.argv = argv
        self.findings: list[Finding] = []
        self.modern: dict[str, Any] = {"supported": False}
        self.legacy: dict[str, Any] = {"supported": False}
        self.stderr_count = 0
        self.stderr_tail: list[str] = []
        self.first_response_ms: float | None = None
        self.skipped = False

    def add(self, level: str, code: str, message: str, era: str | None = None) -> None:
        for f in self.findings:
            if f.code == code and f.message == message:
                if f.era != era:
                    f.era = None  # same problem in both eras
                return
        self.findings.append(Finding(level, code, message, era))

    @property
    def era(self) -> str:
        m, l = self.modern.get("supported"), self.legacy.get("supported")
        if m and l:
            return "dual-era"
        if m:
            return "modern only"
        if l:
            return "legacy only"
        return "unreachable"

    def count(self, level: str) -> int:
        return sum(1 for f in self.findings if f.level == level)

    def server_info(self) -> dict[str, Any] | None:
        return self.modern.get("serverInfo") or self.legacy.get("serverInfo")

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "command": self.argv,
            "era": self.era,
            "modern": self.modern,
            "legacy": self.legacy,
            "serverInfo": self.server_info(),
            "firstResponseMs": None if self.first_response_ms is None else round(self.first_response_ms),
            "stderrLines": self.stderr_count,
            "findings": [f.to_dict() for f in self.findings],
            "errors": self.count(ERROR),
            "warnings": self.count(WARN),
        }


# --------------------------------------------------------------------------- helpers

def modern_meta(version: str) -> dict[str, Any]:
    return {
        "_meta": {
            "io.modelcontextprotocol/protocolVersion": version,
            "io.modelcontextprotocol/clientInfo": CLIENT_INFO,
            "io.modelcontextprotocol/clientCapabilities": {},
        }
    }


def short(value: Any, limit: int = 60) -> str:
    text = value if isinstance(value, str) else repr(value)
    text = text.replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def describe_error(err: Any) -> str:
    if not isinstance(err, dict):
        return short(err)
    code, msg = err.get("code"), err.get("message")
    return f"{code} {short(msg, 50)}" if msg else str(code)


def is_object(v: Any) -> bool:
    return isinstance(v, dict)


def check_implementation(rep: Report, info: Any, where: str, era: str) -> dict[str, Any] | None:
    if not is_object(info):
        return None
    for key in ("name", "version"):
        if not isinstance(info.get(key), str) or not info.get(key):
            rep.add(ERROR, "server-info", f'{where} is missing "{key}" (Implementation requires name and version)', era)
    return {k: info[k] for k in ("name", "version", "title") if isinstance(info.get(k), str)}


def check_modern_result(rep: Report, res: dict[str, Any], method: str, cacheable: bool) -> None:
    era = "modern"
    if "resultType" not in res:
        rep.add(ERROR, "result-type", f'{method} result has no "resultType" (2026-07-28 servers MUST include it)', era)
    elif res.get("resultType") not in ("complete", "input_required"):
        rep.add(WARN, "result-type", f'{method} result has unexpected resultType {short(res.get("resultType"))}', era)
    if not cacheable:
        return
    ttl = res.get("ttlMs")
    if ttl is None:
        rep.add(ERROR, "cache-hint", f'{method} result has no "ttlMs" cache hint', era)
    elif isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or ttl < 0:
        rep.add(ERROR, "cache-hint", f'{method} "ttlMs" must be a number >= 0, got {short(ttl)}', era)
    scope = res.get("cacheScope")
    if scope is None:
        rep.add(ERROR, "cache-hint", f'{method} result has no "cacheScope" ("public" or "private")', era)
    elif scope not in ("public", "private"):
        rep.add(ERROR, "cache-hint", f'{method} "cacheScope" must be "public" or "private", got {short(scope)}', era)


# --------------------------------------------------------------------------- item checks

def check_tools(rep: Report, tools: list[Any], era: str) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for i, tool in enumerate(tools):
        if not is_object(tool):
            rep.add(ERROR, "tool-shape", f"tools[{i}] is not an object", era)
            continue
        name = tool.get("name")
        if not isinstance(name, str) or not name:
            rep.add(ERROR, "tool-shape", f'tools[{i}] has no "name"', era)
            continue
        label = f"tool {name!r}"
        names.append(name)
        if name in seen:
            rep.add(ERROR, "tool-duplicate", f"{label} is listed twice; clients keep one or reject both", era)
        seen.add(name)
        if not TOOL_NAME_RE.match(name):
            rep.add(WARN, "tool-name", f"{label}: names SHOULD be 1-128 chars of A-Z a-z 0-9 _ - .", era)
        schema = tool.get("inputSchema")
        if schema is None:
            rep.add(ERROR, "input-schema", f'{label} has no "inputSchema" (required, even with no parameters)', era)
        elif not is_object(schema):
            rep.add(ERROR, "input-schema", f'{label}: "inputSchema" must be an object', era)
        elif schema.get("type") != "object":
            got = "missing" if "type" not in schema else short(schema.get("type"))
            rep.add(ERROR, "input-schema", f'{label}: inputSchema "type" must be "object" (got {got})', era)
        elif "properties" in schema and not is_object(schema.get("properties")):
            rep.add(ERROR, "input-schema", f'{label}: inputSchema "properties" must be an object', era)
        out = tool.get("outputSchema")
        if out is not None and not is_object(out):
            rep.add(ERROR, "output-schema", f'{label}: "outputSchema" must be an object', era)
        desc = tool.get("description")
        if desc is None or (isinstance(desc, str) and not desc.strip()):
            rep.add(WARN, "tool-description", f"{label} has no description; models pick tools by it", era)
        elif not isinstance(desc, str):
            rep.add(ERROR, "tool-shape", f'{label}: "description" must be a string', era)
        ann = tool.get("annotations")
        if ann is not None:
            if not is_object(ann):
                rep.add(ERROR, "tool-annotations", f'{label}: "annotations" must be an object', era)
            else:
                for key in ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint"):
                    if key in ann and not isinstance(ann[key], bool):
                        rep.add(ERROR, "tool-annotations", f"{label}: annotations.{key} must be a boolean, got {short(ann[key])}", era)
    return names


def check_prompts(rep: Report, prompts: list[Any], era: str) -> list[str]:
    names = []
    for i, p in enumerate(prompts):
        if not is_object(p) or not isinstance(p.get("name"), str) or not p.get("name"):
            rep.add(ERROR, "prompt-shape", f'prompts[{i}] has no "name"', era)
            continue
        names.append(p["name"])
        args = p.get("arguments")
        if args is not None and not isinstance(args, list):
            rep.add(ERROR, "prompt-shape", f'prompt {p["name"]!r}: "arguments" must be an array', era)
    return names


def check_resources(rep: Report, resources: list[Any], era: str) -> list[str]:
    uris = []
    for i, r in enumerate(resources):
        if not is_object(r):
            rep.add(ERROR, "resource-shape", f"resources[{i}] is not an object", era)
            continue
        if not isinstance(r.get("uri"), str) or not r.get("uri"):
            rep.add(ERROR, "resource-shape", f'resources[{i}] has no "uri"', era)
            continue
        if not isinstance(r.get("name"), str) or not r.get("name"):
            rep.add(ERROR, "resource-shape", f'resource {r["uri"]!r} has no "name" (required)', era)
        uris.append(r["uri"])
    return uris


CHECKERS = {"tools": check_tools, "prompts": check_prompts, "resources": check_resources}


def list_all(rep: Report, srv: StdioServer, kind: str, method: str, era: str,
             params_for, timeout: float, advertised: bool) -> list[str] | None:
    items: list[Any] = []
    cursor = None
    seen_cursors: set[str] = set()
    for page in range(MAX_PAGES):
        params = params_for()
        if cursor is not None:
            params["cursor"] = cursor
        r = srv.request(method, params, timeout)
        if r.status != "result":
            if advertised:
                what = {"timeout": f"no reply in {timeout:g}s", "exited": "the server exited"}.get(r.status, describe_error(r.error))
                rep.add(ERROR, "list-failed", f"{kind} capability is advertised but {method} failed: {what}", era)
            return None
        res = r.result
        if not is_object(res) or not isinstance(res.get(kind), list):
            if not advertised:
                return None  # not offered, and the answer is not a list: nothing to check
            rep.add(ERROR, "list-shape", f'{method} result has no "{kind}" array', era)
            return None
        if not advertised and page == 0 and res.get(kind):
            rep.add(WARN, "capability", f"{method} returns {kind} but the {kind} capability is not advertised; clients will not ask", era)
        if era == "modern":
            check_modern_result(rep, res, method, cacheable=True)
        items.extend(res[kind])
        nxt = res.get("nextCursor")
        if nxt is None:
            break
        if not isinstance(nxt, str):
            rep.add(ERROR, "pagination", f'{method} "nextCursor" must be a string', era)
            break
        if nxt in seen_cursors:
            rep.add(ERROR, "pagination", f"{method} returned the same nextCursor twice (infinite pagination)", era)
            break
        seen_cursors.add(nxt)
        cursor = nxt
    else:
        rep.add(WARN, "pagination", f"{method} still had a nextCursor after {MAX_PAGES} pages", era)
    return CHECKERS[kind](rep, items, era)


def list_everything(rep: Report, srv: StdioServer, caps: Any, era: str, params_for, timeout: float) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    caps = caps if is_object(caps) else {}
    for kind, method in LIST_KINDS:
        advertised = kind in caps
        got = list_all(rep, srv, kind, method, era, params_for, timeout, advertised)
        if got is not None:
            out[kind] = got
    return out


def check_unknown_method(rep: Report, srv: StdioServer, params: dict[str, Any] | None, era: str, timeout: float) -> None:
    r = srv.request(UNKNOWN_METHOD, params, timeout)
    if r.status == "result":
        rep.add(ERROR, "unknown-method", f"{UNKNOWN_METHOD} returned a result instead of -32601 Method not found", era)
    elif r.status == "timeout":
        rep.add(WARN, "unknown-method", f"{UNKNOWN_METHOD} got no reply in {timeout:g}s; clients wait until they time out", era)
    elif r.status == "exited":
        rep.add(ERROR, "unknown-method", f"the server exited when it received an unknown method ({UNKNOWN_METHOD})", era)
    elif r.error.get("code") != METHOD_NOT_FOUND:
        rep.add(WARN, "unknown-method", f"{UNKNOWN_METHOD} answered with code {r.error.get('code')}, expected -32601", era)


def transport_findings(rep: Report, srv: StdioServer, era: str, closing_alive: bool) -> None:
    if srv.noise:
        lines = ", ".join(f"{n}: {short(t, 48)!r}" if not t.startswith("(") else f"{n}: {t}" for n, t in srv.noise[:2])
        more = f" (+{len(srv.noise) - 2} more)" if len(srv.noise) > 2 else ""
        rep.add(ERROR, "stdout-noise", f"{len(srv.noise)} stdout line(s) are not JSON-RPC: line {lines}{more}; log to stderr", era)
    for n, why in srv.bad_messages[:3]:
        rep.add(ERROR, "bad-message", f"stdout line {n}: {why}", era)
    for n, rid in srv.unknown_ids[:3]:
        rep.add(ERROR, "unknown-id", f"stdout line {n}: response for id {short(rid)} that was never sent", era)
    if srv.late_replies:
        rep.add(INFO, "late-reply", f"{len(srv.late_replies)} reply(ies) arrived after mcpprobe had given up waiting", era)
    for n, method in srv.server_requests[:3]:
        if era == "modern":
            rep.add(ERROR, "server-request", f"stdout line {n}: server sent a {method!r} request; 2026-07-28 servers MUST NOT send requests", era)
        elif method != "ping":
            rep.add(WARN, "server-request", f"stdout line {n}: server sent a {method!r} request though the client declared no capabilities", era)
    if era == "modern" and "notifications/message" in srv.notifications:
        rep.add(WARN, "log-notification", "server sent notifications/message although no logLevel was requested (MUST NOT)", era)
    if not closing_alive and srv.exit_code not in (None, 0):
        tail = f": {short(srv.stderr_lines[-1], 70)}" if srv.stderr_lines else ""
        rep.add(ERROR, "crashed", f"the server exited with code {srv.exit_code} during the probe{tail}", era)
    elif not closing_alive:
        rep.add(WARN, "exited-early", "the server exited on its own during the probe", era)
    elif srv.exited_promptly is False:
        rep.add(WARN, "slow-exit", "the server did not exit within 2s of stdin closing (SHOULD exit on EOF)", era)
    rep.stderr_count += srv.stderr_count
    if not rep.stderr_tail:
        rep.stderr_tail = srv.stderr_lines[-5:]
    if srv.first_byte_ms is not None and rep.first_response_ms is None:
        rep.first_response_ms = srv.first_byte_ms


def start(rep: Report, argv: list[str], env: dict[str, str] | None, cwd: str | None, era: str) -> StdioServer | None:
    srv = StdioServer(argv, env, cwd)
    if not srv.start():
        rep.add(ERROR, "spawn-failed", f"could not start {short(argv[0] if argv else '', 40)!r}: {srv.spawn_error}")
        return None
    return srv


# --------------------------------------------------------------------------- sessions

def modern_session(rep: Report, argv: list[str], env, cwd, timeout: float) -> bool:
    srv = start(rep, argv, env, cwd, "modern")
    if srv is None:
        return False
    era = "modern"
    m = rep.modern
    version = MODERN_VERSIONS[0]
    r = srv.request("server/discover", modern_meta(version), timeout)
    if r.status == "error" and r.error.get("code") == UNSUPPORTED_PROTOCOL_VERSION:
        data = r.error.get("data") if is_object(r.error.get("data")) else {}
        supported = data.get("supported")
        if not isinstance(supported, list) or not supported:
            rep.add(ERROR, "unsupported-version", 'UnsupportedProtocolVersionError without a "data.supported" list', era)
            supported = []
        m["rejected"] = version
        newer = sorted((v for v in supported if isinstance(v, str) and DATE_RE.fullmatch(v) and v > version), reverse=True)
        if newer:
            version = newer[0]
            rep.add(INFO, "newer-version", f"server only speaks newer modern versions ({', '.join(newer)}); probed with {version}", era)
            r = srv.request("server/discover", modern_meta(version), timeout)
        else:
            m["supported"] = True
            m["supportedVersions"] = supported
            rep.add(ERROR, "unsupported-version", f"server is modern but rejects {version}; it lists {short(supported)}", era)
    if r.status == "result":
        res = r.result if is_object(r.result) else {}
        sv = res.get("supportedVersions")
        if not isinstance(sv, list) or not res:
            rep.add(ERROR, "discover-shape", 'server/discover returned a result without "supportedVersions"', era)
        else:
            m["supported"] = True
            m["version"] = version
            m["supportedVersions"] = sv
            if version not in sv:
                rep.add(WARN, "discover-shape", f"server/discover answered a {version} request but supportedVersions is {short(sv)}", era)
            legacy_listed = [v for v in sv if v in LEGACY_VERSIONS]
            m["listsLegacy"] = legacy_listed
        if m["supported"]:
            caps = res.get("capabilities")
            if not is_object(caps):
                rep.add(ERROR, "discover-shape", 'server/discover result has no "capabilities" object', era)
                caps = {}
            m["capabilities"] = sorted(caps)
            meta = res.get("_meta") if is_object(res.get("_meta")) else {}
            info = meta.get("io.modelcontextprotocol/serverInfo")
            if info is None:
                rep.add(WARN, "server-info", 'server/discover has no _meta "io.modelcontextprotocol/serverInfo" (name, version)', era)
            else:
                m["serverInfo"] = check_implementation(rep, info, "serverInfo", era)
            check_modern_result(rep, res, "server/discover", cacheable=True)
            m["discoverMs"] = round(r.ms)
            m["lists"] = list_everything(rep, srv, caps, era, lambda: modern_meta(version), timeout)
            check_unknown_method(rep, srv, modern_meta(version), era, timeout)
    elif r.status == "error":
        m["discover"] = describe_error(r.error)
    elif r.status == "timeout":
        m["discover"] = "no reply"
    elif r.status == "exited":
        m["discover"] = "exited"
    alive = srv.alive()
    srv.close()
    transport_findings(rep, srv, era, alive)
    return bool(m["supported"])


def legacy_session(rep: Report, argv: list[str], env, cwd, timeout: float) -> bool:
    srv = start(rep, argv, env, cwd, "legacy")
    if srv is None:
        return False
    era = "legacy"
    lg = rep.legacy
    srv.answer_server_requests(True)
    params = {"protocolVersion": LEGACY_VERSIONS[0], "capabilities": {}, "clientInfo": CLIENT_INFO}
    r = srv.request("initialize", params, timeout)
    if r.status == "result" and is_object(r.result):
        res = r.result
        lg["supported"] = True
        pv = res.get("protocolVersion")
        lg["version"] = pv
        if not isinstance(pv, str):
            rep.add(ERROR, "init-shape", 'initialize result has no "protocolVersion"', era)
        elif pv in MODERN_VERSIONS or (DATE_RE.fullmatch(pv) and pv > LEGACY_VERSIONS[0]):
            rep.add(ERROR, "init-version", f"initialize negotiated {pv}, a handshake-free version; legacy clients cannot use it", era)
        elif pv not in LEGACY_VERSIONS:
            rep.add(WARN, "init-version", f"initialize negotiated unknown protocol version {short(pv)}", era)
        caps = res.get("capabilities")
        if not is_object(caps):
            rep.add(ERROR, "init-shape", 'initialize result has no "capabilities" object', era)
            caps = {}
        lg["capabilities"] = sorted(caps)
        info = res.get("serverInfo")
        if info is None:
            rep.add(ERROR, "server-info", 'initialize result has no "serverInfo"', era)
        else:
            lg["serverInfo"] = check_implementation(rep, info, "serverInfo", era)
        lg["initializeMs"] = round(r.ms)
        srv.notify("notifications/initialized")
        p = srv.request("ping", None, timeout)
        if p.status != "result":
            what = {"timeout": "no reply", "exited": "the server exited"}.get(p.status) or describe_error(p.error)
            rep.add(ERROR, "ping", f"ping failed ({what}); legacy clients use it as a health check", era)
        elif not is_object(p.result) or any(k != "_meta" for k in p.result):
            rep.add(WARN, "ping", "ping should return an empty result {}", era)
        lg["lists"] = list_everything(rep, srv, caps, era, dict, timeout)
        check_unknown_method(rep, srv, None, era, timeout)
    elif r.status == "result":
        rep.add(ERROR, "init-shape", "initialize returned a non-object result", era)
    else:
        lg["initialize"] = {"timeout": "no reply", "exited": "exited"}.get(r.status) or describe_error(r.error)
        lg["initializeError"] = r.error if r.status == "error" else None
    alive = srv.alive()
    srv.close()
    transport_findings(rep, srv, era, alive)
    return bool(lg["supported"])


def probe(name: str, argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None,
          timeout: float = 5.0, eras: tuple[str, ...] = ("modern", "legacy")) -> Report:
    rep = Report(name, argv)
    modern = modern_session(rep, argv, env, cwd, timeout) if "modern" in eras else False
    if any(f.code == "spawn-failed" for f in rep.findings):
        return rep
    legacy = legacy_session(rep, argv, env, cwd, timeout) if "legacy" in eras else False
    both = eras == ("modern", "legacy") or set(eras) == {"modern", "legacy"}

    if both and legacy and not modern:
        how = rep.modern.get("discover", "")
        if how == "no reply":
            rep.add(WARN, "discover-silent", f"server/discover went unanswered; dual-era clients wait their full timeout ({timeout:g}s here) before falling back to initialize")
        rep.add(WARN, "legacy-only", "no server/discover: clients that speak only MCP 2026-07-28 cannot use this server")
    if both and modern and not legacy:
        how = rep.legacy.get("initialize", "")
        err = rep.legacy.get("initializeError") or {}
        text = f"{err.get('message', '')} {err.get('data', '')}" if is_object(err) else ""
        if how in ("no reply", "exited"):
            rep.add(WARN, "init-rejection", f"initialize got {'no reply' if how == 'no reply' else 'the process to exit'}; legacy clients hang or fail without a reason")
        elif not DATE_RE.search(text):
            rep.add(WARN, "init-rejection", f"initialize is rejected with {short(how, 40)!r}, which names no supported version (SHOULD, it is all a legacy client can show)")
        rep.add(INFO, "modern-only", "clients on MCP 2025-11-25 or earlier cannot connect (no initialize)")
    if both and modern and legacy:
        mt = set(rep.modern.get("lists", {}).get("tools", []))
        lt = set(rep.legacy.get("lists", {}).get("tools", []))
        if mt != lt and (mt or lt):
            only_m, only_l = sorted(mt - lt), sorted(lt - mt)
            parts = []
            if only_m:
                parts.append("modern only: " + ", ".join(only_m[:4]))
            if only_l:
                parts.append("legacy only: " + ", ".join(only_l[:4]))
            rep.add(WARN, "era-drift", "tools differ between eras (" + "; ".join(parts) + ")")
    if not modern and not legacy:
        rep.add(ERROR, "unreachable", "the server answered neither server/discover (2026-07-28) nor initialize (2025-11-25)")
    return rep
