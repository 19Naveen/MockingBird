"""Native Google SDK parsing check, all requests local. Optional --model tests a stand-in CLI too."""
import argparse
import asyncio
import json
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mockingbird import agent, attachments, dashboard, server, state
from google import genai
from google.genai import errors, types
from google.oauth2.credentials import Credentials
from media_fixtures import red_png, text_pdf


class GoogleSDKTests(unittest.TestCase):
    client = None

    def test_text_stream_and_usage(self):
        response = self.client.models.generate_content(model="gemini-test", contents="Reply with exactly hello.")
        self.assertEqual(response.text.strip(), "hello")
        self.assertEqual(response.usage_metadata.total_token_count, 0)
        chunks = self.client.models.generate_content_stream(model="gemini-test", contents="Reply with exactly hello.")
        self.assertEqual("".join(chunk.text or "" for chunk in chunks).strip(), "hello")

    def test_json_output(self):
        response = self.client.models.generate_content(model="gemini-test", contents='Return JSON: {"ok": true}',
            config=types.GenerateContentConfig(response_mime_type="application/json", response_json_schema={"type": "object"}))
        self.assertEqual(json.loads(response.text), {"ok": True})

    def test_function_call_and_result(self):
        tools = [types.Tool(function_declarations=[types.FunctionDeclaration(name="weather", description="Get weather for a city",
            parameters_json_schema={"type": "object", "properties": {"city": {"type": "string"}}, "required": ["city"]})])]
        user = types.Content(role="user", parts=[types.Part.from_text(text="Use weather for Paris. Then reply with exactly its returned value.")])
        first = self.client.models.generate_content(model="gemini-test", contents=[user],
            config=types.GenerateContentConfig(tools=tools, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
                tool_config=types.ToolConfig(function_calling_config=types.FunctionCallingConfig(mode="ANY", allowed_function_names=["weather"]))))
        call = first.function_calls[0]
        self.assertEqual(call.name, "weather")
        self.assertEqual(call.args, {"city": "Paris"})
        followup = types.Content(role="user", parts=[types.Part.from_function_response(name="weather", response={"weather": "sunny"})])
        final = self.client.models.generate_content(model="gemini-test", contents=[user, first.candidates[0].content, followup],
            config=types.GenerateContentConfig(tools=tools, automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True)))
        self.assertEqual(final.text.strip(), "sunny")

    def test_inline_image(self):
        response = self.client.models.generate_content(model="gemini-test", contents=[
            types.Part.from_text(text="What is the dominant color of this image? Reply with exactly its lowercase English color name."),
            types.Part.from_bytes(data=red_png(), mime_type="image/png")])
        self.assertEqual(response.text.strip(), "red")

    def test_inline_text_document(self):
        response = self.client.models.generate_content(model="gemini-test", contents=[
            types.Part.from_text(text="Reply with exactly the passphrase in this document."),
            types.Part.from_bytes(data=b"The passphrase is strawberry.", mime_type="text/plain")])
        self.assertEqual(response.text.strip(), "strawberry")

    def test_inline_pdf(self):
        response = self.client.models.generate_content(model="gemini-test", contents=[
            types.Part.from_text(text="What single word appears on this PDF page? Reply with exactly that word."),
            types.Part.from_bytes(data=text_pdf(), mime_type="application/pdf")])
        self.assertEqual(response.text.strip(), "PINEAPPLE")

    def test_async_text_and_stream(self):
        async def check():
            response = await self.client.aio.models.generate_content(model="gemini-test", contents="Reply with exactly hello.")
            self.assertEqual(response.text.strip(), "hello")
            stream = await self.client.aio.models.generate_content_stream(model="gemini-test", contents="Reply with exactly hello.")
            self.assertEqual("".join([chunk.text or "" async for chunk in stream]).strip(), "hello")
        asyncio.run(check())

    def test_native_rate_limit_error(self):
        state.FAULTS = [{"status": 429, "error": "scripted Google rate limit"}]
        with self.assertRaises(errors.ClientError) as error:
            self.client.models.generate_content(model="gemini-test", contents="Reply with exactly hello.")
        self.assertEqual(error.exception.code, 429)
        self.assertIn("scripted Google rate limit", str(error.exception))


def fixed_answer(api, request, rid):
    # This mode verifies SDK transport/schema parsing without a model, including attachment conversion.
    with tempfile.TemporaryDirectory() as folder:
        clean, images = attachments.prepare(request, Path(folder))
    text = json.dumps(clean)
    if '"tool_result"' in text:
        return {"text": "sunny"}
    if request.get("tool_choice", {}).get("type") == "tool":
        return {"tool_calls": [{"name": "weather", "input": {"city": "Paris"}}]}
    if request.get("response_format"):
        return {"text": '{"ok":true}'}
    if "PDF page" in text:
        if not images:
            raise AssertionError("PDF must be rendered")
        return {"text": "PINEAPPLE"}
    if images:
        return {"text": "red"}
    if "strawberry" in text:
        return {"text": "strawberry"}
    return {"text": "hello"}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", help="optional stand-in CLI; omitted uses deterministic local answers")
    options = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="mockingbird-google-sdk-") as folder:
        state.DIR, state.CACHE = Path(folder) / "queue", Path(folder) / "cache"
        state.AUTO = agent.auto_cmd(options.model) if options.model else "google-sdk-shape-check"
        state.REAL, state.REDACT, state.REPLAY, state.FAULTS, state.NAMESPACE = False, False, False, [], ""
        state.JOURNAL, state.QUIET = None, True
        server_obj = server.serve(0)
        threading.Thread(target=server_obj.serve_forever, daemon=True).start()
        url = f"http://127.0.0.1:{server_obj.server_port}"
        try:
            for mode in ("developer", "vertex"):
                kwargs = {"api_key": "bridge-dummy", "vertexai": False} if mode == "developer" else {
                    "vertexai": True, "project": "test-project", "location": "us-central1", "credentials": Credentials(token="bridge-dummy")}
                with genai.Client(**kwargs, http_options=types.HttpOptions(base_url=url, api_version="v1beta" if mode == "developer" else "v1",
                        retry_options=types.HttpRetryOptions(attempts=1))) as client:
                    GoogleSDKTests.client = client
                    for phase in ("fresh", "replay"):
                        state.REPLAY = phase == "replay"
                        start = time.monotonic()
                        with patch.object(agent, "run_auto", side_effect=agent.run_auto if options.model else fixed_answer) as auto:
                            result = unittest.TextTestRunner(verbosity=2).run(unittest.defaultTestLoader.loadTestsFromTestCase(GoogleSDKTests))
                        print(f"google {mode} {phase}: {time.monotonic() - start:.2f}s, {auto.call_count} generations", flush=True)
                        if not result.wasSuccessful() or (phase == "replay" and auto.call_count):
                            return 1
            print(f"google-genai {genai.__version__}")
            return 0
        finally:
            server_obj.shutdown()
            server_obj.server_close()


if __name__ == "__main__":
    sys.exit(main())
