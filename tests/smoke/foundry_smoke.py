"""SDK-only Foundry compatibility check; all calls hit a local fake stand-in, never Azure.

uv run --with azure-ai-inference --with aiohttp --with openai --with anthropic python tests/smoke/foundry_smoke.py
The retired azure-ai-inference SDK is covered for existing applications; OpenAI/v1 is preferred for new code.
"""
import asyncio
import base64
import importlib.util
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))  # tests/media_fixtures.py
from mockingbird.api import server
from mockingbird.core import agent, state, store

# A user-level hook may already shadow this checkout on PYTHONPATH; exercise the checkout's copy.
_spec = importlib.util.spec_from_file_location(
    "mockingbird_hook_repo", Path(__file__).resolve().parents[2] / "mockingbird_hook.py")
hook = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hook)
from anthropic import AnthropicFoundry, AsyncAnthropicFoundry
from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.aio import ChatCompletionsClient as AsyncChatCompletionsClient
from azure.ai.inference.models import ChatCompletionsToolDefinition, FunctionDefinition, ImageContentItem, ImageUrl, TextContentItem, UserMessage
from azure.core.credentials import AzureKeyCredential
from azure.core.exceptions import HttpResponseError
from openai import OpenAI
from media_fixtures import red_png


class FoundrySDKFlow(unittest.TestCase):
    url = ""
    state = ""

    def inference(self, endpoint=None):
        return ChatCompletionsClient(endpoint=endpoint or self.url + "/models", credential=AzureKeyCredential("bridge-dummy"),
                                     retry_total=0, connection_timeout=2, read_timeout=5)

    def test_azure_inference_text_and_json(self):
        with self.inference() as client:
            reply = client.complete(model="foundry-test", messages=[UserMessage("hello")])
            self.assertEqual(reply.choices[0].message.content, "hello")
            self.assertEqual(reply.usage.total_tokens, 0)
            reply = client.complete(model="foundry-test", messages=[UserMessage("JSON please")], response_format="json_object")
            self.assertEqual(json.loads(reply.choices[0].message.content), {"ok": True})

    def test_azure_inference_stream(self):
        with self.inference() as client:
            chunks = list(client.complete(model="foundry-test", messages=[UserMessage("hello")], stream=True))
        self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "hello")
        self.assertEqual(chunks[-1].choices[0].finish_reason, "stop")

    def test_azure_inference_tool_loop_and_streamed_tool(self):
        tools = [ChatCompletionsToolDefinition(function=FunctionDefinition(name="lookup", description="Look up weather",
                 parameters={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]}))]
        messages = [UserMessage("weather in Paris?")]
        with self.inference() as client:
            reply = client.complete(model="foundry-test", messages=messages, tools=tools, tool_choice="required")
            call = reply.choices[0].message.tool_calls[0]
            self.assertEqual(json.loads(call.function.arguments), {"city": "Paris"})
            chunks = list(client.complete(model="foundry-test", messages=messages, tools=tools, tool_choice="required", stream=True))
            calls = [tool for chunk in chunks for tool in (chunk.choices[0].delta.tool_calls or [])]
            self.assertEqual(calls[0].function.name, "lookup")
            messages += [{"role": "assistant", "tool_calls": [call.as_dict()]},
                         {"role": "tool", "tool_call_id": call.id, "content": "sunny"}]
            final = client.complete(model="foundry-test", messages=messages, tools=tools)
            self.assertEqual(final.choices[0].message.content, "sunny")

    def test_azure_inference_image_request_shape(self):
        data = "data:image/png;base64," + base64.b64encode(red_png()).decode()
        with self.inference() as client:
            reply = client.complete(model="foundry-image", messages=[UserMessage(content=[
                TextContentItem(text="image request"), ImageContentItem(image_url=ImageUrl(url=data))])])
        self.assertEqual(reply.choices[0].message.content, "hello")

    def test_azure_inference_faults_become_sdk_errors(self):
        with patch.object(state, "FAULTS", [{"status": 429, "error": "test rate limit"}]), self.inference() as client:
            with self.assertRaises(HttpResponseError) as error:
                client.complete(model="foundry-fault", messages=[UserMessage("hello")])
            self.assertEqual(error.exception.status_code, 429)
            self.assertIn("test rate limit", str(error.exception))

    def test_azure_inference_async(self):
        async def check():
            async with AsyncChatCompletionsClient(endpoint=self.url + "/models", credential=AzureKeyCredential("bridge-dummy"),
                                                  retry_total=0) as client:
                reply = await client.complete(model="foundry-async", messages=[UserMessage("hello")])
                self.assertEqual(reply.choices[0].message.content, "hello")
                chunks = [chunk async for chunk in await client.complete(model="foundry-async", messages=[UserMessage("hello")], stream=True)]
                self.assertEqual("".join(c.choices[0].delta.content or "" for c in chunks), "hello")
        asyncio.run(check())

    def test_openai_foundry_v1(self):
        with OpenAI(api_key="bridge-dummy", base_url=self.url + "/openai/v1", max_retries=0) as client:
            reply = client.chat.completions.create(model="foundry-v1", messages=[{"role": "user", "content": "hello"}])
            self.assertEqual(reply.choices[0].message.content, "hello")
            stream = client.chat.completions.create(model="foundry-v1", messages=[{"role": "user", "content": "hello"}], stream=True)
            self.assertEqual("".join(c.choices[0].delta.content or "" for c in stream), "hello")

    def test_anthropic_foundry_sdk(self):
        with AnthropicFoundry(api_key="bridge-dummy", base_url=self.url + "/anthropic", max_retries=0) as client:
            reply = client.messages.create(model="claude-foundry", max_tokens=64, messages=[{"role": "user", "content": "hello"}])
            self.assertEqual(reply.content[0].text, "hello")
            with client.messages.stream(model="claude-foundry", max_tokens=64, messages=[{"role": "user", "content": "hello"}]) as stream:
                self.assertEqual("".join(stream.text_stream), "hello")

        async def check():
            async with AsyncAnthropicFoundry(api_key="bridge-dummy", base_url=self.url + "/anthropic", max_retries=0) as client:
                reply = await client.messages.create(model="claude-foundry", max_tokens=64, messages=[{"role": "user", "content": "hello"}])
                self.assertEqual(reply.content[0].text, "hello")
        asyncio.run(check())

    def test_hardcoded_foundry_hosts_redirect_sync_and_async(self):
        with patch.dict(os.environ, MOCKINGBIRD_ENABLED="1", MOCKINGBIRD_STATE=self.state):
            hook.STATE = self.state
            for name in hook.MODULES:
                if name in sys.modules:
                    hook._patch(sys.modules[name])
            with self.inference("https://bridge-test.services.ai.azure.com/models") as client:
                reply = client.complete(model="foundry-hook", messages=[UserMessage("hello")])
                self.assertEqual(reply.choices[0].message.content, "hello")

            async def check():
                async with AsyncChatCompletionsClient(endpoint="https://bridge-test.services.ai.azure.com/models",
                        credential=AzureKeyCredential("bridge-dummy"), retry_total=0, connection_timeout=2, read_timeout=5) as client:
                    reply = await client.complete(model="foundry-hook-async", messages=[UserMessage("hello")])
                    self.assertEqual(reply.choices[0].message.content, "hello")
            asyncio.run(check())


