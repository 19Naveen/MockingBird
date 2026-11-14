"""Opt-in SDK check: python tests/sdk_smoke.py --model codex|sonnet|opencode.

Requires openai and anthropic in the test environment only. Runs twice with
an isolated cache; the second pass is offline and must not invoke any CLI.
"""
import json
import argparse
import asyncio
import base64
import concurrent.futures
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mockingbird import agent, dashboard, server, state
from anthropic import Anthropic, AsyncAnthropic
from openai import OpenAI, AsyncOpenAI, AzureOpenAI
from media_fixtures import red_png, text_pdf


class SDKFlow(unittest.TestCase):
    anthropic = None
    openai = None
    url = ""

    def test_anthropic_image_input(self):
        reply = self.anthropic.messages.create(model="claude-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "What is the dominant color of this image? Reply with exactly its lowercase English color name."},
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": base64.b64encode(red_png()).decode()}}]}])
        self.assertEqual(reply.content[0].text.strip(), "red")

    def test_openai_image_input(self):
        reply = self.openai.chat.completions.create(model="gpt-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "What is the dominant color of this image? Reply with exactly its lowercase English color name."},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64," + base64.b64encode(red_png()).decode()}}]}])
        self.assertEqual(reply.choices[0].message.content.strip(), "red")

    def test_anthropic_text_document(self):
        reply = self.anthropic.messages.create(model="claude-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Reply with exactly the passphrase in this document."},
            {"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "The passphrase is strawberry."}}]}])
        self.assertEqual(reply.content[0].text.strip(), "strawberry")

    def test_openai_text_file(self):
        reply = self.openai.chat.completions.create(model="gpt-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "Reply with exactly the passphrase in this document."},
            {"type": "file", "file": {"filename": "facts.txt", "file_data": "data:text/plain;base64," +
                base64.b64encode(b"The passphrase is strawberry.").decode()}}]}])
        self.assertEqual(reply.choices[0].message.content.strip(), "strawberry")

    def test_anthropic_pdf_input(self):
        reply = self.anthropic.messages.create(model="claude-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "What single word appears on this PDF page? Reply with exactly that word."},
            {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(text_pdf()).decode()}}]}])
        self.assertEqual(reply.content[0].text.strip(), "PINEAPPLE")

    def test_openai_pdf_input(self):
        reply = self.openai.chat.completions.create(model="gpt-test", max_tokens=64, messages=[{"role": "user", "content": [
            {"type": "text", "text": "What single word appears on this PDF page? Reply with exactly that word."},
            {"type": "file", "file": {"filename": "page.pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(text_pdf()).decode()}}]}])
        self.assertEqual(reply.choices[0].message.content.strip(), "PINEAPPLE")

    def test_async_clients_and_streams(self):
        async def check():
            async with AsyncAnthropic(api_key="bridge-dummy", base_url=self.url, max_retries=0) as ant:
                reply = await ant.messages.create(model="claude-test", max_tokens=64,
                    messages=[{"role": "user", "content": "Reply with exactly hello."}])
                self.assertEqual(reply.content[0].text.strip(), "hello")
                async with ant.messages.stream(model="claude-test", max_tokens=64,
                        messages=[{"role": "user", "content": "Reply with exactly hello."}]) as stream:
                    self.assertEqual("".join([text async for text in stream.text_stream]).strip(), "hello")
            async with AsyncOpenAI(api_key="bridge-dummy", base_url=self.url + "/v1", max_retries=0) as oai:
                args = dict(model="gpt-test", max_tokens=64, response_format={"type": "json_object"},
                            messages=[{"role": "user", "content": 'Return JSON: {"ok": true}'}])
                reply = await oai.chat.completions.create(**args)
                self.assertEqual(json.loads(reply.choices[0].message.content), {"ok": True})
                stream = await oai.chat.completions.create(**args, stream=True)
                self.assertEqual(json.loads("".join([c.choices[0].delta.content or "" async for c in stream])), {"ok": True})
        asyncio.run(check())

    def test_azure_deployment_route(self):
        with AzureOpenAI(api_key="bridge-dummy", azure_endpoint=self.url, api_version="2024-10-21", max_retries=0) as azure:
            reply = azure.chat.completions.create(model="test-deployment", max_tokens=64,
                messages=[{"role": "user", "content": "Reply with exactly hello."}])
            self.assertEqual(reply.choices[0].message.content.strip(), "hello")

    def test_concurrent_identical_requests(self):
        def request(_):
            reply = self.openai.chat.completions.create(model="gpt-test", max_tokens=64,
                messages=[{"role": "user", "content": "Reply with exactly concurrent."}])
            return reply.choices[0].message.content.strip()
        with concurrent.futures.ThreadPoolExecutor(8) as pool:
            self.assertEqual(list(pool.map(request, range(8))), ["concurrent"] * 8)

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

    def test_anthropic_streamed_tool(self):
        with self.anthropic.messages.stream(model="claude-test", max_tokens=128,
                messages=[{"role": "user", "content": "Use lookup for Paris. Then reply with exactly its returned value."}],
                tools=[{"name": "lookup", "description": "Get a city's weather", "input_schema": {
                    "type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}],
                tool_choice={"type": "tool", "name": "lookup"}) as stream:
            message = stream.get_final_message()
        call = next(b for b in message.content if b.type == "tool_use")
        self.assertEqual(call.name, "lookup")
        self.assertEqual(call.input, {"city": "Paris"})

    def test_openai_streamed_tool(self):
        stream = self.openai.chat.completions.create(model="gpt-test", max_tokens=128, stream=True,
            messages=[{"role": "user", "content": "Use lookup for Paris. Then reply with exactly its returned value."}],
            tools=[{"type": "function", "function": {"name": "lookup", "description": "Get a city's weather",
                "parameters": {"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}}}],
            tool_choice={"type": "function", "function": {"name": "lookup"}})
        calls = [t for chunk in stream for t in (chunk.choices[0].delta.tool_calls or [])]
        self.assertEqual(calls[0].function.name, "lookup")
        self.assertEqual(json.loads("".join(t.function.arguments or "" for t in calls)), {"city": "Paris"})

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
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", default="codex")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mockingbird-sdk-") as tmp:
        state.DIR, state.CACHE = Path(tmp) / "queue", Path(tmp) / "cache"
        state.AUTO = agent.auto_cmd(args.model)
        state.REAL, state.REDACT, state.REPLAY, state.FAULTS, state.NAMESPACE = False, False, False, [], ""
        state.JOURNAL = dashboard.Journal(Path(tmp) / "calls.db")
        srv = server.serve(0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{srv.server_port}"
        try:
            with Anthropic(api_key="bridge-dummy", base_url=url, max_retries=0) as ant, OpenAI(
                    api_key="bridge-dummy", base_url=url + "/v1", max_retries=0) as oai:
                SDKFlow.anthropic, SDKFlow.openai, SDKFlow.url = ant, oai, url
                for phase in ("fresh", "replay"):
                    state.REPLAY = phase == "replay"
                    start = time.monotonic()
                    with patch.object(agent, "run_auto", wraps=agent.run_auto) as auto:
                        result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(SDKFlow))
                    print(f"{args.model} {phase}: {time.monotonic() - start:.2f}s, {auto.call_count} CLI generations", flush=True)
                    if not result.wasSuccessful() or (phase == "replay" and auto.call_count):
                        return 1
                print(f"SDK versions: anthropic {__import__('anthropic').__version__}, openai {__import__('openai').__version__}")
                return 0
        finally:
            srv.shutdown()
            srv.server_close()


if __name__ == "__main__":
    sys.exit(main())
