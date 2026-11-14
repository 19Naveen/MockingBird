import concurrent.futures
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.request, urllib.error
import shlex

import bridge
import mockingbird_hook as hook


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        settings = dict(DIR=self.root, CACHE=self.root / "cache", AUTO="fake", REAL=False,
                        REDACT=False, REPLAY=False, NAMESPACE="", FAULTS=[], QUIET=True, JOURNAL=None)
        self.settings = patch.multiple(bridge, **settings)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.req = {"model": "test", "messages": [{"role": "user", "content": "private prompt"}]}

    def parallel(self, fn):
        barrier = threading.Barrier(8)

        def run(i):
            barrier.wait(timeout=5)
            return fn(i)
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            return list(pool.map(run, range(8)))

    def server(self):
        srv = bridge.serve(0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{srv.server_port}"

    def post(self, url, req):
        request = urllib.request.Request(url + "/v1/messages", json.dumps(req).encode(),
                                         {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.load(response)

    def test_parser_handles_prose_fences_and_extra_braces(self):
        for text in ('prose {broken} ```json\n{"text":"nested {braces}"}\n``` extra {tail}',
                     '{"text":"hello"} and {"extra":true}'):
            with self.subTest(text=text):
                self.assertIn("text", bridge.parse_answer(text))

    def test_parser_rejects_invalid_shapes(self):
        for text in ('nothing', '{"text":42}', '{"tool_calls":[null]}',
                     '{"tool_calls":"bad"}', '{}', '{"error":"oops","status":"bad"}',
                     '{"raw":[]}', '{"text":"hi","stop":{}}'):
            with self.subTest(text=text), self.assertRaises(ValueError):
                bridge.parse_answer(text)

    def test_validation_survives_optimized_python(self):
        code = 'import bridge; bridge.parse_answer(\'{"text":42}\')'
        out = subprocess.run([sys.executable, "-O", "-c", code], capture_output=True)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b"ValueError", out.stderr)

    def test_concurrent_misses_generate_once(self):
        def generate(*_):
            time.sleep(0.1)
            return {"text": "same answer"}
        with patch.object(bridge, "run_auto", side_effect=generate) as auto:
            replies = self.parallel(lambda i: bridge.get_answer("anthropic", "/v1/messages", self.req, str(i)))
        self.assertEqual(auto.call_count, 1)
        self.assertEqual([s for _, s in replies].count("FRESH"), 1)
        self.assertTrue(all(a == {"text": "same answer"} for a, _ in replies))
        self.assertEqual(len(list(bridge.CACHE.glob("*.json"))), 1)

    def test_failed_generation_is_not_cached(self):
        with patch.object(bridge, "run_auto", return_value={"error": "failed"}) as auto:
            for i in range(2):
                bridge.get_answer("anthropic", "/v1/messages", self.req, str(i))
        self.assertEqual(auto.call_count, 2)
        self.assertFalse(bridge.CACHE.exists())

    def test_redact_bypasses_existing_cache_and_writes_nothing(self):
        bridge.CACHE.mkdir()
        bridge._write(bridge.cache_file(self.req, "anthropic"), {"text": "old private answer"})
        before = list(bridge.CACHE.iterdir())
        with patch.object(bridge, "REDACT", True), patch.object(bridge, "run_auto", return_value={"text": "new"}) as auto:
            self.assertIsNone(bridge.cache_file(self.req))
            for i in range(2):
                self.assertEqual(bridge.get_answer("anthropic", "/v1/messages", self.req, str(i))[0], {"text": "new"})
        self.assertEqual(auto.call_count, 2)
        self.assertEqual(list(bridge.CACHE.iterdir()), before)

    def test_cache_files_are_private_and_writes_are_atomic(self):
        path = self.root / "entry.json"
        self.parallel(lambda i: bridge._write(path, {"text": str(i)}))
        self.assertIn("text", json.loads(path.read_text()))
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_real_concurrent_misses_forward_once(self):
        bridge.REAL = True
        url = self.server()

        def forward(*_):
            time.sleep(0.1)
            return 200, "application/json", b'{"content":[{"type":"text","text":"real"}]}'
        with patch.object(bridge, "forward", side_effect=forward) as upstream:
            replies = self.parallel(lambda _: self.post(url, self.req))
        self.assertEqual(upstream.call_count, 1)
        self.assertTrue(all(r == replies[0] for r in replies))

    def test_real_redact_keeps_no_content_in_cache_journal_or_log(self):
        bridge.REAL, bridge.REDACT = True, True
        bridge.JOURNAL = bridge.dashboard.Journal(self.root / "calls.db")
        url = self.server()
        output = io.StringIO()
        recorded = threading.Event()
        record = bridge.JOURNAL.record

        def finish(*args):
            record(*args)
            recorded.set()
        with patch.object(bridge, "QUIET", False), patch("sys.stderr", output), patch.object(
                bridge, "forward", return_value=(200, "application/json", b'{"text":"private answer"}')), patch.object(
                bridge.JOURNAL, "record", side_effect=finish):
            self.post(url, self.req)
            self.assertTrue(recorded.wait(timeout=5))  # history is recorded after sending the response
        self.assertFalse(bridge.CACHE.exists())
        self.assertNotIn("private", output.getvalue())
        calls = bridge.JOURNAL.calls()
        self.assertEqual(calls[0].label, "[redacted]")
        self.assertEqual(calls[0].model, "[redacted]")
        self.assertEqual(calls[0].key, "")

    def test_hook_requires_opt_in_at_import(self):
        code = ('import sys; before = list(sys.meta_path); import mockingbird_hook; '
                'assert sys.meta_path == before')
        env = {k: v for k, v in os.environ.items() if k != "MOCKINGBIRD_ENABLED"}
        subprocess.run([sys.executable, "-c", code], env=env, check=True)

    def test_hook_requires_opt_in_for_each_redirect(self):
        class URL:
            host, scheme, port = "api.openai.com", "https", None

            def copy_with(self, **kwargs):
                return type("URL", (), kwargs)()
        req = type("Request", (), {"url": URL(), "headers": {}})()
        with patch.object(hook, "bridge_port", return_value=8787), patch.dict(os.environ, {}, clear=True):
            hook._redirect(req)
            self.assertEqual(req.url.host, "api.openai.com")
            os.environ["MOCKINGBIRD_ENABLED"] = "1"
            hook._redirect(req)
            self.assertEqual(req.url.host, "127.0.0.1")

    def test_child_cli_cannot_inherit_hook_opt_in(self):
        with patch.dict(os.environ, MOCKINGBIRD_ENABLED="1", OPENAI_BASE_URL="http://127.0.0.1:8787/v1"), patch.object(
                bridge, "BRIDGE_PORTS", {8787}):
            child = bridge._child_env()
        self.assertNotIn("MOCKINGBIRD_ENABLED", child)
        self.assertNotIn("OPENAI_BASE_URL", child)

    def test_child_cli_preserves_unrelated_local_provider_proxy(self):
        with patch.dict(os.environ, ANTHROPIC_BASE_URL="http://localhost:20128"), patch.object(bridge, "BRIDGE_PORTS", {8787}):
            self.assertEqual(bridge._child_env()["ANTHROPIC_BASE_URL"], "http://localhost:20128")

    def test_global_hook_upgrade_is_guarded_idempotent_and_reversible(self):
        rc = self.root / ".bashrc"
        original = "# existing shell\n"
        rc.write_text(original + '\n# >>> mockingbird >>>\nexport PYTHONPATH="old"\n# <<< mockingbird <<<\n')
        with patch.object(Path, "home", return_value=self.root), patch.object(bridge, "HOOK_DIR", self.root / "hook"):
            bridge.hook_global("install")
            bridge.hook_global("install")
            self.assertEqual(rc.read_text().count("# >>> mockingbird >>>"), 1)
            self.assertIn('"${MOCKINGBIRD_ENABLED:-}" = 1', rc.read_text())
            bridge.hook_global("uninstall")
        self.assertEqual(rc.read_text(), original)

    def test_update_uses_only_release_tag_and_pins_payloads(self):
        urls = []

        def download(url, **_):
            urls.append(url)
            return io.BytesIO(b'{"tag_name":"v99.0.0"}' if len(urls) == 1 else b'VERSION=99.0.0\n')
        with patch("urllib.request.urlopen", side_effect=download), patch.dict(
                os.environ, MOCKINGBIRD_SRC="untrusted", MOCKINGBIRD_BASE="untrusted"), patch.object(
                bridge.subprocess, "run") as run, self.assertRaises(SystemExit) as exit_status:
            run.return_value.returncode = 0
            bridge.update()
        self.assertEqual(exit_status.exception.code, 0)
        self.assertTrue(urls[0].endswith("/releases/latest"))
        self.assertTrue(urls[1].endswith("/v99.0.0/install.sh"))
        self.assertNotIn("MOCKINGBIRD_SRC", run.call_args.kwargs["env"])
        self.assertTrue(run.call_args.kwargs["env"]["MOCKINGBIRD_BASE"].endswith("/v99.0.0"))

    def test_update_rejects_invalid_tag_or_mismatched_installer(self):
        for tag, script in (("main", "VERSION=99.0.0"), ("v99.0.0", "VERSION=1.0.0")):
            with self.subTest(tag=tag), patch("urllib.request.urlopen", side_effect=[
                    io.BytesIO(json.dumps({"tag_name": tag}).encode()), io.BytesIO(script.encode())]), patch.object(
                    bridge.subprocess, "run") as run, self.assertRaises(SystemExit):
                bridge.update()
            run.assert_not_called()

    def test_legacy_selftest(self):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRIDGE_") and k != "MOCKINGBIRD_ENABLED"}
        result = subprocess.run([sys.executable, str(Path(bridge.__file__)), "selftest"], env=env,
                                capture_output=True, text=True, timeout=60)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cache_isolates_backend_effort_api_and_namespace(self):
        key = bridge.request_key(self.req, "anthropic")
        self.assertEqual(key, bridge.request_key({**self.req, "stream": True}, "anthropic"))
        self.assertNotEqual(key, bridge.request_key(self.req, "openai"))
        for command in (bridge.auto_cmd("codex"), bridge.auto_cmd("sonnet[low]"), bridge.auto_cmd("sonnet[high]")):
            with patch.object(bridge, "AUTO", command):
                self.assertNotEqual(key, bridge.request_key(self.req, "anthropic"))
        with patch.object(bridge, "NAMESPACE", "fixture-v1"):
            self.assertNotEqual(key, bridge.request_key(self.req, "anthropic"))
            with patch.object(bridge, "AUTO", bridge.auto_cmd("sonnet[low]")):
                low = bridge.request_key(self.req, "anthropic")
            with patch.object(bridge, "AUTO", bridge.auto_cmd("sonnet[high]")):
                self.assertNotEqual(low, bridge.request_key(self.req, "anthropic"))

    def test_offline_replay_hit_and_miss_never_generate(self):
        with patch.object(bridge, "run_auto", return_value={"text": "saved"}):
            bridge.get_answer("anthropic", "/v1/messages", self.req, "record")
        bridge.REPLAY = True
        with patch.object(bridge, "run_auto", side_effect=AssertionError("must be offline")), patch.object(
                bridge, "wait_for_agent", side_effect=AssertionError("must be offline")):
            self.assertEqual(bridge.get_answer("anthropic", "/v1/messages", self.req, "hit"), ({"text": "saved"}, "CACHE"))
            miss, _ = bridge.get_answer("anthropic", "/v1/messages", {**self.req, "max_tokens": 9}, "miss")
        self.assertEqual(miss["status"], 409)

    def test_offline_real_replays_and_never_forwards_a_miss(self):
        bridge.REAL = True
        url = self.server()
        with patch.object(bridge, "forward", return_value=(200, "application/json", b'{"text":"saved"}')):
            self.post(url, self.req)
        # Wait for the cache writer before changing modes (the response is sent first).
        with bridge.cache_lock(bridge.live_key(self.req, "https://api.anthropic.com", "/v1/messages")):
            bridge.REPLAY = True
        with patch.object(bridge, "forward", side_effect=AssertionError("must be offline")):
            self.assertEqual(self.post(url, self.req), {"text": "saved"})
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post(url, {**self.req, "max_tokens": 9})
        self.assertEqual(error.exception.code, 409)
        error.exception.close()

    def test_real_cache_isolates_azure_deployment_and_api_version(self):
        a = bridge.live_key(self.req, "https://example.openai.azure.com", "/openai/deployments/a/chat/completions?api-version=1")
        for path in ("/openai/deployments/b/chat/completions?api-version=1", "/openai/deployments/a/chat/completions?api-version=2"):
            self.assertNotEqual(a, bridge.live_key(self.req, "https://example.openai.azure.com", path))

    def test_replay_rejects_disabled_cache_and_redaction(self):
        for settings in ({"CACHE": None}, {"REDACT": True}):
            with self.subTest(settings=settings), patch.multiple(bridge, **settings), self.assertRaises(SystemExit):
                bridge.configure(["--replay"])

    def test_scripted_errors_malformed_body_delay_and_exhaustion(self):
        bridge.FAULTS = [{"status": 429, "error": "rate limited"}, {"body": "not-json"}, {"delay": 0.03}]
        url = self.server()
        with patch.object(bridge, "run_auto", return_value={"text": "normal"}) as auto:
            with self.assertRaises(urllib.error.HTTPError) as error:
                self.post(url, self.req)
            self.assertEqual(error.exception.code, 429)
            self.assertIn(b"rate limited", error.exception.read())
            error.exception.close()
            with self.assertRaises(ValueError):
                self.post(url, self.req)
            start = time.monotonic()
            self.assertEqual(self.post(url, self.req)["content"][0]["text"], "normal")
            self.assertGreaterEqual(time.monotonic() - start, 0.03)
            self.post(url, self.req)  # normal replay after fault steps run out
        self.assertEqual(auto.call_count, 1)

    def test_fault_file_validation(self):
        path = self.root / "faults.json"
        for bad in ({}, [1], [{"delay": -1}], [{"delay": float("nan")}], [{"status": 100}],
                    [{"body": {}}], [{"body": "x", "error": "y"}], [{"unknown": 1}]):
            path.write_text(json.dumps(bad))
            with self.subTest(bad=bad), self.assertRaises(SystemExit):
                bridge.configure(["--faults", str(path)])
        path.write_text('[{"delay": 0.1}, {"status": 503}, {"body": "invalid JSON"}]')
        bridge.configure(["--faults", str(path)])
        self.assertEqual(len(bridge.FAULTS), 3)

    def runner(self, code, *options):
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRIDGE_") and k != "MOCKINGBIRD_ENABLED"}
        env.update(BRIDGE_CACHE=str(self.root / "cache"), XDG_STATE_HOME=str(self.root / "state"))
        return subprocess.run([sys.executable, str(Path(bridge.__file__)), "run", *options, "--",
                               sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=30)

    def test_runner_scopes_routing_preserves_exit_and_stops_bridge(self):
        code = ('import os,json,sys; print(json.dumps({k:os.environ[k] for k in '
                '["OPENAI_BASE_URL","MOCKINGBIRD_STATE","PYTHONPATH","OPENAI_API_KEY"]})); sys.exit(7)')
        state_before = bridge.STATE.read_bytes() if bridge.STATE.exists() else None
        result = self.runner(code, "--agent")
        self.assertEqual(result.returncode, 7, result.stderr)
        env = json.loads(result.stdout)
        self.assertEqual(env["OPENAI_API_KEY"], "bridge-dummy")
        self.assertFalse(Path(env["MOCKINGBIRD_STATE"]).exists())
        self.assertFalse(Path(env["PYTHONPATH"].split(os.pathsep)[0]).exists())
        with self.assertRaises(OSError):
            urllib.request.urlopen(env["OPENAI_BASE_URL"].removesuffix("/v1") + "/health", timeout=1)
        self.assertEqual(bridge.STATE.read_bytes() if bridge.STATE.exists() else None, state_before)

    def test_runner_hook_redirects_hardcoded_provider_urls(self):
        (self.root / "httpx.py").write_text('''
class Client:
    def send(self, request, **kw):
        return request.url.host
class AsyncClient:
    async def send(self, request, **kw):
        return request.url.host
''')
        code = f'''
import sys, asyncio
sys.path.insert(0, {str(self.root)!r})
import httpx
class URL:
    host, scheme, port = "api.openai.com", "https", None
    def copy_with(self, **kw):
        return type("URL", (), kw)()
class Request:
    url, headers = URL(), {{}}
assert httpx.Client().send(Request()) == "127.0.0.1"
assert asyncio.run(httpx.AsyncClient().send(Request())) == "127.0.0.1"
'''
        result = self.runner(code, "--agent")
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_runner_command_and_missing_command(self):
        result = self.runner('import sys; print("command ran")', "--agent")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("command ran", result.stdout)
        with self.assertRaises(SystemExit):
            bridge.run_command(["--"])

    def test_runner_passes_command_help_without_intercepting_it(self):
        result = subprocess.run([sys.executable, str(Path(bridge.__file__)), "run", "--agent", "--", sys.executable, "--help"],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("PYTHONPATH", result.stdout)

    def test_scripted_delay_can_trigger_a_client_timeout(self):
        import socket
        bridge.FAULTS = [{"delay": 0.1, "status": 503}]
        url = self.server()
        request = urllib.request.Request(url + "/v1/messages", json.dumps(self.req).encode())
        with self.assertRaises(socket.timeout):
            urllib.request.urlopen(request, timeout=0.02)

    def test_http_body_size_and_length_are_validated_before_reading(self):
        import http.client
        from urllib.parse import urlsplit
        url = urlsplit(self.server())
        for size, expected in ((str(33 * 1024 * 1024), 413), ("-1", 400), ("invalid", 400)):
            with self.subTest(size=size):
                connection = http.client.HTTPConnection(url.hostname, url.port, timeout=2)
                try:
                    connection.request("POST", "/v1/messages", body=b"", headers={"Content-Length": size})
                    response = connection.getresponse()
                    self.assertEqual(response.status, expected)
                    response.read()
                finally:
                    connection.close()

    def test_runner_cleans_up_after_interrupt(self):
        import signal
        env = {k: v for k, v in os.environ.items() if not k.startswith("BRIDGE_")}
        env["XDG_STATE_HOME"] = str(self.root / "state")
        ready = self.root / "ready"
        code = f'import os,time; from pathlib import Path; Path({str(ready)!r}).write_text(os.environ["MOCKINGBIRD_STATE"]); time.sleep(60)'
        process = subprocess.Popen([sys.executable, str(Path(bridge.__file__)), "run", "--agent", "--", sys.executable, "-c", code],
                                   env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 10
            while not ready.exists() and time.monotonic() < deadline:
                time.sleep(0.05)
            self.assertTrue(ready.exists())
            process.send_signal(signal.SIGINT)
            self.assertEqual(process.wait(timeout=10), 130)
            self.assertFalse(Path(ready.read_text()).exists())
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


if __name__ == "__main__":
    unittest.main()
