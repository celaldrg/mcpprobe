"""A minimal stdio JSON-RPC client that records everything the server does wrong.

The client launches the server as a subprocess, writes one JSON-RPC message per
line to its stdin and reads stdout line by line on a background thread. Every
stdout line that is not a JSON-RPC message is kept as a transport problem
instead of being silently skipped, because real MCP clients will choke on it.
"""

from __future__ import annotations

import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from typing import Any

STDERR_KEEP = 40


class Event:
    __slots__ = ("kind", "data", "line", "at")

    def __init__(self, kind: str, data: Any, line: int, at: float) -> None:
        self.kind = kind  # "message", "noise", "eof"
        self.data = data
        self.line = line
        self.at = at


class Reply:
    """Outcome of a request: result, error, timeout or exited."""

    __slots__ = ("status", "result", "error", "ms")

    def __init__(self, status: str, result: Any = None, error: Any = None, ms: float = 0.0) -> None:
        self.status = status
        self.result = result
        self.error = error
        self.ms = ms

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Reply({self.status}, result={self.result!r}, error={self.error!r})"


def resolve_command(argv: list[str]) -> list[str]:
    """Resolve argv[0] on PATH so that npx/uvx shims work on Windows too."""
    if not argv:
        return argv
    found = shutil.which(argv[0])
    if found:
        return [found] + argv[1:]
    return argv


