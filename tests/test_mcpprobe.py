import io
import json
import os
import sys
import tempfile
import textwrap
import unittest
from contextlib import redirect_stdout, redirect_stderr

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))

from mcpprobe import config  # noqa: E402
from mcpprobe.cli import main  # noqa: E402
from mcpprobe.probe import probe  # noqa: E402

SERVERS = os.path.join(ROOT, "examples", "servers")
PY = sys.executable


def server(name):
    return [PY, os.path.join(SERVERS, name)]


def codes(rep, level=None):
    return {f.code for f in rep.findings if level is None or f.level == level}


def script(body):
    fd, path = tempfile.mkstemp(suffix=".py")
    with os.fdopen(fd, "w") as fh:
        fh.write(textwrap.dedent(body))
    return path


def run_cli(*args):
    out, err = io.StringIO(), io.StringIO()
    with redirect_stdout(out), redirect_stderr(err):
        code = main(list(args))
    return code, out.getvalue(), err.getvalue()


class ExampleServers(unittest.TestCase):
    def test_dual_era_server_is_clean(self):
        rep = probe("weather", server("weather_dual.py"), timeout=3)
        self.assertEqual(rep.era, "dual-era")
        self.assertEqual(rep.findings, [])
        self.assertEqual(rep.modern["version"], "2026-07-28")
        self.assertEqual(rep.legacy["version"], "2025-11-25")
        self.assertEqual(rep.modern["lists"]["tools"], ["get_forecast", "get_alerts"])
        self.assertEqual(rep.server_info(), {"name": "weather-dual", "version": "1.4.0"})

    def test_legacy_server_problems(self):
        rep = probe("notes", server("notes_legacy.py"), timeout=1)
        self.assertEqual(rep.era, "legacy only")
        self.assertEqual(rep.legacy["version"], "2025-06-18")
        found = codes(rep)
        for code in ("stdout-noise", "input-schema", "tool-duplicate", "tool-name",
                     "tool-description", "unknown-method", "discover-silent", "legacy-only"):
            self.assertIn(code, found)
        self.assertNotIn("list-shape", found)  # prompts/resources not advertised
        noise = [f for f in rep.findings if f.code == "stdout-noise"][0]
        self.assertIsNone(noise.era)  # seen in both sessions
        self.assertIn("listening on stdio", noise.message)

    def test_modern_server_problems(self):
        rep = probe("files", server("files_modern.py"), timeout=2)
        self.assertEqual(rep.era, "modern only")
        msgs = " ".join(f.message for f in rep.findings)
        self.assertIn('tools/list result has no "ttlMs"', msgs)
        self.assertIn('"cacheScope" must be "public" or "private", got shared', msgs)
        self.assertIn("readOnlyHint must be a boolean", msgs)
        self.assertIn("log-notification", codes(rep, "warn"))
        self.assertIn("init-rejection", codes(rep, "warn"))
        self.assertIn("modern-only", codes(rep, "info"))

    def test_single_era(self):
        rep = probe("files", server("files_modern.py"), timeout=2, eras=("modern",))
        self.assertEqual(rep.era, "modern only")
        self.assertNotIn("init-rejection", codes(rep))
        self.assertNotIn("modern-only", codes(rep))