def fake_answer(api, req, rid):
    if req.get("model") == "foundry-image":
        parts = req["messages"][0]["content"]
        assert parts[1]["type"] == "image_url"
        assert parts[1]["image_url"]["url"] == "data:image/png;base64," + base64.b64encode(red_png()).decode()
    if req.get("messages") and req["messages"][-1].get("role") == "tool":
        return {"text": "sunny"}
    if req.get("tools"):
        return {"tool_calls": [{"name": "lookup", "input": {"city": "Paris"}}]}
    if req.get("response_format"):
        return {"text": '{"ok":true}'}
    return {"text": "hello"}


def runner_client():
    """Used under bridge.py run: explicit URLs exercise hooks, Foundry defaults exercise runner env."""
    with ChatCompletionsClient(endpoint="https://bridge-test.services.ai.azure.com/models",
            credential=AzureKeyCredential("bridge-dummy"), retry_total=0, connection_timeout=2, read_timeout=5) as client:
        assert client.complete(model="foundry-runner", messages=[UserMessage("hello")]).choices[0].message.content == "hello"
    with AnthropicFoundry(max_retries=0) as client:
        assert client.messages.create(model="claude-foundry", max_tokens=64,
                                      messages=[{"role": "user", "content": "hello"}]).content[0].text == "hello"
    with OpenAI(api_key="bridge-dummy", base_url="https://bridge-test.services.ai.azure.com/openai/v1", max_retries=0) as client:
        assert client.chat.completions.create(model="foundry-runner", messages=[{"role": "user", "content": "hello"}]).choices[0].message.content == "hello"

    async def check():
        async with AsyncChatCompletionsClient(endpoint="https://bridge-test.services.ai.azure.com/models",
                credential=AzureKeyCredential("bridge-dummy"), retry_total=0, connection_timeout=2, read_timeout=5) as client:
            reply = await client.complete(model="foundry-runner", messages=[UserMessage("hello")])
            assert reply.choices[0].message.content == "hello"
        async with AsyncAnthropicFoundry(max_retries=0) as client:
            reply = await client.messages.create(model="claude-foundry", max_tokens=64, messages=[{"role": "user", "content": "hello"}])
            assert reply.content[0].text == "hello"
    asyncio.run(check())
    print("Foundry runner: 5 sync/async SDK calls passed through scoped routing")
    return 0


