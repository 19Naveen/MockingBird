import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest

import _bootstrap  # noqa: F401  (puts src/ on sys.path before the package imports)
from mockingbird.api.providers import google
from mockingbird.media import attachments
from media_fixtures import red_png


class GoogleTests(unittest.TestCase):
    developer = "/v1beta/models/gemini-test:generateContent"
    vertex = "/v1/projects/test/locations/us-central1/publishers/google/models/gemini-test:generateContent"

    def test_matches_developer_vertex_and_stream_routes(self):
        for path in (self.developer, self.vertex, self.developer.replace(":generate", ":streamGenerate"),
                     "/v1/publishers/google/models/gemini-test:generateContent?key=not-stored"):
            with self.subTest(path=path):
                self.assertTrue(google.matches(path))
        for path in ("/v1/models/gemini-test:embedContent", "/v1/models/gemini-test:generateImages",
                     "/v1/publishers/anthropic/models/claude:rawPredict", "/v1/files"):
            self.assertFalse(google.matches(path))

    def test_normalizes_system_generation_json_and_tools_without_mutating_input(self):
        req = {"contents": [{"role": "user", "parts": [{"text": "Which city?"}]}],
               "systemInstruction": {"parts": [{"text": "Return a city."}]},
               "generationConfig": {"maxOutputTokens": 50, "temperature": 0.1, "responseMimeType": "application/json",
                                    "responseJsonSchema": {"type": "object"}},
               "tools": [{"functionDeclarations": [{"name": "weather", "parameters": {"type": "OBJECT"}}]}],
               "toolConfig": {"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["weather"]}}}
        original = copy.deepcopy(req)
        out = google.normalize(self.developer, req)
        self.assertEqual(out["model"], "gemini-test")
        self.assertEqual(out["system"], [{"type": "text", "text": "Return a city."}])
        self.assertEqual(out["max_tokens"], 50)
        self.assertEqual(out["temperature"], 0.1)
        self.assertEqual(out["response_format"]["json_schema"]["schema"], {"type": "object"})
        self.assertEqual(out["tool_choice"], {"type": "tool", "name": "weather"})
        self.assertEqual(out["tools"][0]["name"], "weather")
        self.assertEqual(req, original)

    def test_normalizes_function_calls_results_and_reuses_ids(self):
        req = {"contents": [{"role": "model", "parts": [{"functionCall": {"name": "weather", "args": {"city": "Paris"}}}]},
                            {"role": "user", "parts": [{"functionResponse": {"name": "weather", "response": {"weather": "sunny"}}}]}]}
        out = google.normalize(self.vertex, req)
        call, result = out["messages"][0]["content"][0], out["messages"][1]["content"][0]
        self.assertEqual(call["id"], result["tool_use_id"])
        self.assertEqual(call["input"], {"city": "Paris"})
        self.assertIn("sunny", result["content"][0]["text"])
        self.assertEqual(call["id"], google.normalize(self.vertex, req)["messages"][0]["content"][0]["id"])

    def test_inline_images_and_text_documents_work_with_attachment_normalizer(self):
        req = {"contents": [{"parts": [
            {"inlineData": {"mimeType": "image/png", "data": base64.urlsafe_b64encode(red_png()).decode().rstrip("=")}},
            {"inlineData": {"mime_type": "text/plain", "data": base64.b64encode(b"private text").decode()}}]}]}
        with tempfile.TemporaryDirectory() as tmp:
            clean, images = attachments.prepare(google.normalize(self.developer, req), Path(tmp))
            self.assertEqual(images[0].read_bytes(), red_png())
            self.assertIn("private text", json.dumps(clean))
            self.assertNotIn(base64.b64encode(red_png()).decode(), json.dumps(clean))

    def test_full_request_hash_preserves_unmapped_option_identity_and_hides_query_key(self):
        req = {"contents": [{"parts": [{"text": "hi"}]}], "generationConfig": {"seed": 1}}
        out = google.normalize(self.developer + "?key=secret", req)
        self.assertNotIn("secret", json.dumps(out))
        req["generationConfig"]["seed"] = 2
        self.assertNotEqual(out["_google_request_hash"], google.normalize(self.developer, req)["_google_request_hash"])
        streaming = google.normalize(self.developer.replace(":generate", ":streamGenerate"), req)
        regular = google.normalize(self.developer, req)
        streaming.pop("stream")
        regular.pop("stream")
        self.assertEqual(streaming, regular)

    def test_rejects_invalid_or_unsupported_payloads(self):
        cases = [({"contents": {}}, 400), ({"contents": [{"role": "system", "parts": []}]}, 400),
                 ({"contents": [{"parts": [{"text": 9}]}]}, 400),
                 ({"contents": [{"parts": [{"fileData": {"fileUri": "gs://private/doc.pdf"}}]}]}, 501),
                 ({"tools": [{"googleSearch": {}}]}, 501), ({"generationConfig": {"candidateCount": 2}}, 501),
                 ({"toolConfig": {"functionCallingConfig": {"allowedFunctionNames": "weather"}}}, 400)]
        for payload, status in cases:
            with self.subTest(payload=payload), self.assertRaises(attachments.InputError) as error:
                google.normalize(self.developer, payload)
            self.assertEqual(error.exception.status, status)

    def test_accepts_vertex_snake_case_aliases(self):
        req = {"contents": [{"role": "user", "parts": [{"text": "Which city?"}]}],
               "system_instruction": {"parts": [{"text": "Return a city."}]},
               "generation_config": {"max_output_tokens": 50, "top_p": 0.9, "stop_sequences": ["END"],
                                     "response_mime_type": "application/json",
                                     "response_json_schema": {"type": "object"}},
               "tools": [{"function_declarations": [{"name": "weather", "parameters_json_schema": {
                   "type": "object", "properties": {"city": {"type": "string"}}}}]}],
               "tool_config": {"function_calling_config": {"mode": "ANY", "allowed_function_names": ["weather"]}}}
        out = google.normalize(self.vertex, req)
        self.assertEqual(out["system"], [{"type": "text", "text": "Return a city."}])
        self.assertEqual((out["max_tokens"], out["top_p"], out["stop_sequences"]), (50, 0.9, ["END"]))
        self.assertEqual(out["tools"][0]["input_schema"]["properties"], {"city": {"type": "string"}})
        self.assertEqual(out["tool_choice"], {"type": "tool", "name": "weather"})
        self.assertEqual(out["response_format"]["json_schema"]["schema"], {"type": "object"})
        self.assertNotIn("system_instruction", out["google_options"])
        self.assertNotIn("generation_config", out["google_options"])

    def test_response_text_tools_usage_stops_raw_and_sse(self):
        req = google.normalize(self.developer, {"contents": [{"parts": [{"text": "hi"}]}]})
        body = google.response({"text": "hi", "tool_calls": [{"name": "weather", "input": {"city": "Paris"}}]}, req, "1")
        candidate = body["candidates"][0]
        self.assertEqual(candidate["content"]["role"], "model")
        self.assertEqual(candidate["content"]["parts"][1]["functionCall"]["args"], {"city": "Paris"})
        call_id = candidate["content"]["parts"][1]["functionCall"]["id"]
        self.assertEqual(call_id, google.response({"tool_calls": [{"name": "weather", "input": {}}]}, req, "2")
                         ["candidates"][0]["content"]["parts"][0]["functionCall"]["id"])
        self.assertEqual(body["usageMetadata"]["totalTokenCount"], 0)
        self.assertEqual(google.response({"text": "x", "stop": "max_tokens"}, req, "1")["candidates"][0]["finishReason"], "MAX_TOKENS")
        self.assertEqual(google.response({"raw": {"custom": True}}, req, "1"), {"custom": True})
        self.assertEqual(json.loads(google.stream(body)[6:]), body)
        self.assertEqual(google.error(429, "limited")["error"]["status"], "RESOURCE_EXHAUSTED")


if __name__ == "__main__":
    unittest.main()