class Transport(unittest.TestCase):
    def test_spawn_failure(self):
        rep = probe("x", ["mcpprobe-definitely-not-installed"], timeout=1)
        self.assertEqual(rep.era, "unreachable")
        self.assertEqual(codes(rep), {"spawn-failed"})

    def test_crash_on_start(self):
        path = script("""
            import sys
            print("Error: API_KEY is not set", file=sys.stderr)
            sys.exit(3)
        """)
        try:
            rep = probe("crash", [PY, path], timeout=1)
        finally:
            os.unlink(path)
        self.assertEqual(rep.era, "unreachable")
        crashed = [f for f in rep.findings if f.code == "crashed"]
        self.assertTrue(crashed)
        self.assertIn("code 3", crashed[0].message)
        self.assertIn("API_KEY is not set", crashed[0].message)
        self.assertIn("unreachable", codes(rep, "error"))

    def test_wrong_id_and_missing_jsonrpc(self):
        path = script("""
            import json, sys
            for line in sys.stdin:
                req = json.loads(line)
                if req.get("id") is None:
                    continue
                if req["method"] == "initialize":
                    print(json.dumps({"jsonrpc": "2.0", "id": 999, "result": {}}), flush=True)
                    print(json.dumps({"id": req["id"], "result": {"protocolVersion": "2025-11-25",
                          "capabilities": {}, "serverInfo": {"name": "x"}}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"],
                          "error": {"code": -32601, "message": "nope"}}), flush=True)
        """)
        try:
            rep = probe("odd", [PY, path], timeout=1, eras=("legacy",))
        finally:
            os.unlink(path)
        msgs = " ".join(f.message for f in rep.findings)
        self.assertIn("unknown-id", codes(rep))
        self.assertIn('missing "jsonrpc": "2.0"', msgs)
        self.assertIn('serverInfo is missing "version"', msgs)

    def test_slow_exit(self):
        path = script("""
            import json, sys, time
            for line in sys.stdin:
                req = json.loads(line)
                if req.get("id") is None:
                    continue
                if req["method"] == "initialize":
                    res = {"protocolVersion": "2025-11-25", "capabilities": {}, "serverInfo": {"name": "s", "version": "1"}}
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}), flush=True)
                elif req["method"] == "ping":
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "x"}}), flush=True)
            time.sleep(30)
        """)
        try:
            rep = probe("slow", [PY, path], timeout=1, eras=("legacy",))
        finally:
            os.unlink(path)
        self.assertIn("slow-exit", codes(rep, "warn"))

    def test_unsupported_version_retry(self):
        path = script("""
            import json, sys
            for line in sys.stdin:
                req = json.loads(line)
                if req.get("id") is None:
                    continue
                v = (req.get("params") or {}).get("_meta", {}).get("io.modelcontextprotocol/protocolVersion")
                if v != "2099-01-01":
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32022,
                          "message": "Unsupported protocol version", "data": {"supported": ["2099-01-01"]}}}), flush=True)
                elif req["method"] == "server/discover":
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": {"resultType": "complete",
                          "supportedVersions": ["2099-01-01"], "capabilities": {}, "ttlMs": 0, "cacheScope": "public",
                          "_meta": {"io.modelcontextprotocol/serverInfo": {"name": "future", "version": "9"}}}}), flush=True)
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "x"}}), flush=True)
        """)
        try:
            rep = probe("future", [PY, path], timeout=1, eras=("modern",))
        finally:
            os.unlink(path)
        self.assertTrue(rep.modern["supported"])
        self.assertEqual(rep.modern["version"], "2099-01-01")
        self.assertIn("newer-version", codes(rep, "info"))
        self.assertEqual(codes(rep, "error"), set())