def main():
    if "--runner-client" in sys.argv:
        return runner_client()
    with tempfile.TemporaryDirectory(prefix="mockingbird-foundry-") as tmp:
        state.DIR, state.CACHE = Path(tmp) / "queue", Path(tmp) / "cache"
        state.AUTO, state.NAMESPACE = "fake-foundry-sdk", ""
        state.REAL, state.REPLAY, state.REDACT, state.FAULTS, state.QUIET, state.JOURNAL = False, False, False, [], True, None
        server_obj = server.serve(0)
        threading.Thread(target=server_obj.serve_forever, daemon=True).start()
        FoundrySDKFlow.url = f"http://127.0.0.1:{server_obj.server_port}"
        FoundrySDKFlow.state = str(Path(tmp) / "active.json")
        store.write(Path(FoundrySDKFlow.state), {"port": server_obj.server_port, "pid": os.getpid()})
        try:
            for phase in ("fresh", "replay"):
                state.REPLAY = phase == "replay"
                with patch.object(agent, "run_auto", side_effect=fake_answer) as answer:
                    result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(FoundrySDKFlow))
                print(f"foundry {phase}: {answer.call_count} fake stand-in calls")
                if not result.wasSuccessful() or (phase == "replay" and answer.call_count):
                    return 1
            import azure.ai.inference, openai, anthropic
            print(f"SDK versions: azure-ai-inference {azure.ai.inference.__version__}, openai {openai.__version__}, anthropic {anthropic.__version__}")
            fake = Path(tmp) / "fake_foundry_cli.py"
            fake.write_text('import json\nprint(json.dumps({"text": "hello"}))\n')
            env = {k: v for k, v in os.environ.items() if not k.startswith("BRIDGE_")}
            env["BRIDGE_CACHE"] = "off"
            result = subprocess.run(
                [sys.executable, str(Path(__file__).resolve().parents[2] / "bridge.py"), "run",
                 "--auto", shlex.join([sys.executable, str(fake)]), "--", sys.executable, __file__, "--runner-client"],
                cwd=Path(__file__).resolve().parents[2], env=env, capture_output=True, text=True, timeout=60)
            if result.returncode:
                sys.stderr.write(result.stderr)
                return result.returncode
            print(result.stdout.strip())
            return 0
        finally:
            server_obj.shutdown()
            server_obj.server_close()


if __name__ == "__main__":
    sys.exit(main())
