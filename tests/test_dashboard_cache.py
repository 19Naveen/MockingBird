import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

import _bootstrap  # noqa: F401
from mockingbird.ui import dashboard as ui


class Window:
    def __init__(self, height, width):
        self.size = (height, width)
        self.cells = {}

    def getmaxyx(self):
        return self.size

    def addnstr(self, y, x, text, n, attr):
        for char in text[:n]:
            cells = ui._cells(char)
            if cells:
                self.cells[y, x] = char
                if cells == 2:
                    self.cells[y, x + 1] = ""
                x += cells

    def row(self, y):
        return "".join(self.cells.get((y, x), " ") for x in range(self.size[1])).rstrip()


class DashboardCacheTests(unittest.TestCase):
    def test_cache_answerer_comes_from_saved_backend_not_requested_model(self):
        requested = "gpt-6.1-sol"
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            for key, data in (
                ("claude", {"meta": {"model": requested, "backend": 'claude -p --model sonnet --effort medium'},
                            "answer": {"text": "ok"}}),
                ("opencode", {"meta": {"model": requested, "backend": "opencode run -m openai/gpt-test"},
                              "answer": {"text": "ok"}}),
                ("codex", {"meta": {"model": requested, "backend": "codex exec --json -"},
                           "answer": {"text": "ok"}}),
                ("real", {"meta": {"model": requested, "via": "https://example.openai.azure.com"},
                          "http": {"content_type": "application/json", "body": '{"model":"actual-model"}'}}),
                ("old", {"text": "legacy"}),
            ):
                (path / f"{key}.json").write_text(json.dumps(data))
            entries = {e.key: e for e in ui.cache_entries(path, [])}
        self.assertEqual(entries["claude"].model, requested)
        self.assertEqual(entries["claude"].answered_by, "Claude Code (Sonnet)")
        self.assertEqual(entries["opencode"].answered_by, "OpenCode (openai/gpt-test)")
        self.assertEqual(entries["codex"].answered_by, "Codex")
        self.assertEqual(entries["real"].answered_by, "Real API (Azure) · actual-model")
        self.assertEqual(entries["old"].answered_by, "Unknown")

    def test_cache_panel_wraps_metadata_and_pretty_prints_json(self):
        entry = ui.CacheEntry(key="test", saved=1700000000, model="gpt-6.1-sol", project="長いプロジェクト " * 12,
                              asked="Question " * 25, label="", answer='{"changes":[{"field":"名称","value":7}]}',
                              size=10, used=1, last_used=1700000000, answered_by="Claude Code (Sonnet)",
                              tokens_in=20013, tokens_out=379)
        colors = dict.fromkeys(("accent", "text", "muted", "name", "ok", "border"), 0)
        for width in (65, 170):
            with self.subTest(width=width):
                win = Window(46, width)
                ui._cache_page(win, [entry], colors, {"cache": "/tmp/cache"}, 0)
                screen = "\n".join(win.row(y) for y in range(46))
                self.assertIn("Answered by: Claude Code (Sonnet)", screen)
                self.assertIn("Requested: gpt-6.1-sol", screen)
                self.assertIn('"changes": [', screen)
                self.assertIn('│        "field": "名称"', screen)
                box_w = min(width - ui.MARGIN - 1, 120)
                for y in range(46):
                    if win.cells.get((y, 0)) in ("╭", "│", "╰"):
                        self.assertFalse(any(row == y and x > box_w for row, x in win.cells))
                self.assertEqual(win.row(45), "")

    def test_journal_adds_backend_column_without_losing_history(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "calls.db"
            with closing(sqlite3.connect(path)) as db, db:
                db.executescript(ui.SCHEMA)
                db.execute("INSERT INTO calls (at, model) VALUES (1, 'old')")
            journal = ui.Journal(path)
            journal.record(None, ui.Call(2, "new", "openai", "FRESH", 1, 200, "", "test",
                                         answered_by="Claude Code (Sonnet)"))
            self.assertEqual([(c.model, c.answered_by) for c in journal.calls()],
                             [("old", ""), ("new", "Claude Code (Sonnet)")])
