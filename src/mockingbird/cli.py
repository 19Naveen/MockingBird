"""Fake LLM endpoint for tests: parks each API call on disk until a coding-agent subagent answers it.

  serve [--model M[E]]  HTTP shim on 127.0.0.1 (Anthropic Messages, OpenAI/Azure/Foundry Chat Completions,
        [--auto CMD]    Bedrock Converse and Gemini generateContent). Shows a live dashboard in a terminal; --plain
        [--plain]       (or no terminal) logs lines to stderr instead.
                        --model sonnet[high] answers each request with a lean `claude -p` call (gpt*/o*: `codex exec`).
        [--redact]      Disable disk caching and hide model, project and error details in history and logs.
        [--replay]      Offline: replay saved answers, return 409 on a cache miss, never call a model or upstream.
        [--faults FILE] Apply a JSON list of errors, raw bodies and delays, one step per incoming API request.
        [--real]        --real: no stand-in model. A call seen before is replayed; anything new goes to the real
                        API with the app's own key (Anthropic, OpenAI, Azure/Foundry) and its answer is saved.
        [--agent]       Default: sonnet[medium] when the claude CLI exists. --auto CMD runs any command instead
                        (prompt on stdin, JSON on stdout). --agent: a coding agent serves requests via next/answer.
                        Every call is logged (metadata only) to ~/.local/state/mockingbird/calls.db.
  stats                 print the analytics (all calls ever, across projects)
  run [serve options] -- COMMAND ...
                        start an isolated bridge, route only COMMAND to it, then clean up; returns its exit code.
  update                install the newest release from GitHub (your history and saved answers stay)
  uninstall [--purge]   remove mockingbird; --purge also deletes the history and saved answers
  cache [--purge]       list the saved answers, or delete them all (asks first; --yes skips the question).
                        Env BRIDGE_CACHE picks another folder
  hook install|uninstall|status [PYTHON]
                        with MOCKINGBIRD_ENABLED=1 and while serve runs, Python API calls go to the bridge; when it
                        stops, they go to the real API. No args: global (opted-in Python started from your shell,
                        via a PYTHONPATH line in ~/.bashrc and ~/.zshrc). PYTHON: only that one environment.
  next [--wait S]       claim every pending request, print one JSON line each (exit 1 if none)
  prompt ID             print the full subagent prompt for a claimed request
  answer ID             read the subagent's JSON reply on stdin, validate it, hand it to the shim
  selftest              run the built-in check
  version               print the mockingbird version

Env: BRIDGE_DIR (./.llm-bridge), BRIDGE_PORT (8787), BRIDGE_TIMEOUT seconds (900),
     BRIDGE_AUTO (same as --auto), BRIDGE_PARALLEL (4, max auto commands at once),
     BRIDGE_REPLAY=1 (offline), BRIDGE_NAMESPACE (fixture version, in addition to backend identity),
     BRIDGE_CACHE (~/.local/state/mockingbird/cache, or a folder, or "off"): saved answers, replayed instantly.
Stdlib only; optional PDF input requires Poppler (pdfinfo, pdftoppm).
"""

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from .api import responses, server
from .core import agent, log, state, store
from .ui import dashboard

REPO = "19Naveen/MockingBird"
ROOT = state.PROJECT_ROOT

HOOK_DIR = Path.home() / ".mockingbird" / "hook"
RC_FILES = (".bashrc", ".zshrc")
RC_BLOCK = ('# >>> mockingbird >>>\n'
            'if [ "${MOCKINGBIRD_ENABLED:-}" = 1 ]; then\n'
            '  export PYTHONPATH="$HOME/.mockingbird/hook${PYTHONPATH:+:$PYTHONPATH}"\n'
            'fi\n'
            '# <<< mockingbird <<<\n')
SITECUSTOMIZE = '''\
# mockingbird: loads the opt-in LLM-call hook via PYTHONPATH.
# Remove with `mockingbird hook uninstall`.
try:
    import mockingbird_hook  # noqa: F401
except Exception as e:  # a broken hook must never break Python
    import sys
    print(f"[mockingbird] hook disabled: {e!r}", file=sys.stderr)


def _chain():
    # This file hides any other sitecustomize (Debian ships one), so run that one as well.
    import importlib.machinery, importlib.util, os, sys
    here = os.path.dirname(os.path.abspath(__file__))
    rest = [p for p in sys.path if os.path.abspath(p or os.curdir) != here]
    spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
    while spec and spec.loader:
        # A private runner and an older global hook can both be on PYTHONPATH; don't chain them recursively.
        if spec.origin and spec.origin.endswith(".py"):
            with open(spec.origin, "rb") as f:
                ours = f.read(32).startswith(b"# mockingbird:")
            if ours:
                other = os.path.dirname(os.path.abspath(spec.origin))
                rest = [p for p in rest if os.path.abspath(p or os.curdir) != other]
                spec = importlib.machinery.PathFinder.find_spec("sitecustomize", rest)
                continue
        spec.loader.exec_module(importlib.util.module_from_spec(spec))
        break


_chain()
del _chain
'''


