"""Runtime settings shared by the CLI, server and agent helpers.

The values are module attributes rather than constants so `configure()` and the
built-in selftest can change them, and so tests can patch one module and have
every caller see the change.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from ..ui import dashboard

PROJECT_ROOT = Path(__file__).resolve().parents[3]  # the checkout or installed tree that holds bridge.py

VERSION = "1.14.0"

JOURNAL = None  # dashboard.Journal while `serve` runs
PROJECT = Path.cwd().name  # the folder the server runs in, shown per call
PROJECT_SESSION = ""  # nonempty when the app discovers the bridge through its project
QUIET = False  # True while the dashboard owns the terminal

CLAUDE_CMD = ('claude -p --model {m} --effort {e} --tools "" --system-prompt "You stand in for an LLM API. '
              'Follow the instructions in the user message exactly." --strict-mcp-config --disable-slash-commands '
              '--no-session-persistence --output-format json')
# Codex: prompt on stdin ("-"). Without -m it uses the model in your Codex config (your ChatGPT plan).
CODEX_CMD = "codex exec --ephemeral --sandbox read-only{model}{effort} --json -"
# opencode takes the message as an argument, not stdin, so the prompt goes in a file it attaches.
# Without -m it uses the default model in your opencode config (any provider you set up there).
OPENCODE_CMD = ('opencode run --archived{model} -f {{prompt_file}} --format json '
                '"Follow the instructions in the attached file exactly. Reply with only the JSON object it asks for."')
# Environment variables a child CLI must not inherit while they point back at this bridge.
REDIRECT_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT",
                 "ANTHROPIC_FOUNDRY_BASE_URL", "ANTHROPIC_FOUNDRY_API_KEY", "AZURE_AI_ENDPOINT", "AZURE_INFERENCE_ENDPOINT",
                 "GOOGLE_GEMINI_BASE_URL", "GOOGLE_VERTEX_BASE_URL", "GOOGLE_API_KEY", "GEMINI_API_KEY",
                 "AWS_ENDPOINT_URL_BEDROCK_RUNTIME", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")

STATE = Path(os.environ.get("BRIDGE_STATE", str(Path.home() / ".mockingbird" / "active.json")))
DIR = Path(os.environ.get("BRIDGE_DIR", ".llm-bridge")).resolve()
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
BRIDGE_PORTS = set()
TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "900"))
AUTO = os.environ.get("BRIDGE_AUTO", "")
REAL = os.environ.get("BRIDGE_REAL") == "1"  # serve --real: misses go to the real API
SLOTS = threading.BoundedSemaphore(int(os.environ.get("BRIDGE_PARALLEL", "4")))
_cache = os.environ.get("BRIDGE_CACHE", str(dashboard.CACHE_DIR))  # one cache for every project; "off" disables
CACHE = None if _cache.lower() == "off" else Path(_cache).resolve()
REDACT = os.environ.get("BRIDGE_REDACT") == "1"
REPLAY = os.environ.get("BRIDGE_REPLAY") == "1"
NAMESPACE = os.environ.get("BRIDGE_NAMESPACE", "")
FAULTS = []
FAULT_LOCK = threading.Lock()
MODE_LOCK = threading.Lock()
ACTIVE_REQUESTS = 0
# The coding agent (claude, codex or opencode) that answers when serve has no --model. Set by install and `mockingbird agent`.
AGENT_PREF = dashboard.DEFAULT_DB.parent / "agent"