class Config(unittest.TestCase):
    def write(self, text, name="mcp.json"):
        d = tempfile.mkdtemp()
        path = os.path.join(d, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path

    def test_mcp_servers_shape_and_env(self):
        path = self.write(json.dumps({"mcpServers": {
            "a": {"command": "node", "args": ["${ROOT:-/srv}/a.js", "--key=${KEY}"], "env": {"T": "${TOKEN}"}},
            "r": {"url": "https://example.com/mcp"},
            "bad": {"args": ["x"]},
        }}))
        specs = {s.name: s for s in config.load(path, {"KEY": "k1", "TOKEN": "t"})}
        self.assertEqual(specs["a"].argv, ["node", "/srv/a.js", "--key=k1"])
        self.assertEqual(specs["a"].env, {"T": "t"})
        self.assertEqual(specs["a"].cwd, os.path.dirname(path))
        self.assertIn("remote", specs["r"].skip)
        self.assertEqual(specs["bad"].skip, 'no "command"')

    def test_vscode_jsonc(self):
        path = self.write(textwrap.dedent("""\
            {
              // VS Code allows comments
              "servers": {
                "fs": { "type": "stdio", "command": "npx", "args": ["-y", "pkg"], }, /* trailing */
                "gh": { "type": "http", "url": "https://api.example.com/mcp" },
              },
            }
        """))
        specs = config.load(path, {})
        self.assertEqual([s.argv for s in specs if not s.skip], [["npx", "-y", "pkg"]])
        self.assertEqual(len([s for s in specs if s.skip]), 1)

    def test_vscode_settings(self):
        path = self.write(json.dumps({"mcp": {"servers": {"x": {"command": "x"}}}}), "settings.json")
        self.assertEqual(config.load(path, {})[0].argv, ["x"])

    def test_errors(self):
        with self.assertRaises(config.ConfigError):
            config.load(self.write("{nope"))
        with self.assertRaises(config.ConfigError):
            config.load(self.write('{"other": {}}'))
        with self.assertRaises(config.ConfigError):
            config.load("/definitely/not/here.json")

    def test_comment_markers_inside_strings_survive(self):
        text = '{"mcpServers": {"u": {"command": "curl", "args": ["http://x//y", "/*keep*/"],}}}'
        self.assertEqual(config.load(self.write(text), {})[0].argv, ["curl", "http://x//y", "/*keep*/"])


class Cli(unittest.TestCase):
    def test_usage_errors(self):
        self.assertEqual(run_cli()[0], 2)
        self.assertEqual(run_cli("--bogus")[0], 2)
        self.assertEqual(run_cli("--server", "x", "--", "y")[0], 2)
        self.assertEqual(run_cli("--timeout", "0", "--", "y")[0], 2)
        self.assertEqual(run_cli("--era", "future", "--", "y")[0], 2)

    def test_version_and_help(self):
        code, out, _ = run_cli("--version")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("mcpprobe "))
        code, out, _ = run_cli("--help")
        self.assertEqual(code, 0)
        self.assertIn("usage: mcpprobe", out)

    def test_clean_server_exit_zero(self):
        code, out, _ = run_cli("--no-color", "--", *server("weather_dual.py"))
        self.assertEqual(code, 0, out)
        self.assertIn("dual-era", out)
        self.assertIn("no problems", out)
        self.assertNotIn("\x1b[", out)

    def test_json_and_config(self):
        cfg = os.path.join(ROOT, "examples", "mcp.json")
        code, out, _ = run_cli("--json", "-t", "1", "--config", cfg, "-s", "files", "-s", "linear")
        self.assertEqual(code, 1)
        doc = json.loads(out)
        self.assertEqual([s["name"] for s in doc["servers"]], ["files", "linear"])
        self.assertEqual(doc["servers"][0]["era"], "modern only")
        self.assertGreater(doc["errors"], 0)
        self.assertTrue(doc["servers"][1].get("skipped") or doc["servers"][1]["era"] == "unreachable")

    def test_unknown_server_name(self):
        cfg = os.path.join(ROOT, "examples", "mcp.json")
        code, _, err = run_cli("--config", cfg, "-s", "nope")
        self.assertEqual(code, 2)
        self.assertIn("no server named nope", err)

    def test_strict(self):
        path = script("""
            import json, sys
            for line in sys.stdin:
                req = json.loads(line)
                if req.get("id") is None:
                    continue
                if req["method"] == "initialize":
                    res = {"protocolVersion": "2025-11-25", "capabilities": {"tools": {}}, "serverInfo": {"name": "s", "version": "1"}}
                elif req["method"] == "ping":
                    res = {}
                elif req["method"] == "tools/list":
                    res = {"tools": [{"name": "t", "inputSchema": {"type": "object"}}]}
                else:
                    print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "error": {"code": -32601, "message": "x"}}), flush=True)
                    continue
                print(json.dumps({"jsonrpc": "2.0", "id": req["id"], "result": res}), flush=True)
        """)
        try:
            self.assertEqual(run_cli("--era", "legacy", "-t", "1", "--", PY, path)[0], 0)
            self.assertEqual(run_cli("--era", "legacy", "-t", "1", "--strict", "--", PY, path)[0], 1)
        finally:
            os.unlink(path)


if __name__ == "__main__":
    unittest.main()