def configure(args):
    """Shared options for serve and the bridge subprocess started by run."""
    import argparse, math
    parser = argparse.ArgumentParser(prog="mockingbird serve")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--model")
    mode.add_argument("--auto")
    mode.add_argument("--agent", action="store_true")
    mode.add_argument("--real", action="store_true")
    parser.add_argument("--plain", action="store_true")
    parser.add_argument("--redact", action="store_true")
    parser.add_argument("--replay", action="store_true")
    parser.add_argument("--faults")
    opts = parser.parse_args(args)
    state.REAL = state.REAL or opts.real
    state.REPLAY = state.REPLAY or opts.replay
    state.REDACT = state.REDACT or opts.redact
    if state.REPLAY and (not state.CACHE or state.REDACT):
        parser.error("--replay requires an enabled cache and cannot use --redact")
    if state.REDACT:
        state.CACHE = None
    if opts.faults:
        try:
            faults = json.loads(Path(opts.faults).read_text())
            if not isinstance(faults, list):
                raise ValueError("expected a JSON list")
            for step in faults:
                if not isinstance(step, dict) or set(step) - {"delay", "status", "error", "body"}:
                    raise ValueError("steps accept only delay, status, error and body")
                delay = step.get("delay", 0)
                if type(delay) not in (float, int) or not math.isfinite(delay) or not 0 <= delay <= state.TIMEOUT:
                    raise ValueError(f"delay must be between 0 and {state.TIMEOUT} seconds")
                status = step.get("status", 200 if "body" in step else 500)
                if type(status) is not int or not (200 if "body" in step else 400) <= status <= 599:
                    raise ValueError("invalid HTTP status")
                if ("error" in step and not isinstance(step["error"], str)) or (
                        "body" in step and (not isinstance(step["body"], str) or "error" in step)):
                    raise ValueError("body/error must be strings and cannot be combined")
        except (OSError, ValueError) as e:
            parser.error(f"invalid faults file: {e}")
        state.FAULTS = faults
    spec = opts.model
    if state.REAL:
        spec, state.AUTO = None, ""
    elif opts.agent:
        state.AUTO = ""
    elif opts.auto:
        state.AUTO = opts.auto
    elif not spec and not state.AUTO:
        spec = agent.default_spec()
        if not spec:
            log.log("no CLI found: agent mode (or pass --model to select an offline replay namespace)")
    if spec:
        state.AUTO = agent.auto_cmd(spec)
    return spec, opts.plain


