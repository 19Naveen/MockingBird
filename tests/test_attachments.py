import base64
import concurrent.futures
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import _bootstrap  # noqa: F401  (puts src/ on sys.path before the package imports)
from mockingbird.core import agent, answers, state, store
from mockingbird.media import attachments
from media_fixtures import red_png, text_pdf


def encoded(data):
    return base64.b64encode(data).decode()


class AttachmentTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.folder = Path(self.tmp.name)
        self.image = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": encoded(red_png())}}
        settings = patch.multiple(state, DIR=self.folder, AUTO="", REAL=False, REPLAY=False, REDACT=False,
                                  CACHE=self.folder / "cache", NAMESPACE="", QUIET=True, JOURNAL=None, FAULTS=[])
        settings.start()
        self.addCleanup(settings.stop)

    def request(self, *blocks):
        return {"model": "test", "messages": [{"role": "user", "content": list(blocks)}]}

    def test_images_and_text_are_normalized_without_mutating_request(self):
        req = self.request(self.image, {"type": "file", "file": {"filename": "facts.txt", "file_data": encoded(b"strawberry")}})
        clean, images = attachments.prepare(req, self.folder)
        self.assertEqual(images[0].read_bytes(), red_png())
        self.assertEqual(images[0].stat().st_mode & 0o777, 0o600)
        self.assertNotIn(encoded(red_png()), json.dumps(clean))
        self.assertIn("strawberry", json.dumps(clean))
        self.assertEqual(req["messages"][0]["content"][0], self.image)

    def test_openai_data_url_and_nested_tool_result(self):
        block = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + encoded(red_png())}}
        clean, images = attachments.prepare(self.request({"type": "tool_result", "content": [block]}), self.folder)
        self.assertEqual(len(images), 1)
        self.assertIn("attached image 1", json.dumps(clean))

    def test_text_document_source(self):
        clean, images = attachments.prepare(self.request({"type": "document", "source": {"type": "text", "data": "hello"}}), self.folder)
        self.assertFalse(images)
        self.assertIn("hello", json.dumps(clean))

    def test_invalid_sources_and_unsupported_references_are_rejected(self):
        bad = [{"type": "image_url", "image_url": {"url": "https://example.com/image.png"}},
               {"type": "file", "file": {"file_id": "file_private"}},
               {"type": "file", "file": {"filename": "/etc/passwd"}},
               {"type": "image", "source": {"type": "base64", "data": "!bad", "media_type": "image/png"}},
               {"type": "image", "source": {"type": "base64", "data": encoded(b"bad image"), "media_type": "image/png"}},
               {"type": "image_url", "image_url": 42}, {"type": "image", "source": "bad"},
               {"type": "image_url", "image_url": {"url": 42}},
               {"type": "image", "source": {"type": "base64", "data": "YQ==", "media_type": []}},
               {"type": "file", "file": {"filename": []}},
               {"type": "file", "file": "bad"}, {"type": "document", "source": {"type": "text", "data": 9}}]
        for block in bad:
            with self.subTest(block=block), self.assertRaises(attachments.InputError):
                attachments.prepare(self.request(block), self.folder)

    def test_size_and_image_count_limits(self):
        with patch.object(attachments, "MAX_BYTES", 8), self.assertRaises(attachments.InputError):
            attachments.prepare(self.request(self.image), self.folder)
        with patch.object(attachments, "MAX_IMAGES", 1), self.assertRaises(attachments.InputError):
            attachments.prepare(self.request(self.image, self.image), self.folder)

    @unittest.skipUnless(shutil.which("pdftoppm") and shutil.which("pdfinfo"), "Poppler is optional")
    def test_pdf_is_rendered_as_readable_images(self):
        req = self.request({"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": encoded(text_pdf())}})
        clean, images = attachments.prepare(req, self.folder)
        self.assertEqual(len(images), 1)
        self.assertTrue(images[0].read_bytes().startswith(b"\x89PNG"))
        self.assertIn("PDF pages", json.dumps(clean))

    def test_missing_pdf_tools_give_actionable_error(self):
        block = {"type": "file", "file": {"filename": "test.pdf", "file_data": encoded(text_pdf())}}
        with patch.object(attachments.shutil, "which", return_value=None), self.assertRaises(attachments.InputError) as error:
            attachments.prepare(self.request(block), self.folder)
        self.assertEqual(error.exception.status, 501)
        self.assertIn("Poppler", str(error.exception))

    def test_cli_image_transport_and_cleanup_for_all_backends(self):
        for model in ("codex", "sonnet", "opencode"):
            paths = []

            def cli(argv, **kwargs):
                if model == "sonnet":
                    payload = json.loads(kwargs["input"])
                    self.assertEqual(payload["message"]["content"][1]["source"]["data"], encoded(red_png()))
                    self.assertIn("--input-format", argv)
                    stdout = json.dumps({"result": '{"text":"red"}'})
                else:
                    flag = "--image" if model == "codex" else "--file"
                    paths.append(Path(argv[argv.index(flag) + 1]))
                    self.assertEqual(paths[0].read_bytes(), red_png())
                    stdout = '{"text":"red"}'
                return SimpleNamespace(returncode=0, stdout=stdout, stderr="")
            with self.subTest(model=model), patch.object(state, "AUTO", agent.auto_cmd(model)), patch.object(
                    agent.subprocess, "run", side_effect=cli):
                self.assertEqual(agent.run_auto("anthropic", self.request(self.image), "test"), {"text": "red"})
            self.assertTrue(all(not p.exists() for p in paths))

    def test_temp_media_is_deleted_on_cli_timeout(self):
        paths = []

        def timeout(argv, **_):
            paths.append(Path(argv[argv.index("--image") + 1]))
            raise subprocess.TimeoutExpired(argv, 1)
        with patch.object(state, "AUTO", agent.auto_cmd("codex")), patch.object(agent.subprocess, "run", side_effect=timeout):
            answer = agent.run_auto("anthropic", self.request(self.image), "timeout")
        self.assertEqual(answer["status"], 504)
        self.assertFalse(paths[0].exists())

    def test_offline_image_replay_does_not_decode_or_invoke_cli(self):
        state.AUTO = agent.auto_cmd("codex")
        req = self.request(self.image)
        with patch.object(agent, "run_auto", return_value={"text": "red"}):
            answers.get_answer("anthropic", "/v1/messages", req, "record")
        state.REPLAY = True
        with patch.object(attachments, "prepare", side_effect=AssertionError("must use fixture")):
            self.assertEqual(answers.get_answer("anthropic", "/v1/messages", req, "replay"), ({"text": "red"}, "CACHE"))

    def test_agent_can_inspect_images_until_answer_then_media_is_removed(self):
        store.dirs()
        with concurrent.futures.ThreadPoolExecutor(1) as pool:
            future = pool.submit(agent.wait_for_agent, "anthropic", "/v1/messages", self.request(self.image), "image-agent")
            claimed = agent.claim(5)
            self.assertEqual(len(claimed), 1)
            queued = json.loads((state.DIR / "claimed" / "image-agent.json").read_text())
            path = Path(queued["images"][0])
            self.assertTrue(path.exists())
            self.assertIn(str(path), agent.prompt("image-agent"))
            agent.answer("image-agent", '{"text":"red"}')
            self.assertEqual(future.result(timeout=5), {"text": "red"})
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
