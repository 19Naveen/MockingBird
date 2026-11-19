"""Offline Responses API coverage for the automation's attachment requests."""
import base64
import json
import shlex
import sys
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

import _bootstrap  # noqa: F401
from media_fixtures import red_png, text_pdf
from mockingbird.api import server
from mockingbird.core import agent, state
from mockingbird.media import attachments


class ResponsesTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.settings = patch.multiple(state, DIR=self.root, CACHE=self.root / "cache", AUTO="fake",
                                       REAL=False, REPLAY=False, REDACT=False, NAMESPACE="", FAULTS=[],
                                       QUIET=True, JOURNAL=None)
        self.settings.start()
        self.addCleanup(self.settings.stop)
        srv = server.serve(0)
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{srv.server_port}/openai/v1/responses"
        self.body = {
            "model": "test-deployment", "instructions": "Return JSON",
            "input": [{"role": "user", "content": [
                {"type": "input_text", "text": "Attachment: note.txt"},
                {"type": "input_file", "filename": "note.txt",
                 "file_data": base64.b64encode(b"synthetic document").decode()},
                {"type": "input_text", "text": "Extract the value"},
            ]}],
            "text": {"format": {"type": "json_schema", "name": "test", "strict": True,
                                "schema": {"type": "object", "properties": {"value": {"type": "string"}}}}},
        }

    def post(self, body=None):
        req = urllib.request.Request(self.url, json.dumps(body or self.body).encode(),
                                     {"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            return json.load(resp)

    def test_inline_file_response_and_replay(self):
        def answer(api, req, rid):
            self.assertEqual(api, "openai_responses")
            self.assertEqual(req["response_format"]["type"], "json_schema")
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                prepared, images = attachments.prepare(req, Path(tmp))
            self.assertFalse(images)
            self.assertIn("synthetic document", json.dumps(prepared))
            return {"text": '{"value":"hello"}', "usage": {"prompt_tokens": 12, "completion_tokens": 4}}

        with patch.object(agent, "run_auto", side_effect=answer) as auto:
            first, replay = self.post(), self.post()
        self.assertEqual(auto.call_count, 1)
        self.assertEqual(first["status"], "completed")
        self.assertEqual(first["output"][0]["content"][0]["text"], '{"value":"hello"}')
        self.assertEqual(first["usage"]["input_tokens"], 12)
        self.assertEqual(replay["output"][0]["content"][0]["text"], first["output"][0]["content"][0]["text"])

    def test_inline_file_end_to_end_with_local_fake_cli(self):
        code = ("import sys,json; prompt=sys.stdin.read(); "
                "assert 'synthetic document' in prompt and 'json_schema' in prompt; "
                "print(json.dumps({'text': '{\"value\":\"ok\"}'}))")
        state.AUTO = f"{shlex.quote(sys.executable)} -c {shlex.quote(code)}"
        reply = self.post()
        self.assertEqual(json.loads(reply["output"][0]["content"][0]["text"]), {"value": "ok"})

    def test_image_reaches_attachment_pipeline(self):
        image = "data:image/png;base64," + base64.b64encode(red_png()).decode()
        body = {**self.body, "input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": image},
            {"type": "input_text", "text": "synthetic"},
        ]}]}
        def answer(api, req, rid):
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                prepared, images = attachments.prepare(req, Path(tmp))
                self.assertEqual(len(images), 1)
                self.assertIn("[attached image 1]", json.dumps(prepared))
            return {"text": "{}"}
        with patch.object(agent, "run_auto", side_effect=answer):
            self.assertEqual(self.post(body)["status"], "completed")

    def test_pdf_reaches_attachment_pipeline(self):
        import shutil
        if not shutil.which("pdfinfo") or not shutil.which("pdftoppm"):
            self.skipTest("Poppler is not installed")
        pdf = "data:application/pdf;base64," + base64.b64encode(text_pdf()).decode()
        body = {**self.body, "input": [{"role": "user", "content": [
            {"type": "input_file", "filename": "document.pdf", "file_data": pdf},
            {"type": "input_text", "text": "synthetic"},
        ]}]}
        def answer(api, req, rid):
            with tempfile.TemporaryDirectory(dir=self.root) as tmp:
                prepared, images = attachments.prepare(req, Path(tmp))
                self.assertEqual(len(images), 1)
                self.assertIn("[PDF pages:", json.dumps(prepared))
            return {"text": "{}"}
        with patch.object(agent, "run_auto", side_effect=answer):
            self.assertEqual(self.post(body)["status"], "completed")

    def test_remote_attachments_and_stream_are_rejected(self):
        for body in (
            {**self.body, "stream": True},
            {**self.body, "previous_response_id": "resp_old"},
        ):
            with self.subTest(body=body), self.assertRaises(urllib.error.HTTPError) as error:
                self.post(body)
            self.assertEqual(error.exception.code, 501)
            error.exception.close()
        remote = {**self.body, "input": [{"role": "user", "content": [
            {"type": "input_image", "image_url": "https://example.invalid/image.png"}]}]}
        with patch.object(agent.subprocess, "run", side_effect=AssertionError("must not run a CLI")), \
                self.assertRaises(urllib.error.HTTPError) as error:
            self.post(remote)
        self.assertEqual(error.exception.code, 501)
        error.exception.close()

    def test_real_responses_are_forwarded_unchanged(self):
        state.REAL = True
        response = {"object": "response", "status": "completed", "output": [],
                    "usage": {"input_tokens": 123, "output_tokens": 7}}
        with patch.object(server, "forward", return_value=(200, "application/json", json.dumps(response).encode())) as f:
            self.assertEqual(self.post(), response)
            self.assertEqual(json.loads(f.call_args.args[2]), self.body)
            self.assertEqual(f.call_args.args[1], "/openai/v1/responses")
            state.REPLAY = True
            self.assertEqual(self.post(), response)
            f.assert_called_once()


if __name__ == "__main__":
    unittest.main()