def run_command(args):
    """Own two process groups: a private bridge and the command under test. Never touch global hook state."""
    import tempfile
    if "--" not in args:
        sys.exit("usage: mockingbird run [serve options] -- COMMAND ...")
    split = args.index("--")
    options, command = args[:split], args[split + 1:]
    if not command:
        sys.exit("run needs a command after --")
    configure(options)  # reject invalid options before starting anything
    processes = []
    with tempfile.TemporaryDirectory(prefix="mockingbird-run-") as folder:
        root = Path(folder)
        active = root / "active.json"
        (root / "sitecustomize.py").write_text(SITECUSTOMIZE)
        (root / "mockingbird_hook.py").write_text((ROOT / "mockingbird_hook.py").read_text())
        server_env = {**agent._child_env(), "BRIDGE_PORT": "0", "BRIDGE_DIR": str(root / "queue"), "BRIDGE_STATE": str(active)}
        try:
            bridge = subprocess.Popen([sys.executable, str(ROOT / "bridge.py"), "serve", "--plain", *options],
                                      env=server_env, start_new_session=True)
            processes.append(bridge)
            deadline = time.monotonic() + 10
            while not active.exists():
                if bridge.poll() is not None:
                    return bridge.returncode or 1
                if time.monotonic() > deadline:
                    sys.exit("private bridge did not start within 10 seconds")
                time.sleep(0.05)
            port = json.loads(active.read_text())["port"]
            env = {**os.environ, "MOCKINGBIRD_ENABLED": "1", "MOCKINGBIRD_STATE": str(active),
                   "PYTHONPATH": str(root) + os.pathsep + os.environ.get("PYTHONPATH", ""),
                   "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{port}",
                   "OPENAI_BASE_URL": f"http://127.0.0.1:{port}/v1", "AZURE_OPENAI_ENDPOINT": f"http://127.0.0.1:{port}",
                   "ANTHROPIC_FOUNDRY_BASE_URL": f"http://127.0.0.1:{port}/anthropic",
                   "AZURE_AI_ENDPOINT": f"http://127.0.0.1:{port}/models", "AZURE_INFERENCE_ENDPOINT": f"http://127.0.0.1:{port}/models",
                   "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}", "GOOGLE_VERTEX_BASE_URL": f"http://127.0.0.1:{port}",
                   "AWS_ENDPOINT_URL_BEDROCK_RUNTIME": f"http://127.0.0.1:{port}"}
            if not state.REAL:
                env.update({key: "bridge-dummy" for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY",
                    "ANTHROPIC_FOUNDRY_API_KEY", "GOOGLE_API_KEY", "GEMINI_API_KEY", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN")})
                env.setdefault("AWS_DEFAULT_REGION", env.get("AWS_REGION", "us-east-1"))
                env["AWS_EC2_METADATA_DISABLED"] = "true"
            child = subprocess.Popen(command, env=env, start_new_session=True)
            processes.append(child)
            try:
                code = child.wait()
                return code if code >= 0 else 128 - code
            except KeyboardInterrupt:
                os.killpg(child.pid, signal.SIGINT)
                return 130
        except OSError as e:
            log.log(f"could not start command: {e}")
            return 127
        finally:
            for process in reversed(processes):
                try:
                    os.killpg(process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    os.killpg(process.pid, signal.SIGKILL)
                    process.wait()


def _version(text):
    return tuple(int(p) for p in re.findall(r"\d+", text)[:3])


def purge_cache(folder, yes=False, interactive=None):
    """Delete every saved answer. History stays; the next call to each answer is asked fresh."""
    files = sorted(folder.glob("*.json")) if folder.is_dir() else []
    if not files:
        print(f"The cache is already empty ({folder}).")
        return
    size = dashboard._size(sum(f.stat().st_size for f in files))
    print(f"This deletes {len(files)} saved answers ({size}) in {folder}.")
    print("Your call history stays. The next call to each answer will be asked fresh.")
    if not yes:
        if not (sys.stdin.isatty() if interactive is None else interactive):
            sys.exit("Run it again with --yes to confirm.")
        if input("Delete them? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Nothing was deleted.")
            return
    for f in files:
        f.unlink(missing_ok=True)
    print(f"Deleted {len(files)} saved answers.")


def update():
    """Discover the latest release, then run the installer and payloads from that exact tag."""
    import tempfile, urllib.request
    try:
        with urllib.request.urlopen(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=30) as r:
            tag = json.load(r)["tag_name"]
        if not re.fullmatch(r"v\d+\.\d+\.\d+", tag):
            sys.exit(f"unsupported release tag: {tag!r}")
        latest = tag[1:]
        if _version(latest) <= _version(state.VERSION):
            print(f"mockingbird {state.VERSION} is up to date (newest release: {latest}).")
            return
        base = f"https://raw.githubusercontent.com/{REPO}/{tag}"
        with urllib.request.urlopen(base + "/install.sh", timeout=30) as r:
            script = r.read().decode()
    except OSError as e:
        sys.exit(f"could not reach GitHub ({e}). Check your connection and try again.")
    except (ValueError, KeyError, TypeError) as e:
        sys.exit(f"invalid GitHub release metadata: {e}")
    found = re.search(r"^VERSION=(\S+)", script, re.M)
    if not found or found.group(1) != latest:
        sys.exit("release installer version does not match its tag")
    print(f"updating mockingbird {state.VERSION} -> {latest}")
    with tempfile.NamedTemporaryFile("w", suffix="-install.sh", delete=False) as f:
        f.write(script)
    try:
        env = {k: v for k, v in os.environ.items() if k != "MOCKINGBIRD_SRC"}
        env["MOCKINGBIRD_BASE"] = base
        code = subprocess.run(["bash", f.name], env=env).returncode
    finally:
        os.unlink(f.name)
    if code == 0 and state.STATE.exists():
        print("A server is running on the old version. Press q on its dashboard and start it again.")
    sys.exit(code)


def hook_global(action):
    rcs = [Path.home() / f for f in RC_FILES if (Path.home() / f).exists()]
    block = re.compile(r"\n?# >>> mockingbird >>>.*?# <<< mockingbird <<<\n", re.S)
    if action == "install":
        HOOK_DIR.mkdir(parents=True, exist_ok=True)
        (HOOK_DIR / "mockingbird_hook.py").write_text((ROOT / "mockingbird_hook.py").read_text())
        (HOOK_DIR / "sitecustomize.py").write_text(SITECUSTOMIZE)
        for rc in rcs:
            rc.write_text(block.sub("", rc.read_text()) + "\n" + RC_BLOCK)
        print(f"global hook installed: {HOOK_DIR}\n  PYTHONPATH line added to: {', '.join(r.name for r in rcs) or 'none found'}"
              "\n  run tests with: MOCKINGBIRD_ENABLED=1 bash -ic 'pytest'"
              "\n  only opted-in test processes route calls while the server runs")
    elif action == "uninstall":
        for rc in rcs:
            rc.write_text(block.sub("", rc.read_text()))  # install added "\n" + block, so this restores the file exactly
        shutil.rmtree(HOOK_DIR, ignore_errors=True)
        print("global hook removed; open a new terminal")
    else:
        on_path = str(HOOK_DIR) in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        print(f"global hook: {'installed' if (HOOK_DIR / 'sitecustomize.py').exists() else 'not installed'}"
              f"{', on PYTHONPATH' if on_path else ', not on PYTHONPATH'}"
              f"; opt-in: {'enabled' if os.environ.get('MOCKINGBIRD_ENABLED') == '1' else 'disabled'}")
        print(f"bridge: {'running ' + state.STATE.read_text() if state.STATE.exists() else 'stopped (real API)'}")


def hook(action, python=None):
    if python is None:
        return hook_global(action)
    try:
        site = Path(subprocess.run([python, "-c", "import sysconfig; print(sysconfig.get_paths()['purelib'])"],
                                   capture_output=True, text=True, check=True).stdout.strip())
    except (OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"could not run {python}: {e}")
    pth, mod = site / "mockingbird_hook.pth", site / "mockingbird_hook.py"
    if action == "install":
        mod.write_text((ROOT / "mockingbird_hook.py").read_text())
        pth.write_text('import os; os.environ.get("MOCKINGBIRD_ENABLED") == "1" and __import__("mockingbird_hook")\n')
        print(f"hook installed in {site}\n  MOCKINGBIRD_ENABLED=1 + bridge running -> LLM calls go to mockingbird\n"
              f"  bridge stopped -> real API\n  never install it in a production environment")
    elif action == "uninstall":
        pth.unlink(missing_ok=True)
        mod.unlink(missing_ok=True)
        print(f"hook removed from {site}")
    else:
        print(f"hook: {'installed' if pth.exists() else 'not installed'} in {site}")
        print(f"opt-in: {'enabled' if os.environ.get('MOCKINGBIRD_ENABLED') == '1' else 'disabled'}")
        print(f"bridge: {'running ' + state.STATE.read_text() if state.STATE.exists() else 'stopped (real API)'}")


def selftest():
    import sqlite3
    import tempfile, urllib.request, urllib.error
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    state.DIR, state.AUTO, state.CACHE, state.QUIET = Path(tempfile.mkdtemp()), "", None, True
    state.JOURNAL = dashboard.Journal(state.DIR / "calls.db")  # never the real journal

    def wait_for_calls(count, timeout=5):
        # Journal records are written after the response is sent, so a client can return first.
        deadline = time.time() + timeout
        while len(state.JOURNAL.calls()) < count and time.time() < deadline:
            time.sleep(0.01)

    srv = server.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    replies = iter([
        '```json\n{"tool_calls": [{"name": "get_weather", "input": {"city": "Singapore"}}]}\n```',
        '{"text": "hello"}',
        '{"error": "overloaded", "status": 529}',
    ])

    def serve_requests():
        for _ in range(3):
            (r,) = agent.claim(10)
            assert "Request JSON" in agent.prompt(r["id"])
            agent.answer(r["id"], next(replies))
    threading.Thread(target=serve_requests, daemon=True).start()

    def post(path, body):
        rq = urllib.request.Request(url + path, json.dumps(body).encode(), {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(rq, timeout=20) as resp:
                return resp.status, resp.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode()

    s, b = post("/v1/messages", {"model": "claude-x", "messages": [{"role": "user", "content": "weather?"}]})
    b = json.loads(b)
    assert s == 200 and b["stop_reason"] == "tool_use" and b["content"][0]["input"] == {"city": "Singapore"}, b
    tc = {"tool_calls": [{"name": "t", "input": {}}]}
    assert responses.anthropic_body(tc, {"m": 1}, "a")["content"][0]["id"] == responses.anthropic_body(tc, {"m": 1}, "b")["content"][0]["id"]
    s, b = post("/openai/deployments/d/chat/completions?api-version=x", {"model": "gpt", "stream": True, "messages": []})
    assert s == 200 and '"content": "hello"' in b and '"finish_reason": "stop"' in b and b.endswith("data: [DONE]\n\n"), b
    s, b = post("/v1/messages", {"model": "claude-x", "messages": []})
    assert s == 529 and "overloaded" in b, (s, b)
    assert post("/v1/embeddings", {})[0] == 501
    assert not list(state.DIR.glob("*/*.json")), "request/answer files must be deleted after replying"

    # auto mode: fake CLI that wraps its JSON in prose and reports whether it inherited the redirect
    fake = ('import os,sys,json; sys.stdin.read(); '
            'print("Sure! " + json.dumps({"text": "auto:" + str("ANTHROPIC_BASE_URL" in os.environ)}))')
    state.AUTO, state.CACHE = f"{sys.executable} -c {shlex.quote(fake)}", state.DIR / "cache"
    os.environ["ANTHROPIC_BASE_URL"] = url
    hi = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
    s, b = post("/v1/messages", hi)
    assert s == 200 and json.loads(b)["content"][0]["text"] == "auto:False", (s, b)  # loop guard held
    s, b = post("/v1/messages", {**hi, "stream": True})
    assert s == 200 and "auto:False" in b and "message_stop" in b, (s, b)
    state.AUTO = f"{sys.executable} -c 'raise SystemExit(3)'"
    s, b = post("/v1/messages", {**hi, "max_tokens": 5})  # cache miss -> failing command -> API error, not cached
    assert s == 502 and "exited 3" in b and len(list(state.CACHE.glob("*.json"))) == 1, (s, b)

    # cache tab: entries carry what was asked and how often they were replayed; v1.6 files still replay
    wait_for_calls(7)  # `used` is counted from journal records, which trail their responses
    entries = dashboard.cache_entries(state.CACHE, state.JOURNAL.calls())
    assert len(entries) == 1 and entries[0].asked == "hi" and entries[0].used == 1, entries
    assert entries[0].answer == "auto:False" and not entries[0].legacy, entries
    legacy = state.CACHE / ("0" * 64 + ".json")
    store.write(legacy, {"text": "old"})
    assert dashboard.cached_answer(json.loads(legacy.read_text())) == {"text": "old"}
    assert any(e.legacy and e.answer == "old" for e in dashboard.cache_entries(state.CACHE, []))
    legacy.unlink()
    assert "1 saved answers" in dashboard.cache_report(state.CACHE, [])
    blocks = {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image"}]}]}
    assert store.asked_text(blocks) == "a [image]" and store.asked_text({"messages": []}) == ""
    result = {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": "shipped"}]}]}
    assert store.asked_text(result) == "[tool result: shipped]", store.asked_text(result)
    # a v1.6 journal (no key column) upgrades in place
    old = sqlite3.connect(state.DIR / "old.db")
    old.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, at REAL NOT NULL, model TEXT, api TEXT, source TEXT,"
                " seconds REAL, status INTEGER, label TEXT, project TEXT)")
    old.execute("INSERT INTO calls (at, model, api, source, seconds, status, label, project)"
                " VALUES (1, 'm', 'a', 'CACHE', 0, 200, 'l', 'p')")
    old.commit()
    old.close()
    upgraded = dashboard.Journal(state.DIR / "old.db")
    assert upgraded.calls()[0].key == ""
    upgraded.record(None, dashboard.Call(2, "m", "a", "CACHE", 0, 200, "l", "p", "k"))
    assert upgraded.calls()[-1].key == "k"
    srv.shutdown()

    # hook on/off switch: live bridge -> its port; stale file from a dead bridge or no file -> real API
    sys.path.insert(0, str(ROOT))
    import mockingbird_hook as hk
    hk.STATE = str(state.DIR / "active.json")
    assert hk.bridge_port() is None
    store.write(Path(hk.STATE), {"port": 9999, "pid": os.getpid()})
    assert hk.bridge_port() == 9999
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    store.write(Path(hk.STATE), {"port": 9999, "pid": dead.pid})
    assert hk.bridge_port() is None

    # dashboard: every call above was recorded with the right source, and the screen data builds from it
    wait_for_calls(7)
    calls, inflight = state.JOURNAL.calls(), state.JOURNAL.inflight()
    sources = [c.source for c in calls]
    assert sources.count("AGENT") == 2 and sources.count("FRESH") == 1 and sources.count("CACHE") == 1, sources
    assert sources.count("ERROR") == 3 and not inflight, sources  # 529, 501, 502
    assert [c.label for c in calls[:2]] == ["tool_use get_weather", "text · 5 chars"], [c.label for c in calls]
    assert sorted(c.status for c in calls if c.source == "ERROR") == [501, 502, 529]
    now = time.time()
    snap = dashboard.build(calls, [("m", "anthropic", now - 3)], now)
    assert snap.in_window == 7 and sum(snap.cached_series) == 1 and sum(snap.fresh_series) == 6 and snap.asking == 1
    asking = [r for r in snap.rows if r.model == "m"]
    assert asking and asking[0].state == "asking" and asking[0].asking_for >= 3 and snap.rows[0].model == "m"
    a = snap.stats
    assert (a.total, a.cached, a.fresh, a.errors) == (7, 1, 3, 3) and a.hit_rate == 0.25 and a.saved is not None
    old = dashboard.Call(now - 7200, "old-model", "openai", "CACHE", 0.0, 200, "x", "p")
    assert any(r.model == "old-model" and r.calls == 0 for r in dashboard.build([old], [], now).rows)  # idle rows stay
    stale = dashboard.Call(now - 7200, "gone", "openai", "ERROR", 0.0, 501, "x", "p")
    assert dashboard.build([stale], [], now).rows[0].state == "idle"  # errors outside the window don't stick
    # --model specs pick the right CLI: Claude plan, ChatGPT plan via Codex, or whatever opencode is set up with
    assert agent.auto_cmd("sonnet").startswith("claude -p --model sonnet --effort medium")
    assert agent.auto_cmd("codex") == state.CODEX_CMD.format(model="", effort="") and agent.auto_cmd("gpt-5[low]") == (
        "codex exec --ephemeral --sandbox read-only -m gpt-5 -c model_reasoning_effort=low --json -")
    assert agent.auto_cmd("opencode").startswith("opencode run --archived -f {prompt_file}")
    assert " -m anthropic/claude-sonnet-5-5 " in agent.auto_cmd("anthropic/claude-sonnet-5-5")
    assert _version("1.10.0") > _version("1.9.0")
    # CLIs that read a file instead of stdin get the prompt in a temp file that is removed afterwards
    state.AUTO = (f"{sys.executable} -c 'import sys, json; t = open(sys.argv[1]).read(); "
                  f"print(json.dumps({{\"text\": str(len(t) > 50)}}))' {{prompt_file}}")
    assert agent.run_auto("anthropic", {"model": "m", "messages": []}, "pf") == {"text": "True"}
    assert not list(state.DIR.glob("prompt-*")), "the prompt file must be deleted"

    # cache --purge: refuses without --yes when nobody can answer the question, then empties the folder
    junk = state.DIR / "purge-test"
    junk.mkdir()
    for n in range(3):
        store.write(junk / f"{n}.json", {"text": "x"})
    try:
        purge_cache(junk, interactive=False)  # never ask here: the installer runs this with output hidden
        raise AssertionError("purge must refuse without --yes when stdin is not a terminal")
    except SystemExit:
        pass
    assert len(list(junk.glob("*.json"))) == 3
    purge_cache(junk, yes=True)
    assert not list(junk.glob("*.json"))
    purge_cache(junk, yes=True)  # an empty cache is not an error
    assert "7 calls" in dashboard.report(state.JOURNAL) and dashboard.duration(9000) == "2h 30m"
    assert dashboard.sparkline([0, 1, 4], 3) == "·▂█" and dashboard.sparkline([], 4) == "····"
    assert dashboard.bar(0.5, 4) == "██░░" and dashboard.bar(1.0, 3) == "███"
    assert dashboard.bar(0.84, 18) == "███████████████▏░░"  # 15.12 cells: a left-eighth, never a "▁"
    chart = dashboard.area([0, 2, 4, 1], 4, 2)
    assert len(chart) == 2 and all(len(line) == 4 for line in chart) and chart[0] != chart[1]

    # --real, against a fake upstream: forward misses with the app's headers, save and replay successes,
    # keep streamed replies apart, pass errors through uncached, and never write a key to disk.
    seen = []

    class Upstream(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
            seen.append((self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if self.headers.get("x-api-key") == "bad":
                status, ctype = 401, "application/json"
                out = b'{"type":"error","error":{"type":"authentication_error","message":"invalid x-api-key"}}'
            elif body.get("stream"):
                status, ctype = 200, "text/event-stream"
                out = (b'event: content_block_delta\ndata: {"type":"content_block_delta","index":0,'
                       b'"delta":{"type":"text_delta","text":"streamed real"}}\n\n')
            else:
                status, ctype = 200, "application/json"
                out = json.dumps({"type": "message", "role": "assistant", "stop_reason": "end_turn",
                                  "content": [{"type": "text", "text": "real answer"}]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, format, *args):
            pass

    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    os.environ["BRIDGE_UPSTREAM_ANTHROPIC"] = f"http://127.0.0.1:{upstream.server_port}"
    journal_before = len(state.JOURNAL.calls())
    state.REAL, state.CACHE = True, state.DIR / "real-cache"
    srv = server.serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(body, headers):
        rq = urllib.request.Request(f"http://127.0.0.1:{srv.server_port}/v1/messages", json.dumps(body).encode(),
                                    {"Content-Type": "application/json", **headers})
        try:
            with urllib.request.urlopen(rq, timeout=20) as resp:
                return resp.status, resp.read().decode(), resp.headers.get("Content-Type")
        except urllib.error.HTTPError as e:
            return e.code, e.read().decode(), e.headers.get("Content-Type")

    key = {"x-api-key": "sk-test-not-real", "anthropic-version": "2023-06-01"}
    q = {"model": "claude-x", "max_tokens": 9, "messages": [{"role": "user", "content": "stage 3"}]}
    s, body, _ = call(q, key)
    assert s == 200 and "real answer" in body and len(seen) == 1, (s, body)
    path, headers, sent = seen[0]
    assert path == "/v1/messages" and sent == q and headers["x-api-key"] == "sk-test-not-real"
    assert headers["anthropic-version"] == "2023-06-01" and "x-mockingbird-upstream" not in headers
    s, body, _ = call(q, key)
    assert s == 200 and "real answer" in body and len(seen) == 1  # replayed: the real API was not called again
    s, body, ctype = call({**q, "stream": True}, key)
    assert "text/event-stream" in ctype and "streamed real" in body and len(seen) == 2  # stored apart from plain
    s, body, ctype = call({**q, "stream": True}, key)
    assert "streamed real" in body and len(seen) == 2
    bad = {**q, "messages": [{"role": "user", "content": "stage 4"}]}
    assert call(bad, {"x-api-key": "bad"})[0] == 401 and call(bad, {"x-api-key": "bad"})[0] == 401
    assert len(seen) == 4  # errors are never cached, so both reached the real API
    assert server.scrub("Incorrect API key provided: sk-proj-ab*****xyz.") == "Incorrect API key provided: [key]."
    assert server.scrub("invalid x-api-key") == "invalid x-api-key"  # header names are not keys
    assert not any("sk-test" in f.read_text() for f in state.CACHE.glob("*.json")), "an API key reached the cache"
    wait_for_calls(journal_before + 6)
    entries = dashboard.cache_entries(state.CACHE, state.JOURNAL.calls())
    assert len(entries) == 2 and all(e.via for e in entries), entries
    assert {e.answer for e in entries} == {"real answer", "streamed real"} and entries[0].asked == "stage 3"
    sources = [c.source for c in state.JOURNAL.calls()[journal_before:]]
    assert sources == ["LIVE", "CACHE", "LIVE", "CACHE", "ERROR", "ERROR"], sources
    assert store.request_key(q) != store.live_key(q, "https://api.anthropic.com")  # real answers never replay stand-in ones
    srv.shutdown()
    upstream.shutdown()
    del os.environ["BRIDGE_UPSTREAM_ANTHROPIC"]
    state.REAL = False

    # only the providers may receive a key; Azure's per-customer hosts come from the hook
    assert server.upstream_for("anthropic", {}) == "https://api.anthropic.com"
    assert server.upstream_for("openai", {"x-mockingbird-upstream": "https://myco.openai.azure.com"}) == \
        "https://myco.openai.azure.com"
    assert server.upstream_for("anthropic", {"x-mockingbird-upstream": "https://evil.example"}) is None

    class FakeURL:  # the slice of httpx.URL the hook touches
        def __init__(self, scheme, host, port):
            self.scheme, self.host, self.port = scheme, host, port

        def copy_with(self, scheme, host, port):
            return FakeURL(scheme, host, port)

    store.write(Path(hk.STATE), {"port": 8787, "pid": os.getpid()})
    sent = type("Req", (), {})()
    sent.url, sent.headers = FakeURL("https", "myco.openai.azure.com", None), {}
    os.environ["MOCKINGBIRD_ENABLED"] = "1"
    hk._redirect(sent)
    del os.environ["MOCKINGBIRD_ENABLED"]
    assert sent.headers["x-mockingbird-upstream"] == "https://myco.openai.azure.com" and sent.url.port == 8787
    print("selftest ok")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    cmd, args = (argv[0], argv[1:]) if argv else ("", [])
    if cmd in ("", "help", "-h", "--help"):
        print(__doc__)
        return 0
    if cmd == "serve":
        spec, plain = configure(args)
        # Dashboard in a real terminal; plain log lines when asked, or when an agent runs us in the background.
        live = not plain and sys.stdout.isatty() and sys.stdin.isatty()
        state.JOURNAL = dashboard.Journal()
        srv = server.serve()
        state.STATE.parent.mkdir(exist_ok=True)
        store.write(state.STATE, {"port": srv.server_port, "pid": os.getpid()})  # switches hooked apps to the bridge
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so `kill` also runs the cleanup below
        try:
            if live:
                state.QUIET = True
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                hooked = (os.environ.get("MOCKINGBIRD_ENABLED") == "1" and
                          str(HOOK_DIR) in os.environ.get("PYTHONPATH", "").split(os.pathsep))
                dashboard.run(state.JOURNAL, cache_dir=state.CACHE, meta={
                    "mode": ("offline replay" if state.REPLAY else "real API, repeats replayed" if state.REAL
                             else f"{agent.spec_label(spec)} answers" if spec
                             else "custom auto command" if state.AUTO else "agent mode (subagents answer)"),
                    "address": f"http://127.0.0.1:{srv.server_port}",
                    "cache": str(state.CACHE) if state.CACHE else "",
                    "hook": "on in this terminal" if hooked else "off here (env vars or per-venv hook)",
                })
            else:
                srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if live:
                srv.shutdown()
            state.STATE.unlink(missing_ok=True)  # hooked apps go back to the real API
            state.QUIET = False
            log.log("stopped: hooked apps now use the real API")
    elif cmd == "run":
        sys.exit(run_command(args))
    elif cmd == "hook" and len(args) in (1, 2) and args[0] in ("install", "uninstall", "status"):
        hook(*args)
    elif cmd == "cache" and not state.CACHE:
        sys.exit("the cache is off (BRIDGE_CACHE=off)")
    elif cmd == "cache" and ("--purge" in args or args[:1] == ["clear"]):  # "clear" kept from v1.7
        purge_cache(state.CACHE, yes="--yes" in args or "-y" in args)
    elif cmd == "cache":
        print(dashboard.cache_report(state.CACHE, dashboard.Journal().calls()))
    elif cmd == "uninstall":
        script = ROOT / "uninstall.sh"
        os.execvp("bash", ["bash", str(script), *args])
    elif cmd == "update":
        update()
    elif cmd == "stats":
        print(dashboard.report(dashboard.Journal()))
    elif cmd == "next":
        got = agent.claim(float(args[args.index("--wait") + 1]) if "--wait" in args else 30)
        print("\n".join(json.dumps(g) for g in got))
        sys.exit(0 if got else 1)
    elif cmd == "prompt":
        print(agent.prompt(args[0]))
    elif cmd == "answer":
        try:
            agent.answer(args[0], sys.stdin.read())
        except (ValueError, AssertionError) as e:
            sys.exit(f"answer rejected: {e}")
        print("accepted")
    elif cmd == "selftest":
        selftest()
    elif cmd in ("version", "--version"):
        print(state.VERSION)
    else:
        sys.exit(__doc__)
    return 0
