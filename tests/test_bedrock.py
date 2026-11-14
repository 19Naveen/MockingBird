import base64
import importlib.util
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import struct
import tempfile
import threading
import unittest
import zlib

from mockingbird import attachments
from mockingbird.providers import bedrock
from media_fixtures import red_png


class BedrockTests(unittest.TestCase):
    path = "/model/us.anthropic.test%3A0/converse"

    def request(self, content=None, **kwargs):
        return {"messages": [{"role": "user", "content": content or [{"text": "hello"}]}], **kwargs}

    def test_supported_routes_include_encoded_model_ids(self):
        self.assertTrue(bedrock.matches(self.path))
        self.assertTrue(bedrock.matches(self.path + "-stream"))
        self.assertFalse(bedrock.matches("/model/test/invoke"))
        self.assertFalse(bedrock.matches("/v1/messages"))
        self.assertEqual(bedrock.normalize(self.path, self.request())["model"], "us.anthropic.test:0")

    def test_generation_options_and_system_are_normalized(self):
        req = self.request(system=[{"text": "Be concise"}], inferenceConfig={"maxTokens": 20, "temperature": 0.1,
                          "topP": 0.9, "stopSequences": ["END"]}, additionalModelRequestFields={"top_k": 5})
        normalized = bedrock.normalize(self.path, req)
        self.assertEqual(normalized["system"], [{"type": "text", "text": "Be concise"}])
        self.assertEqual(normalized["max_tokens"], 20)
        self.assertEqual(normalized["stop_sequences"], ["END"])
        self.assertEqual(normalized["bedrock_options"]["additionalModelRequestFields"], {"top_k": 5})
        self.assertFalse(normalized["stream"])
        self.assertTrue(bedrock.normalize(self.path + "-stream", req)["stream"])

    def test_tools_calls_and_results_keep_provider_data(self):
        req = self.request(toolConfig={"tools": [{"toolSpec": {"name": "lookup", "description": "weather",
                            "inputSchema": {"json": {"type": "object"}}}}], "toolChoice": {"tool": {"name": "lookup"}}})
        req["messages"] += [{"role": "assistant", "content": [{"toolUse": {"toolUseId": "id-1", "name": "lookup", "input": {"city": "Paris"}}}]},
                            {"role": "user", "content": [{"toolResult": {"toolUseId": "id-1", "status": "success", "content": [{"json": {"weather": "sunny"}}]}}]}]
        normalized = bedrock.normalize(self.path, req)
        self.assertEqual(normalized["tools"][0]["input_schema"], {"type": "object"})
        self.assertEqual(normalized["tool_choice"], {"type": "tool", "name": "lookup"})
        result = normalized["messages"][-1]["content"][0]
        self.assertEqual(result["tool_use_id"], "id-1")
        self.assertIn('"weather": "sunny"', result["content"][0]["text"])
        self.assertFalse(result["is_error"])

    def test_inline_images_and_documents_reach_shared_attachment_pipeline(self):
        data = base64.b64encode(red_png()).decode()
        req = self.request([{"image": {"format": "png", "source": {"bytes": data}}},
                            {"document": {"format": "txt", "name": "facts", "source": {"bytes": "aGVsbG8="}}}])
        normalized = bedrock.normalize(self.path, req)
        with tempfile.TemporaryDirectory() as tmp:
            clean, images = attachments.prepare(normalized, Path(tmp))
            self.assertEqual(images[0].read_bytes(), red_png())
            self.assertIn("hello", json.dumps(clean))
            self.assertNotIn(data, json.dumps(clean))

    def test_all_original_controls_affect_request_hash(self):
        first = bedrock.normalize(self.path, self.request(inferenceConfig={"custom": 1}))
        second = bedrock.normalize(self.path, self.request(inferenceConfig={"custom": 2}))
        self.assertNotEqual(first["bedrock_request_hash"], second["bedrock_request_hash"])

    def test_unsupported_refs_and_content_are_explicit_errors(self):
        bad = [self.request([{"image": {"format": "png", "source": {"s3Location": {"uri": "s3://private/image"}}}}]),
               self.request([{"video": {"format": "mp4"}}]),
               self.request([{"document": {"format": "docx", "source": {"bytes": "aA=="}}}]),
               self.request([{"text": "x", "image": {}}]),
               self.request([{"toolUse": {"name": "lookup", "input": []}}])]
        for req in bad:
            with self.subTest(req=req), self.assertRaises(attachments.InputError):
                bedrock.normalize(self.path, req)

    def test_response_has_native_usage_and_stable_tool_ids(self):
        req = bedrock.normalize(self.path, self.request())
        ans = {"tool_calls": [{"name": "lookup", "input": {"city": "Paris"}}]}
        first = bedrock.response(ans, req, "one")
        second = bedrock.response(ans, {**req, "stream": True}, "two")
        self.assertEqual(first, second)
        self.assertEqual(first["stopReason"], "tool_use")
        self.assertEqual(first["usage"], {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0})
        self.assertEqual(first["output"]["message"]["content"][0]["toolUse"]["name"], "lookup")

    def test_event_stream_frames_have_valid_lengths_and_checksums(self):
        req = bedrock.normalize(self.path, self.request())
        wire = bedrock.stream(bedrock.response({"text": "hello"}, req, "id"))
        frames = 0
        while wire:
            total, header_len, prelude_crc = struct.unpack(">III", wire[:12])
            self.assertGreater(total, header_len + 16)
            self.assertEqual(prelude_crc, zlib.crc32(wire[:8]))
            self.assertEqual(struct.unpack(">I", wire[total - 4:total])[0], zlib.crc32(wire[:total - 4]))
            json.loads(wire[12 + header_len:total - 4])
            wire = wire[total:]
            frames += 1
        self.assertEqual(frames, 5)

    def test_errors_are_provider_shaped(self):
        self.assertEqual(bedrock.error(429, "slow down"), {"__type": "ThrottlingException", "message": "slow down"})


