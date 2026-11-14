"""Foundry uses the existing OpenAI/Anthropic wire formats; no second response adapter is needed."""
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401  (puts src/ on sys.path before the package imports)
from mockingbird.api import server
from mockingbird.core import agent, state


class FoundryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.settings = patch.multiple(state, DIR=root, CACHE=root / "cache", AUTO="fake-foundry",
                                       REAL=False, REPLAY=False, REDACT=False, NAMESPACE="", FAULTS=[],
                                       QUIET=True, JOURNAL=None)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        self.server = server.serve(0)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def post(self, path, body):
        request = urllib.request.Request(self.url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.headers.get("Content-Type"), response.read().decode()

    def test_foundry_chat_prefixes_use_existing_openai_response(self):
        body = {"model": "deployed-model", "messages": [{"role": "user", "content": "hello"}]}
        paths = ("/models/chat/completions?api-version=2024-05-01-preview", "/openai/v1/chat/completions",
                 "/openai/deployments/deployed-model/chat/completions?api-version=2024-06-01")
        with patch.object(agent, "run_auto", return_value={"text": "hello"}):
            for path in paths:
                with self.subTest(path=path):
                    content_type, data = self.post(path, body)
                    reply = json.loads(data)
                    self.assertEqual(content_type, "application/json")
                    self.assertEqual(reply["choices"][0]["message"]["content"], "hello")
                    self.assertEqual(reply["model"], "deployed-model")
                    self.assertEqual(reply["usage"]["total_tokens"], 0)

    def test_foundry_chat_streamed_tools_use_openai_events(self):
        body = {"model": "deployed-model", "stream": True, "messages": [], "tools": [{"type": "function", "function": {
            "name": "lookup", "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}]}
        with patch.object(agent, "run_auto", return_value={"tool_calls": [{"name": "lookup", "input": {"city": "Paris"}}]}):
            content_type, data = self.post("/models/chat/completions?api-version=2024-05-01-preview", body)
        self.assertEqual(content_type, "text/event-stream")
        chunks = [json.loads(line[6:]) for line in data.splitlines() if line.startswith("data: ") and line != "data: [DONE]"]
        call = chunks[0]["choices"][0]["delta"]["tool_calls"][0]
        self.assertEqual(call["function"]["name"], "lookup")
        self.assertEqual(json.loads(call["function"]["arguments"]), {"city": "Paris"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    def test_foundry_anthropic_prefix_preserves_messages_contract(self):
        body = {"model": "claude-deployment", "max_tokens": 64, "messages": [{"role": "user", "content": "hello"}]}
        with patch.object(agent, "run_auto", return_value={"text": "hello"}) as answer:
            _, data = self.post("/anthropic/v1/messages", body)
        reply = json.loads(data)
        self.assertEqual(reply["type"], "message")
        self.assertEqual(reply["content"], [{"type": "text", "text": "hello"}])
        self.assertEqual(answer.call_args.args[0], "anthropic")
        self.assertEqual(answer.call_args.args[1], body)

    def test_foundry_unsupported_operations_return_501(self):
        for path in ("/models/embeddings", "/openai/v1/responses", "/api/projects/demo/agents"):
            with self.subTest(path=path), self.assertRaises(urllib.error.HTTPError) as error:
                self.post(path, {})
            self.assertEqual(error.exception.code, 501)
            error.exception.close()

    def test_foundry_real_upstream_hosts_are_allowed_without_accepting_spoofs(self):
        for host in ("example.services.ai.azure.com", "deployment.eastus2.models.ai.azure.com", "models.inference.ai.azure.com"):
            with self.subTest(host=host):
                endpoint = "https://" + host
                self.assertEqual(server.upstream_for("openai", {"x-mockingbird-upstream": endpoint}), endpoint)
        self.assertIsNone(server.upstream_for("openai", {"x-mockingbird-upstream": "https://example.services.ai.azure.com.evil.example"}))


if __name__ == "__main__":
    unittest.main()
