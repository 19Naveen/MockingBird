import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import _bootstrap  # noqa: F401
from mockingbird import cli
from mockingbird.core import agent, state
from mockingbird.ui import dashboard


def installed(*clis):
    """Make only these CLIs look installed on PATH."""
    return patch.object(agent.shutil, "which", side_effect=lambda name: f"/usr/bin/{name}" if name in clis else None)


class AgentChoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.pref = patch.object(state, "AGENT_PREF", Path(self.tmp.name) / "agent")
        self.pref.start()

    def tearDown(self):
        self.pref.stop()
        self.tmp.cleanup()

    def test_no_agent_installed_means_agent_mode(self):
        with installed():
            self.assertIsNone(agent.default_spec())
            self.assertIn("No coding agent found", agent.describe_default())

    def test_one_installed_is_used(self):
        with installed("codex"):
            self.assertEqual(agent.default_spec(), "codex")

    def test_several_installed_default_is_claude_code(self):
        with installed("opencode", "claude", "codex"):
            self.assertEqual(agent.default_spec(), "sonnet")
            line = agent.describe_default()
            self.assertIn("Claude Code answers", line)
            self.assertIn("Also installed: Codex, opencode", line)
            self.assertIn("mockingbird agent codex", line)

    def test_saved_choice_wins_while_installed(self):
        agent.save_agent("opencode")
        with installed("claude", "opencode"):
            self.assertEqual(agent.default_spec(), "opencode")
            self.assertIn("(saved choice)", agent.describe_default())

    def test_saved_choice_that_is_gone_falls_back(self):
        agent.save_agent("codex")
        with installed("claude"):
            self.assertEqual(agent.default_spec(), "sonnet")
            self.assertIn("saved codex is not installed", agent.describe_default())

    def test_clear_forgets_the_choice(self):
        agent.save_agent("codex")
        agent.save_agent(None)
        self.assertIsNone(agent.saved_agent())

    def test_install_default_saves_first_found_only_when_nothing_saved(self):
        with installed("codex", "opencode"):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                self.assertEqual(cli.agent_command(["--default"]), 0)
            self.assertEqual(agent.saved_agent(), "codex")
            self.assertIn("Codex answers", out.getvalue())
            agent.save_agent("opencode")
            with contextlib.redirect_stdout(io.StringIO()):
                cli.agent_command(["--default"])
            self.assertEqual(agent.saved_agent(), "opencode")  # a reinstall or update keeps the user's choice

    def test_install_default_with_no_agent_saves_nothing(self):
        with installed(), contextlib.redirect_stdout(io.StringIO()) as out:
            self.assertEqual(cli.agent_command(["--default"]), 0)
        self.assertIsNone(agent.saved_agent())
        self.assertIn("No coding agent found", out.getvalue())

    def test_agent_command_saves_only_installed_names(self):
        with installed("claude"):
            with self.assertRaises(SystemExit):
                cli.agent_command(["codex"])  # not installed
            with self.assertRaises(SystemExit):
                cli.agent_command(["gemini"])  # not a coding agent mockingbird knows
            with contextlib.redirect_stdout(io.StringIO()):
                cli.agent_command(["claude"])
        self.assertEqual(agent.saved_agent(), "claude")


class ModeRotationTests(unittest.TestCase):
    def test_rotates_installed_backends_and_real(self):
        meta = {"mode": "real API, repeats replayed"}
        with installed("claude", "codex", "opencode"), \
                patch.object(state, "REAL", True), patch.object(state, "AUTO", ""), \
                patch.object(state, "REPLAY", False), patch.object(state, "ACTIVE_REQUESTS", 0):
            cli.rotate_mode(meta)
            self.assertFalse(state.REAL)
            self.assertEqual(state.AUTO, agent.auto_cmd("sonnet"))
            self.assertEqual(meta["mode"], "Claude Code (Sonnet)")
            cli.rotate_mode(meta)
            self.assertEqual(state.AUTO, agent.auto_cmd("codex"))
            cli.rotate_mode(meta)
            self.assertEqual(state.AUTO, agent.auto_cmd("opencode"))
            cli.rotate_mode(meta)
            self.assertTrue(state.REAL)
            self.assertEqual(state.AUTO, "")

    def test_replay_and_active_calls_prevent_switching(self):
        for replay, active in ((True, 0), (False, 1)):
            meta = {"mode": "real API, repeats replayed"}
            with installed("claude"), patch.object(state, "REAL", True), \
                    patch.object(state, "AUTO", ""), patch.object(state, "REPLAY", replay), \
                    patch.object(state, "ACTIVE_REQUESTS", active):
                self.assertTrue(cli.rotate_mode(meta))
                self.assertTrue(state.REAL)
                self.assertEqual(state.AUTO, "")
                self.assertEqual(meta["mode"], "real API, repeats replayed")


class ModeLabelTests(unittest.TestCase):
    def test_claude_is_explicit_sonnet(self):
        self.assertEqual(agent.mode_label("claude"), "Claude Code (Sonnet)")

    def test_codex_root_model_and_profile_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"CODEX_HOME": tmp}):
            config = Path(tmp) / "config.toml"
            config.write_text('model = "gpt-5.4"\n')
            self.assertEqual(agent.mode_label("codex"), "Codex (gpt-5.4)")
            config.write_text('model = "gpt-5.4"\nprofile = "work"\n')
            self.assertEqual(agent.mode_label("codex"), "Codex (default)")

    def test_opencode_model_and_location_fallback(self):
        response = {"location": {"directory": str(Path.cwd())}, "data": {"modelID": "gpt-6.1"}}
        with patch.object(agent.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(response))):
            self.assertEqual(agent.mode_label("opencode"), "OpenCode (gpt-6.1)")
        response["location"]["directory"] = "/some/other/project"
        with patch.object(agent.subprocess, "run", return_value=SimpleNamespace(
                returncode=0, stdout=json.dumps(response))):
            self.assertEqual(agent.mode_label("opencode"), "OpenCode (default)")