class StdioServer:
    def __init__(self, argv: list[str], env: dict[str, str] | None = None, cwd: str | None = None) -> None:
        self.argv = argv
        self.env = env
        self.cwd = cwd
        self.proc: subprocess.Popen[bytes] | None = None
        self.events: "queue.Queue[Event]" = queue.Queue()
        self.noise: list[tuple[int, str]] = []
        self.bad_messages: list[tuple[int, str]] = []
        self.server_requests: list[tuple[int, str]] = []
        self.unknown_ids: list[tuple[int, Any]] = []
        self.late_replies: list[tuple[int, Any]] = []
        self._abandoned: set[int] = set()
        self.notifications: list[str] = []
        self.stderr_lines: list[str] = []
        self.stderr_count = 0
        self.started_at = 0.0
        self.first_byte_ms: float | None = None
        self.exit_code: int | None = None
        self.exited_promptly: bool | None = None
        self.spawn_error: str | None = None
        self._next_id = 0
        self._stdout_lines = 0
        self._lock = threading.Lock()
        self._legacy_replies = False
        self.stdout_closed = False
        self.write_failed = False

    # ---------------------------------------------------------------- lifecycle
    def start(self) -> bool:
        merged = dict(os.environ)
        if self.env:
            merged.update(self.env)
        flags = 0
        if sys.platform == "win32":  # pragma: no cover - Windows only
            flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        try:
            self.proc = subprocess.Popen(
                resolve_command(self.argv),
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=merged,
                cwd=self.cwd,
                creationflags=flags,
            )
        except (OSError, ValueError) as exc:
            self.spawn_error = str(exc)
            return False
        self.started_at = time.monotonic()
        threading.Thread(target=self._read_stdout, daemon=True).start()
        threading.Thread(target=self._read_stderr, daemon=True).start()
        return True

    def close(self, grace: float = 2.0) -> None:
        """Close stdin (the spec's shutdown signal) and wait for the process."""
        proc = self.proc
        if proc is None:
            return
        try:
            if proc.stdin and not proc.stdin.closed:
                proc.stdin.close()
        except OSError:
            pass
        already = proc.poll() is not None
        try:
            proc.wait(timeout=grace)
            self.exited_promptly = True if not already else None
        except subprocess.TimeoutExpired:
            self.exited_promptly = False
            proc.terminate()
            try:
                proc.wait(timeout=2)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn process
                proc.kill()
                proc.wait()
        self.exit_code = proc.returncode
        for stream in (proc.stdout, proc.stderr):
            try:
                if stream:
                    stream.close()
            except OSError:  # pragma: no cover
                pass

    def alive(self) -> bool:
        """Still running and still connected: a server whose stdout hit EOF is gone
        for any client, even if the OS has not reaped the process yet."""
        return self.proc is not None and self.proc.poll() is None and not (self.stdout_closed or self.write_failed)

    # ---------------------------------------------------------------- readers
    def _read_stdout(self) -> None:
        assert self.proc and self.proc.stdout
        for raw in self.proc.stdout:
            now = time.monotonic()
            if self.first_byte_ms is None:
                self.first_byte_ms = (now - self.started_at) * 1000
            self._stdout_lines += 1
            n = self._stdout_lines
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                self.noise.append((n, repr(raw[:80])))
                continue
            text = text.rstrip("\r\n")
            if not text.strip():
                self.noise.append((n, "(blank line)"))
                continue
            try:
                msg = json.loads(text)
            except ValueError:
                self.noise.append((n, text))
                continue
            self.events.put(Event("message", msg, n, now))
        self.stdout_closed = True
        self.events.put(Event("eof", None, self._stdout_lines, time.monotonic()))

    def _read_stderr(self) -> None:
        assert self.proc and self.proc.stderr
        for raw in self.proc.stderr:
            self.stderr_count += 1
            if len(self.stderr_lines) < STDERR_KEEP:
                self.stderr_lines.append(raw.decode("utf-8", "replace").rstrip("\r\n"))

    # ---------------------------------------------------------------- sending
    def _write(self, msg: dict[str, Any]) -> bool:
        if not self.proc or not self.proc.stdin:
            return False
        data = (json.dumps(msg, separators=(",", ":")) + "\n").encode("utf-8")
        try:
            with self._lock:
                self.proc.stdin.write(data)
                self.proc.stdin.flush()
            return True
        except (OSError, ValueError):
            self.write_failed = True  # broken pipe: the server is gone
            return False

    def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        msg: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    def answer_server_requests(self, on: bool) -> None:
        """Legacy servers may send requests (ping, roots/list); reply with an error."""
        self._legacy_replies = on

    def request(self, method: str, params: dict[str, Any] | None, timeout: float) -> Reply:
        self._next_id += 1
        rid = self._next_id
        msg: dict[str, Any] = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        sent_at = time.monotonic()
        if not self._write(msg):
            return Reply("exited")
        deadline = sent_at + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                self._abandoned.add(rid)
                return Reply("timeout", ms=timeout * 1000)
            try:
                ev = self.events.get(timeout=left)
            except queue.Empty:
                self._abandoned.add(rid)
                return Reply("timeout", ms=timeout * 1000)
            if ev.kind == "eof":
                self.events.put(ev)  # keep it for later requests
                return Reply("exited", ms=(ev.at - sent_at) * 1000)
            reply = self._classify(ev, rid)
            if reply is not None:
                reply.ms = (ev.at - sent_at) * 1000
                return reply

    def _classify(self, ev: Event, rid: int) -> Reply | None:
        msg = ev.data
        if not isinstance(msg, dict):
            self.bad_messages.append((ev.line, "not a JSON object" if not isinstance(msg, list) else "JSON-RPC batch"))
            return None
        if msg.get("jsonrpc") != "2.0":
            self.bad_messages.append((ev.line, 'missing "jsonrpc": "2.0"'))
            # still try to use it, clients are not that lenient but we want the rest of the report
        has_id = "id" in msg
        if "method" in msg:
            if has_id:
                self.server_requests.append((ev.line, str(msg.get("method"))))
                if self._legacy_replies:
                    self._write({"jsonrpc": "2.0", "id": msg["id"], "error": {"code": -32601, "message": "Method not found"}})
            else:
                self.notifications.append(str(msg.get("method")))
            return None
        if not has_id:
            self.bad_messages.append((ev.line, "neither a request, a notification nor a response"))
            return None
        if msg["id"] != rid and msg["id"] in self._abandoned:
            self.late_replies.append((ev.line, msg["id"]))
            return None
        if msg["id"] != rid:
            self.unknown_ids.append((ev.line, msg["id"]))
            return None
        has_result, has_error = "result" in msg, "error" in msg
        if has_result and has_error:
            self.bad_messages.append((ev.line, 'response has both "result" and "error"'))
        if has_error:
            err = msg["error"]
            if not isinstance(err, dict) or not isinstance(err.get("code"), int):
                self.bad_messages.append((ev.line, "error object without an integer code"))
                err = {"code": None, "message": str(err)}
            return Reply("error", error=err)
        if has_result:
            return Reply("result", result=msg["result"])
        self.bad_messages.append((ev.line, 'response has neither "result" nor "error"'))
        return Reply("error", error={"code": None, "message": "empty response"})
