"""Opt-in SDK check: python tests/codex_smoke.py (uses your Codex login).

Requires openai and anthropic in the test environment only. Runs twice with
an isolated cache; the second pass must not invoke Codex.
"""
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import bridge
from anthropic import Anthropic
from openai import OpenAI


class SDKFlow(unittest.TestCase):
    anthropic = None
    openai = None

    def test_anthropic_text(self):
        reply = self.anthropic.messages.create(model="claude-test", max_tokens=64,
                messages=[{"role": "user", "content": "Reply with exactly hello."}])
        self.assertEqual(reply.content[0].text.strip(), "hello")
        self.assertEqual(reply.stop_reason, "end_turn")
        self.assertEqual(reply.usage.input_tokens, 0)

    def test_anthropic_stream(self):
        with self.anthropic.messages.stream(model="claude-test", max_tokens=64,
                messages=[{"role": "user", "content": "Reply with exactly hello."}]) as stream:
            self.assertEqual("".join(stream.text_stream).strip(), "hello")
            self.assertEqual(stream.get_final_message().stop_reason, "end_turn")

    def test_openai_json(self):
        reply = self.openai.chat.completions.create(model="gpt-test", max_tokens=64,
                response_format={"type": "json_object"}, messages=[{"role": "user", "content": 'Return JSON: {"ok": true}'}])
        self.assertEqual(json.loads(reply.choices[0].message.content), {"ok": True})
        self.assertEqual(reply.choices[0].finish_reason, "stop")

    def test_openai_stream(self):
        stream = self.openai.chat.completions.create(model="gpt-test", max_tokens=64, stream=True,
                response_format={"type": "json_object"}, messages=[{"role": "user", "content": 'Return JSON: {"ok": true}'}])
        chunks = list(stream)
        self.assertEqual(json.loads("".join(c.choices[0].delta.content or "" for c in chunks)), {"ok": True})
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_anthropic_tool_loop(self):
        messages = [{"role": "user", "content": "Use lookup for Paris. Then reply with exactly its returned value."}]
        tools = [{"name": "lookup", "description": "Get a city's weather", "input_schema": {
            "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}]
        first = self.anthropic.messages.create(model="claude-test", max_tokens=128, messages=messages,
                    tools=tools, tool_choice={"type": "tool", "name": "lookup"})
        call = next(b for b in first.content if b.type == "tool_use")
        self.assertEqual(call.input, {"city": "Paris"})
        messages += [{"role": "assistant", "content": [b.model_dump(exclude_none=True) for b in first.content]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call.id, "content": "sunny"}]}]
        final = self.anthropic.messages.create(model="claude-test", max_tokens=64, tools=tools, messages=messages)
        self.assertEqual(final.content[0].text.strip(), "sunny")

    def test_openai_tool_loop(self):
        messages = [{"role": "user", "content": "Use lookup for Paris. Then reply with exactly its returned value."}]
        tools = [{"type": "function", "function": {"name": "lookup", "description": "Get a city's weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}]
        first = self.openai.chat.completions.create(model="gpt-test", max_tokens=128, messages=messages,
                    tools=tools, tool_choice={"type": "function", "function": {"name": "lookup"}})
        call = first.choices[0].message.tool_calls[0]
        self.assertEqual(json.loads(call.function.arguments), {"city": "Paris"})
        messages += [first.choices[0].message.model_dump(exclude_none=True),
                     {"role": "tool", "tool_call_id": call.id, "content": "sunny"}]
        final = self.openai.chat.completions.create(model="gpt-test", max_tokens=64, tools=tools, messages=messages)
        self.assertEqual(final.choices[0].message.content.strip(), "sunny")


def main():
    with tempfile.TemporaryDirectory(prefix="mockingbird-codex-") as tmp:
        bridge.DIR, bridge.CACHE = Path(tmp) / "queue", Path(tmp) / "cache"
        bridge.AUTO, bridge.REAL, bridge.REDACT = bridge.auto_cmd("codex"), False, False
        bridge.JOURNAL = bridge.dashboard.Journal(Path(tmp) / "calls.db")
        srv = bridge.serve(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}"
        try:
            with Anthropic(api_key="bridge-dummy", base_url=url) as ant, OpenAI(
                    api_key="bridge-dummy", base_url=url + "/v1") as oai:
                SDKFlow.anthropic, SDKFlow.openai = ant, oai
                for phase in ("fresh", "replay"):
                    start = time.monotonic()
                    with patch.object(bridge, "run_auto", wraps=bridge.run_auto) as auto:
                        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SDKFlow))
                    print(f"{phase}: {time.monotonic() - start:.2f}s, {auto.call_count} Codex calls", flush=True)
                    if not result.wasSuccessful() or (phase == "replay" and auto.call_count):
                        return 1
                print(f"SDK versions: anthropic {__import__('anthropic').__version__}, openai {__import__('openai').__version__}")
                return 0
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
