"""Command line interface for mcpprobe."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import sys
import textwrap
from typing import Any

from . import __version__
from .config import ConfigError, ServerSpec, load
from .probe import ERROR, INFO, WARN, Report, probe

USAGE = """\
usage: mcpprobe [options] -- <command> [args...]
       mcpprobe [options] --config <file> [--server <name>]...

Start a stdio MCP server the way MCP 2026-07-28 clients (server/discover) and
2025-11-25-and-earlier clients (initialize) do, list its tools, prompts and
resources (never calls them) and report protocol era, stdio hygiene and shape
problems.

options:
  -c, --config <file>   probe every stdio server in an MCP config
                        (.mcp.json, claude_desktop_config.json, .cursor/mcp.json,
                        .vscode/mcp.json, ...)
  -s, --server <name>   only probe this server from --config (repeatable)
  --era <modern|legacy> probe only one era (default: both)
  -t, --timeout <sec>   per-request timeout (default: 5)
  --json                machine-readable output
  --strict              exit 1 on warnings too
  -q, --quiet           hide info lines
  --no-color            disable colors (also NO_COLOR=1)
  -V, --version         print version
  -h, --help            show this help

exit codes: 0 ok, 1 problems found, 2 usage error or nothing to probe
"""


class Opts:
    def __init__(self) -> None:
        self.config: str | None = None
        self.servers: list[str] = []
        self.eras: tuple[str, ...] = ("modern", "legacy")
        self.timeout = 5.0
        self.json = False
        self.strict = False
        self.quiet = False
        self.color = True
        self.command: list[str] = []


class UsageError(Exception):
    pass


def parse(argv: list[str]) -> Opts:
    o = Opts()
    i = 0

    def value(flag: str) -> str:
        nonlocal i
        if "=" in argv[i] and argv[i].startswith("--"):
            return argv[i].split("=", 1)[1]
        if i + 1 >= len(argv):
            raise UsageError(f"{flag} needs a value")
        i += 1
        return argv[i]

    while i < len(argv):
        a = argv[i]
        key = a.split("=", 1)[0] if a.startswith("--") else a
        if a == "--":
            o.command = argv[i + 1:]
            break
        if key in ("-h", "--help"):
            raise SystemExit(print(USAGE, end="") or 0)
        if key in ("-V", "--version"):
            raise SystemExit(print(f"mcpprobe {__version__}") or 0)
        if key in ("-c", "--config"):
            o.config = value(key)
        elif key in ("-s", "--server"):
            o.servers.append(value(key))
        elif key == "--era":
            v = value(key)
            if v not in ("modern", "legacy", "both"):
                raise UsageError("--era must be modern, legacy or both")
            o.eras = ("modern", "legacy") if v == "both" else (v,)
        elif key in ("-t", "--timeout"):
            try:
                o.timeout = float(value(key))
            except ValueError:
                raise UsageError("--timeout must be a number of seconds") from None
            if o.timeout <= 0:
                raise UsageError("--timeout must be positive")
        elif key == "--json":
            o.json = True
        elif key == "--strict":
            o.strict = True
        elif key in ("-q", "--quiet"):
            o.quiet = True
        elif key == "--no-color":
            o.color = False
        elif a.startswith("-") and a != "-":
            raise UsageError(f"unknown option {a}")
        else:
            o.command = argv[i:]
            break
        i += 1
    if o.config and o.command:
        raise UsageError("give either --config or a command, not both")
    if not o.config and not o.command:
        raise UsageError("nothing to probe: pass a server command after -- or --config <file>")
    if o.servers and not o.config:
        raise UsageError("--server only works with --config")
    return o


# --------------------------------------------------------------------------- rendering

class Style:
    def __init__(self, on: bool) -> None:
        self.on = on

    def __call__(self, code: str, text: str) -> str:
        return f"\x1b[{code}m{text}\x1b[0m" if self.on else text

    def red(self, t: str) -> str: return self("31", t)
    def yellow(self, t: str) -> str: return self("33", t)
    def green(self, t: str) -> str: return self("32", t)
    def cyan(self, t: str) -> str: return self("36", t)
    def dim(self, t: str) -> str: return self("2", t)
    def bold(self, t: str) -> str: return self("1", t)


def plural(n: int, word: str) -> str:
    return f"{n} {word}" + ("" if n == 1 else "s")


def era_line(rep: Report, st: Style) -> str:
    m, lg = rep.modern, rep.legacy
    era = rep.era
    colored = {"dual-era": st.green, "modern only": st.cyan, "legacy only": st.yellow, "unreachable": st.red}[era](st.bold(era))
    if m.get("supported"):
        mv = m.get("version") or (m.get("supportedVersions") or ["?"])[0]
        mod = st.green("✓") + f" {mv}"
    elif m.get("skipped"):
        mod = st.dim("not probed")
    else:
        mod = st.red("✗") + st.dim(f" server/discover: {m.get('discover', 'failed')}")
    if lg.get("supported"):
        leg = st.green("✓") + f" {lg.get('version')}"
    elif lg.get("skipped"):
        leg = st.dim("not probed")
    else:
        leg = st.red("✗") + st.dim(f" initialize: {lg.get('initialize', 'failed')}")
    return f"{colored}   modern {mod}   legacy {leg}"


def exposes(rep: Report) -> str | None:
    lists = rep.modern.get("lists") or rep.legacy.get("lists")
    if not lists:
        return None
    parts = []
    for kind, word in (("tools", "tool"), ("prompts", "prompt"), ("resources", "resource")):
        if kind in lists:
            parts.append(plural(len(lists[kind]), word))
    return " · ".join(parts) if parts else None


def render(rep: Report, o: Opts, st: Style, width: int) -> str:
    out = []
    cmd = " ".join(shlex.quote(a) for a in rep.argv)
    head = st.bold(rep.name) + "  " + st.dim(cmd) if cmd and rep.name != cmd else st.bold(rep.name or cmd)
    out.append(st.cyan("●") + " " + head)
    if rep.skipped:
        out.append("  " + st.dim("skipped: " + rep.findings[0].message if rep.findings else "skipped"))
        return "\n".join(out)
    label = lambda k: "  " + st.dim(k.ljust(9))  # noqa: E731
    out.append(label("era") + era_line(rep, st))
    info = rep.server_info()
    if info:
        out.append(label("server") + " ".join(x for x in (info.get("name"), info.get("version")) if x))
    ex = exposes(rep)
    if ex:
        out.append(label("exposes") + ex)
    stdio = []
    if rep.first_response_ms is not None:
        stdio.append(f"first reply {rep.first_response_ms:.0f} ms")
    stdio.append(plural(rep.stderr_count, "stderr line"))
    out.append(label("stdio") + " · ".join(stdio))
    shown = [f for f in rep.findings if not (o.quiet and f.level == INFO)]
    if shown:
        out.append("")
        codew = max(len(f.code) for f in shown)
        for f in shown:
            lvl = {ERROR: st.red("error"), WARN: st.yellow("warn "), INFO: st.cyan("info ")}[f.level]
            era = f"[{f.era}] " if f.era else ""
            indent = 2 + 5 + 2 + codew + 2
            body = textwrap.wrap(era + f.message, max(30, width - indent)) or [""]
            out.append(f"  {lvl}  {st.dim(f.code.ljust(codew))}  " + body[0])
            for more in body[1:]:
                out.append(" " * indent + more)
    out.append("")
    e, w = rep.count(ERROR), rep.count(WARN)
    if e:
        out.append("  " + st.red("✗ " + plural(e, "error")) + (f" · {plural(w, 'warning')}" if w else ""))
    elif w:
        out.append("  " + st.yellow("! " + plural(w, "warning")))
    else:
        out.append("  " + st.green("✓ no problems"))
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    try:
        o = parse(argv)
    except UsageError as exc:
        print(f"mcpprobe: {exc}\n", file=sys.stderr)
        print(USAGE, file=sys.stderr, end="")
        return 2
    except SystemExit as exc:
        return int(exc.code or 0)

    if o.config:
        try:
            specs = load(o.config)
        except ConfigError as exc:
            print(f"mcpprobe: {exc}", file=sys.stderr)
            return 2
        if o.servers:
            names = {s.name for s in specs}
            missing = [n for n in o.servers if n not in names]
            if missing:
                print(f"mcpprobe: no server named {', '.join(missing)} in {o.config}", file=sys.stderr)
                return 2
            specs = [s for s in specs if s.name in o.servers]
    else:
        specs = [ServerSpec(" ".join(o.command), o.command, None, None)]

    allowed = o.color and not o.json and not os.environ.get("NO_COLOR")
    forced = os.environ.get("FORCE_COLOR", "") not in ("", "0", "false")
    color = allowed and (forced or sys.stdout.isatty())
    st = Style(color)
    width = min(shutil.get_terminal_size((110, 24)).columns, 120)

    reports: list[Report] = []
    for i, spec in enumerate(specs):
        if spec.skip:
            rep = Report(spec.name, spec.argv)
            rep.skipped = True
            rep.add(INFO, "skipped", spec.skip)
        else:
            rep = probe(spec.name, spec.argv, spec.env, spec.cwd, o.timeout, o.eras)
            for era in ("modern", "legacy"):
                if era not in o.eras:
                    getattr(rep, era)["skipped"] = True
        reports.append(rep)
        if not o.json:
            if i:
                print()
            print(render(rep, o, st, width), flush=True)

    probed = [r for r in reports if not r.skipped]
    errors = sum(r.count(ERROR) for r in probed)
    warnings = sum(r.count(WARN) for r in probed)
    if o.json:
        doc: dict[str, Any] = {"version": __version__, "servers": [r.to_dict() for r in reports],
                               "errors": errors, "warnings": warnings}
        print(json.dumps(doc, indent=2, ensure_ascii=False))
    elif len(reports) > 1:
        eras: dict[str, int] = {}
        for r in probed:
            eras[r.era] = eras.get(r.era, 0) + 1
        mix = ", ".join(f"{n} {e}" for e, n in eras.items())
        skipped = len(reports) - len(probed)
        print()
        print(st.bold(f"{plural(len(probed), 'server')} probed") + (f" ({mix})" if mix else "")
              + (st.dim(f", {skipped} skipped") if skipped else "")
              + f" · {plural(errors, 'error')} · {plural(warnings, 'warning')}")
    if not probed:
        return 2
    if errors or (o.strict and warnings):
        return 1
    return 0