@unittest.skipUnless(importlib.util.find_spec("boto3"), "boto3 is needed only for opt-in SDK checks")
class BedrockSDKTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import boto3
        from botocore.config import Config

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                raw = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                req = bedrock.normalize(self.path, raw)
                status = 200
                if req["messages"][0]["content"][0].get("text") == "fail":
                    status, body, ctype = 429, bedrock.error(429, "slow down"), "application/json"
                else:
                    if req.get("tool_choice", {}).get("type") == "tool":
                        ans = {"tool_calls": [{"name": "lookup", "input": {"city": "Paris"}}]}
                    elif req["messages"][-1]["content"][0].get("type") == "tool_result":
                        ans = {"text": "sunny"}
                    else:
                        ans = {"text": "hello"}
                    body = bedrock.response(ans, req, "test")
                    ctype = bedrock.CONTENT_TYPE if req["stream"] else "application/json"
                    if req["stream"]:
                        body = bedrock.stream(body)
                body = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(body)))
                if status == 429:
                    self.send_header("x-amzn-errortype", "ThrottlingException")
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *_):
                pass

        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.client = boto3.client("bedrock-runtime", endpoint_url=f"http://127.0.0.1:{cls.server.server_port}",
                                 region_name="us-east-1", aws_access_key_id="bridge-dummy", aws_secret_access_key="bridge-dummy",
                                 config=Config(retries={"max_attempts": 0}, read_timeout=5))

    @classmethod
    def tearDownClass(cls):
        cls.client.close()
        cls.server.shutdown()
        cls.server.server_close()

    def request(self, text="hi"):
        return {"modelId": "test-model", "messages": [{"role": "user", "content": [{"text": text}]}]}

    def test_boto3_converse_parses_text_and_usage(self):
        result = self.client.converse(**self.request())
        self.assertEqual(result["output"]["message"]["content"], [{"text": "hello"}])
        self.assertEqual(result["usage"]["totalTokens"], 0)

    def test_boto3_serializes_inline_image_and_document_bytes(self):
        request = self.request()
        request["messages"][0]["content"] += [{"image": {"format": "png", "source": {"bytes": red_png()}}},
            {"document": {"name": "facts", "format": "txt", "source": {"bytes": b"hello"}}}]
        result = self.client.converse(**request)
        self.assertEqual(result["output"]["message"]["content"], [{"text": "hello"}])

    def test_boto3_converse_stream_parses_native_binary_frames(self):
        result = self.client.converse_stream(**self.request())
        events = list(result["stream"])
        self.assertEqual(events[0], {"messageStart": {"role": "assistant"}})
        self.assertEqual(events[1]["contentBlockDelta"]["delta"]["text"], "hello")
        self.assertEqual(events[-2]["messageStop"]["stopReason"], "end_turn")
        self.assertEqual(events[-1]["metadata"]["usage"]["totalTokens"], 0)

    def test_boto3_tool_calls_and_streamed_tool_arguments(self):
        request = self.request()
        request["toolConfig"] = {"tools": [{"toolSpec": {"name": "lookup", "inputSchema": {"json": {"type": "object"}}}}],
                                 "toolChoice": {"tool": {"name": "lookup"}}}
        first = self.client.converse(**request)
        call = first["output"]["message"]["content"][0]["toolUse"]
        self.assertEqual(call["input"], {"city": "Paris"})
        events = list(self.client.converse_stream(**request)["stream"])
        self.assertEqual(events[1]["contentBlockStart"]["start"]["toolUse"]["toolUseId"], call["toolUseId"])
        self.assertEqual(json.loads(events[2]["contentBlockDelta"]["delta"]["toolUse"]["input"]), {"city": "Paris"})
        request.pop("toolConfig")
        request["messages"] += [first["output"]["message"], {"role": "user", "content": [{"toolResult": {
            "toolUseId": call["toolUseId"], "content": [{"text": "sunny"}]}}]}]
        self.assertEqual(self.client.converse(**request)["output"]["message"]["content"], [{"text": "sunny"}])

    def test_boto3_throttle_errors_use_native_exception_type(self):
        with self.assertRaises(self.client.exceptions.ThrottlingException):
            self.client.converse(**self.request("fail"))


if __name__ == "__main__":
    unittest.main()