class FakeWindow:
    """Records what would be drawn, so the footer layout is checked without a terminal."""

    def __init__(self, height, width):
        self.height, self.width, self.cells = height, width, {}

    def getmaxyx(self):
        return self.height, self.width

    def addnstr(self, y, x, text, n, attr=0):
        for i, ch in enumerate(text[:n]):
            self.cells[(y, x + i)] = ch

    def row(self, y):
        return "".join(self.cells.get((y, x), " ") for x in range(self.width)).rstrip()


COLORS = {name: 0 for name in ("chip", "muted", "ok", "text", "fresh", "accent")}
META = {"address": "http://127.0.0.1:8787", "mode": "real API, repeats replayed", "hook": "off"}
IDLE = SimpleNamespace(asking=0, rows=[])
HINTS = [("1 2 3", "Pages"), ("w", "Show Analytics"), ("q", "Quit")]


class FooterLayoutTests(unittest.TestCase):
    def test_status_is_in_the_footer_at_the_right_edge(self):
        win = FakeWindow(24, 120)
        dashboard._header(win, COLORS, 0)
        dashboard._footer(win, COLORS, HINTS, status=dashboard._status(COLORS, IDLE, META))
        self.assertNotIn(":8787", win.row(0))
        footer = win.row(23)
        self.assertTrue(footer.startswith("1 2 3 Pages"))
        self.assertIn("● :8787  real API", footer)
        self.assertNotIn("hook off", footer)
        self.assertEqual(len(footer), 120 - dashboard.MARGIN)

    def test_toast_temporarily_replaces_status(self):
        win = FakeWindow(24, 120)
        dashboard._footer(win, COLORS, [("q", "Quit")], "Press x again to delete.", 0,
                          status=dashboard._status(COLORS, IDLE, META))
        footer = win.row(23)
        self.assertIn("Press x again", footer)
        self.assertNotIn("● :8787", footer)

    def test_offline_replay_has_compact_mode(self):
        meta = {**META, "mode": "offline replay"}
        self.assertEqual("".join(text for text, _ in dashboard._status(COLORS, IDLE, meta)),
                         "● :8787  replay")


class EmptyStateTests(unittest.TestCase):
    def test_live_animation_changes_and_keeps_sdk_hint(self):
        snap = SimpleNamespace(stats=SimpleNamespace(total=0))
        frames = []
        for now in (0, 0.8):
            win = FakeWindow(24, 100)
            with patch.object(dashboard.time, "monotonic", return_value=now):
                dashboard._live(win, snap, COLORS, META)
            screen = "\n".join(win.row(y) for y in range(24))
            self.assertIn("No calls yet", screen)
            self.assertIn("ANTHROPIC_BASE_URL=http://127.0.0.1:8787", screen)
            frames.append(screen)
        self.assertNotEqual(frames[0], frames[1])

    def test_empty_analytics_and_cache(self):
        for render, expected in (
            (lambda w: dashboard._analytics_page(w, SimpleNamespace(total=0), COLORS),
             "Nothing to analyse yet"),
            (lambda w: dashboard._cache_page(w, [], COLORS, {"cache": "/tmp/cache"}, 0),
             "No saved answers yet"),
        ):
            win = FakeWindow(24, 100)
            render(win)
            self.assertIn(expected, "\n".join(win.row(y) for y in range(24)))
            self.assertEqual(win.row(0), "")
            self.assertEqual(win.row(23), "")

    def test_small_terminal_does_not_draw_on_header_or_footer(self):
        win = FakeWindow(5, 20)
        dashboard._empty_state(win, COLORS, "icon", "No calls yet", "Waiting", "hint")
        self.assertEqual(win.row(0), "")
        self.assertEqual(win.row(4), "")
        self.assertIn("No calls yet", win.row(2))


class AnalyticsSpacingTests(unittest.TestCase):
    def test_spacing_adapts_to_terminal_height(self):
        model = dashboard.ModelStats("test-model", 1, 0, 1, 0, 1.0)
        stats = dashboard.Analytics(total=1, fresh=1, hit_rate=0, saved=0, p50=1, p95=1, slowest=1,
                                    per_model=[model], projects=[("test-project", 1)],
                                    daily=[(1700000000, 0, 1)], first_at=1700000000)
        colors = {**COLORS, "border": 0, "name": 0, "err": 0}
        gaps = []
        for height in (29, 42):
            win = FakeWindow(height, 120)
            dashboard._analytics_page(win, stats, colors)
            rows = [win.row(y) for y in range(height)]
            headings = ("Last 14 days", "Speed of fresh answers", "Replayed from the cache",
                        "Failed calls", "Projects")
            indices = [next(i for i, row in enumerate(rows) if heading in row) for heading in headings]
            self.assertEqual(indices, sorted(indices))
            self.assertEqual(rows[-1], "")
            gaps.append(indices[-1] - indices[0])
        self.assertGreater(gaps[1], gaps[0])


if __name__ == "__main__":
    unittest.main()
