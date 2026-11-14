#!/usr/bin/env python3
"""Fake LLM endpoint for tests: parks each API call on disk until a coding-agent subagent answers it.

  serve [--model M[E]]  HTTP shim on 127.0.0.1 (Anthropic /v1/messages, OpenAI + Azure /chat/completions).
        [--auto CMD]    Shows a live dashboard in a terminal; --plain (or no terminal) logs lines to stderr instead.
        [--plain]       --model sonnet[high] answers each request with a lean `claude -p` call (gpt*/o*: `codex exec`).
        [--agent]       Default: sonnet[medium] when the claude CLI exists. --auto CMD runs any command instead
                        (prompt on stdin, JSON on stdout). --agent: a coding agent serves requests via next/answer.
                        Every call is logged (metadata only) to ~/.local/state/mockingbird/calls.db.
  stats                 print the analytics (all calls ever, across projects)
  update                install the newest release from GitHub (your history and saved answers stay)
  uninstall [--purge]   remove mockingbird; --purge also deletes the history and saved answers
  cache [--purge]       list the saved answers, or delete them all (asks first; --yes skips the question).
                        Env BRIDGE_CACHE picks another folder
  hook install|uninstall|status [PYTHON]
                        while serve runs, real Anthropic/OpenAI/Azure calls from Python go to the bridge; when it
                        stops, they go to the real API. No args: global (every Python started from your shell,
                        via a PYTHONPATH line in ~/.bashrc and ~/.zshrc). PYTHON: only that one environment.
  next [--wait S]       claim every pending request, print one JSON line each (exit 1 if none)
  prompt ID             print the full subagent prompt for a claimed request
  answer ID             read the subagent's JSON reply on stdin, validate it, hand it to the shim
  selftest              run the built-in check
  version               print the mockingbird version

Env: BRIDGE_DIR (./.llm-bridge), BRIDGE_PORT (8787), BRIDGE_TIMEOUT seconds (900),
     BRIDGE_AUTO (same as --auto), BRIDGE_PARALLEL (4, max auto commands at once),
     BRIDGE_CACHE (~/.local/state/mockingbird/cache, or a folder, or "off"): saved answers, replayed instantly.
Stdlib only.
"""
import hashlib, json, os, re, shlex, shutil, signal, subprocess, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # dashboard.py sits beside us, even via the ~/.local/bin link
import dashboard  # noqa: E402

JOURNAL = None  # dashboard.Journal while `serve` runs: every call, on disk, for the dashboard and `stats`
PROJECT = Path.cwd().name  # the folder the server runs in, shown per call
QUIET = False  # True while the dashboard owns the terminal

VERSION = "1.10.1"
STATE = Path.home() / ".mockingbird" / "active.json"  # exists only while `serve` runs; the hook reads it
CLAUDE_CMD = ('claude -p --model {m} --effort {e} --tools "" --system-prompt "You stand in for an LLM API. '
              'Follow the instructions in the user message exactly." --strict-mcp-config --disable-slash-commands '
              '--no-session-persistence')
# Codex: prompt on stdin ("-"). Without -m it uses the model in your Codex config (your ChatGPT plan).
# unverified: Codex was not installed where this was built.
CODEX_CMD = "codex exec{model}{effort} -"
# opencode takes the message as an argument, not stdin, so the prompt goes in a file it attaches.
# Without -m it uses the default model in your opencode config (any provider you set up there).
OPENCODE_CMD = ('opencode run --archived{model} -f {{prompt_file}} '
                '"Follow the instructions in the attached file exactly. Reply with only the JSON object it asks for."')
DIR = Path(os.environ.get("BRIDGE_DIR", ".llm-bridge")).resolve()
PORT = int(os.environ.get("BRIDGE_PORT", "8787"))
TIMEOUT = float(os.environ.get("BRIDGE_TIMEOUT", "900"))
AUTO = os.environ.get("BRIDGE_AUTO", "")
SLOTS = threading.BoundedSemaphore(int(os.environ.get("BRIDGE_PARALLEL", "4")))
_cache = os.environ.get("BRIDGE_CACHE", str(dashboard.CACHE_DIR))  # one cache for every project; "off" disables
CACHE = None if _cache.lower() == "off" else Path(_cache).resolve()
REDIRECT_VARS = ("ANTHROPIC_BASE_URL", "OPENAI_BASE_URL", "AZURE_OPENAI_ENDPOINT",
                 "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY")
STOP_TO_OPENAI = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def _dirs():
    for d in ("queue", "claimed", "answers"):
        (DIR / d).mkdir(parents=True, exist_ok=True)
    (DIR / ".gitignore").write_text("*\n")  # prompts may hold personal data: never commit them


def _write(path, obj):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(obj))
    os.replace(tmp, path)  # atomic, so readers never see half a file


# ---------- answer -> provider response ----------

def _req_key(req):
    return json.dumps({k: v for k, v in req.items() if k != "stream"}, sort_keys=True)  # streamed or not, same answer


def _ids(req, prefix, n):
    # Same request -> same tool ids. Random ids made a replayed turn 1 change turn 2, so multi-turn loops missed the cache.
    seed = hashlib.sha256(_req_key(req).encode()).hexdigest()
    return [prefix + hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:24] for i in range(n)]


def anthropic_body(ans, req, rid):
    if "raw" in ans:
        return ans["raw"]
    calls = ans.get("tool_calls") or []
    content = [{"type": "text", "text": ans["text"]}] if ans.get("text") else []
    content += [{"type": "tool_use", "id": i, "name": t["name"], "input": t.get("input", {})}
                for i, t in zip(_ids(req, "toolu_", len(calls)), calls)]
    stop = ans.get("stop") or ("tool_use" if ans.get("tool_calls") else "end_turn")
    return {"id": "msg_bridge_" + rid, "type": "message", "role": "assistant", "model": req.get("model", "bridge"),
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0}}


def openai_body(ans, req, rid):
    if "raw" in ans:
        return ans["raw"]
    msg = {"role": "assistant", "content": ans.get("text") or None}
    calls = ans.get("tool_calls") or []
    if calls:
        msg["tool_calls"] = [{"id": i, "type": "function",
                              "function": {"name": t["name"], "arguments": json.dumps(t.get("input", {}))}}
                             for i, t in zip(_ids(req, "call_", len(calls)), calls)]
    stop = ans.get("stop") or ("tool_use" if calls else "end_turn")
    return {"id": "chatcmpl-bridge-" + rid, "object": "chat.completion", "created": int(time.time()),
            "model": req.get("model", "bridge"),
            "choices": [{"index": 0, "message": msg, "finish_reason": STOP_TO_OPENAI.get(stop, stop)}],
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}}


def anthropic_sse(body):
    ev = [("message_start", {"type": "message_start", "message": {**body, "content": [], "stop_reason": None}})]
    for i, b in enumerate(body["content"]):
        if b["type"] == "text":
            start, delta = {"type": "text", "text": ""}, {"type": "text_delta", "text": b["text"]}
        elif b["type"] == "tool_use":
            start, delta = {**b, "input": {}}, {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}
        else:
            start, delta = b, None
        ev.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": start}))
        if delta:
            ev.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": delta}))
        ev.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
                                 "usage": {"output_tokens": 0}}))
    ev.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev)


def openai_sse(body):
    c = body["choices"][0]
    m = c["message"]
    delta = {"role": "assistant", "content": m.get("content")}
    if m.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **t} for i, t in enumerate(m["tool_calls"])]
    chunk = lambda d, f: {"id": body["id"], "object": "chat.completion.chunk", "created": body["created"],
                          "model": body["model"], "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
    return "".join(f"data: {json.dumps(x)}\n\n" for x in (chunk(delta, None), chunk({}, c["finish_reason"]))) + "data: [DONE]\n\n"


# ---------- HTTP shim ----------

class Shim(BaseHTTPRequestHandler):
    def _send(self, status, payload, ctype="application/json"):
        data = payload.encode() if isinstance(payload, str) else json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _error(self, api, status, msg):
        body = ({"type": "error", "error": {"type": "api_error", "message": msg}} if api == "anthropic"
                else {"error": {"message": msg, "type": "api_error", "code": status}})
        self._send(status, body)

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path.startswith("/health") else self._error("openai", 404, "not found")

    def _record(self, rid, model, api, source, t0, label, status=200):
        if JOURNAL:
            JOURNAL.record(rid, dashboard.Call(time.time(), model, api, source, time.time() - t0, status, label,
                                               PROJECT, self.key))

    def do_POST(self):
        t0 = time.time()
        self.key = ""  # the request's cache key, so the Cache tab can count replays per answer
        path = self.path.split("?")[0].rstrip("/")
        api = "anthropic" if path.endswith("/messages") else "openai" if path.endswith("/chat/completions") else None
        if not api:  # count_tokens, /responses, embeddings, other vendors
            self._error("openai", 501, f"bridge: {path} not supported; only /v1/messages and /chat/completions")
            return self._record(None, "(no model)", "unknown", "ERROR", t0, f"unsupported {path}", 501)
        try:
            req = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            self._error(api, 400, "bridge: request body is not JSON")
            return self._record(None, "(no model)", api, "ERROR", t0, "request body is not JSON", 400)
        rid = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"  # sortable, so `next` serves oldest first
        model = str(req.get("model", "(no model)"))
        self.key = request_key(req)
        if JOURNAL:
            JOURNAL.start(rid, model, api)
        log(f"{rid} received ({api}, model={model}, stream={bool(req.get('stream'))})")
        ans, source = get_answer(api, path, req, rid)
        if ans is None:
            status, msg = 504, f"bridge: no subagent answer within {TIMEOUT:.0f}s"
        elif "error" in ans:
            status, msg = int(ans.get("status", 500)), "bridge: " + str(ans["error"])
        else:
            try:
                body = (anthropic_body if api == "anthropic" else openai_body)(ans, req, rid)
                if req.get("stream"):
                    self._send(200, (anthropic_sse if api == "anthropic" else openai_sse)(body), "text/event-stream")
                else:
                    self._send(200, body)
                log(f"{rid} answered ({source}, {time.time() - t0:.1f}s)")
                return self._record(rid, model, api, source, t0, describe(ans))
            except (KeyError, TypeError, IndexError) as e:
                status, msg = 500, f"bridge: answer could not be shaped into a {api} response: {e!r}"
        self._error(api, status, msg)
        log(f"{rid} error {status}: {msg}")
        self._record(rid, model, api, "ERROR", t0, msg.removeprefix("bridge: ")[:80], status)

    def log_message(self, format, *args):
        pass


def request_key(req):
    return hashlib.sha256(_req_key(req).encode()).hexdigest()


def cache_file(req):
    if not CACHE:
        return None
    return CACHE / (request_key(req) + ".json")


def _block_text(block):
    if block.get("type") == "tool_result":
        inner = block.get("content")
        if isinstance(inner, list):
            inner = " ".join(str(b.get("text", "")) for b in inner if isinstance(b, dict))
        return f"[tool result: {inner}]"
    return block.get("text", f"[{block.get('type', 'block')}]")


def asked_text(req):
    """The last user message as plain text: what the Cache tab shows under ASKED."""
    for m in reversed(req.get("messages") or []):
        if m.get("role") != "user":
            continue
        content = m.get("content")
        if isinstance(content, list):  # content blocks: text as is, tool results with what the app sent back
            content = " ".join(_block_text(b) for b in content if isinstance(b, dict))
        return " ".join(str(content or "").split())[:500]
    return ""


def get_answer(api, path, req, rid):
    """Returns (answer or None, source): CACHE, FRESH (auto CLI) or AGENT (subagent)."""
    cf = cache_file(req)
    if cf and cf.exists():
        log(f"{rid} replayed from cache")
        data = json.loads(cf.read_text())
        return dashboard.cached_answer(data), "CACHE"
    ans = run_auto(api, req, rid) if AUTO else wait_for_agent(api, path, req, rid)
    if cf and ans is not None and "error" not in ans:  # never cache failures
        cf.parent.mkdir(parents=True, exist_ok=True)
        (cf.parent / ".gitignore").write_text("*\n")  # answers may echo test data: opt in to committing them
        _write(cf, {"meta": {"saved": time.time(), "model": str(req.get("model", "")), "api": api,
                             "project": PROJECT, "asked": asked_text(req), "label": describe(ans)},
                    "answer": ans})
    return ans, "FRESH" if AUTO else "AGENT"


def describe(ans):
    """Journal label: tool names or text length only, never the answer itself (it can echo personal data)."""
    if "raw" in ans:
        return "raw response"
    calls = [t["name"] for t in ans.get("tool_calls") or []]
    if calls:
        return "tool_use " + ", ".join(calls)
    return f"text · {len(ans.get('text', '')):,} chars"


def log(msg):
    if not QUIET:  # the dashboard owns the screen; stray stderr would tear it
        print(f"[bridge] {msg}", file=sys.stderr)


def wait_for_agent(api, path, req, rid):
    _write(DIR / "queue" / f"{rid}.json", {"id": rid, "api": api, "path": path, "request": req})
    ans_path, deadline = DIR / "answers" / f"{rid}.json", time.time() + TIMEOUT
    ans = None
    while time.time() < deadline:
        try:
            ans = json.loads(ans_path.read_text())
            break
        except (FileNotFoundError, ValueError):  # not there yet, or mid-write by a non-atomic writer
            time.sleep(0.2)
    for p in (ans_path, DIR / "queue" / f"{rid}.json", DIR / "claimed" / f"{rid}.json"):
        p.unlink(missing_ok=True)  # keep no copies of prompts once answered
    return ans


def _child_env():
    # The test shell points these at the bridge; a child CLI inheriting them would call itself forever.
    return {k: v for k, v in os.environ.items()
            if not (k in REDIRECT_VARS and ("127.0.0.1" in v or "localhost" in v or v == "bridge-dummy"))}


def run_auto(api, req, rid):
    text, note, last = render(api, req, rid, auto=True), "", ""
    with SLOTS:
        for _ in range(2):  # one retry with the rejection reason, then give the app a normal API error
            prompt_file = None
            try:
                cmd, stdin = AUTO, text + note
                if "{prompt_file}" in cmd:  # CLIs that take a file, not stdin (opencode)
                    _dirs()
                    prompt_file = DIR / f"prompt-{rid}.md"
                    prompt_file.write_text(stdin)
                    cmd, stdin = cmd.replace("{prompt_file}", shlex.quote(str(prompt_file))), ""
                out = subprocess.run(shlex.split(cmd), input=stdin, capture_output=True, text=True,
                                     timeout=TIMEOUT, env=_child_env())
            except subprocess.TimeoutExpired:
                return {"error": f"auto command gave no answer within {TIMEOUT:.0f}s", "status": 504}
            except OSError as e:
                return {"error": f"auto command could not start: {e}", "status": 500}
            finally:
                if prompt_file:
                    prompt_file.unlink(missing_ok=True)  # it holds the whole request: never leave it behind
            if out.returncode != 0:
                return {"error": f"auto command exited {out.returncode}: {out.stderr.strip()[-300:]}", "status": 502}
            try:
                return parse_answer(out.stdout)
            except (ValueError, AssertionError) as e:
                last = str(e)
                note = f"\n\nYour previous reply was rejected ({last}). Reply again with only the JSON object."
        return {"error": f"auto answer rejected twice: {last}", "status": 500}


def serve(port=PORT):
    _dirs()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Shim)  # loopback only
    mode = f"auto: {AUTO}" if AUTO else "agent serves via next/answer"
    log(f"v{VERSION} listening on http://127.0.0.1:{srv.server_port}  mode={mode}  cache={CACHE or 'off'}")
    return srv


# ---------- agent side ----------

def claim(wait=30.0):
    _dirs()
    deadline = time.time() + wait
    while True:
        got = []
        for f in sorted((DIR / "queue").glob("*.json")):
            try:
                os.rename(f, DIR / "claimed" / f.name)  # atomic claim; loser of a race just skips it
            except FileNotFoundError:
                continue
            r = json.loads((DIR / "claimed" / f.name).read_text())
            req = r["request"]
            got.append({"id": r["id"], "api": r["api"], "model": req.get("model"), "stream": bool(req.get("stream")),
                        "messages": len(req.get("messages", [])), "tools": len(req.get("tools", []))})
        if got or time.time() >= deadline:
            return got
        time.sleep(0.3)


PROMPT = """You are standing in for an LLM API during an automated test. An application sent the request below to the {api} API, addressed to model "{model}". Produce the reply that model would give. Nothing you say reaches a human; only the application reads it.

Rules:
- The system prompt (`system` field or system/developer-role messages) is your instruction set. Answer the final turn using the whole conversation, prior tool results included.
- If the request defines tools/functions and calling one is the right next step, return it in "tool_calls". Never run the tools yourself; the application runs them.
- If the request demands JSON (response_format, a schema, or the prompt says so), "text" must be a string that parses as that JSON.
- Respect max_tokens roughly; stop sequences end the text.
- Never mention this test, the bridge, or that you are a stand-in.

{submit}
Shape: {{"text": "<reply, or empty>", "tool_calls": [{{"name": "<tool>", "input": {{}}}}], "stop": "end_turn" | "tool_use" | "max_tokens"}}
If you truly cannot answer: {{"error": "<why>", "status": 500}}

Request JSON:
{request}
"""
SUBMIT_AGENT = """When done, submit exactly one JSON object (no prose, no code fences) by piping it to:
  python3 {script} answer {rid}
If `answer` exits non-zero, read its message, fix the JSON, and submit again."""
SUBMIT_AUTO = "Reply with exactly one JSON object and nothing else: no prose, no code fences."


def render(api, req, rid, auto=False):
    submit = SUBMIT_AUTO if auto else SUBMIT_AGENT.format(script=Path(__file__).resolve(), rid=rid)
    return PROMPT.format(api=api, model=req.get("model"), submit=submit, request=json.dumps(req, indent=1))


def prompt(rid):
    r = json.loads((DIR / "claimed" / f"{rid}.json").read_text())
    return render(r["api"], r["request"], rid)


def parse_answer(text):
    m = re.search(r"\{.*\}", text, re.S)  # models sometimes wrap the JSON in prose or code fences
    ans = json.loads(m.group(0) if m else text)  # raises with a readable message on bad JSON
    assert isinstance(ans, dict), "answer must be a JSON object"
    if "error" not in ans and "raw" not in ans:
        assert isinstance(ans.get("text", ""), str), '"text" must be a string'
        for t in ans.get("tool_calls") or []:
            assert isinstance(t.get("name"), str) and isinstance(t.get("input", {}), dict), \
                'each tool_call needs "name" (string) and "input" (object)'
        assert ans.get("text") or ans.get("tool_calls"), 'need "text" or "tool_calls"'
    return ans


def answer(rid, text):
    _write(DIR / "answers" / f"{rid}.json", parse_answer(text))


# ---------- convenience ----------

REPO = "19Naveen/MockingBird"


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
    """Read the version on main, and if it's newer, run that release's installer."""
    import tempfile, urllib.request
    url = f"https://raw.githubusercontent.com/{REPO}/main/install.sh"
    try:
        script = urllib.request.urlopen(url, timeout=30).read().decode()
    except OSError as e:
        sys.exit(f"could not reach GitHub ({e}). Check your connection and try again.")
    found = re.search(r"^VERSION=(\S+)", script, re.M)
    latest = found.group(1) if found else "?"
    if found and _version(latest) <= _version(VERSION):
        print(f"mockingbird {VERSION} is up to date (newest release: {latest}).")
        return
    print(f"updating mockingbird {VERSION} -> {latest}")
    with tempfile.NamedTemporaryFile("w", suffix="-install.sh", delete=False) as f:
        f.write(script)
    try:
        code = subprocess.run(["bash", f.name]).returncode
    finally:
        os.unlink(f.name)
    if code == 0 and STATE.exists():
        print("A server is running on the old version. Press q on its dashboard and start it again.")
    sys.exit(code)


def _parse_spec(spec):
    m = re.fullmatch(r"([\w.\-/:#]+)(?:\[(\w+)\])?", spec)
    if not m:
        sys.exit(f"--model must look like sonnet[high], codex, gpt-5[low] or opencode/<provider>/<model>; got {spec!r}")
    return m.group(1), m.group(2)


def auto_cmd(spec):
    """Map a --model spec to the CLI that answers.
    sonnet / opus / haiku / claude-* -> claude (your Claude plan); codex, gpt-*, o<n>-* -> codex exec (your ChatGPT
    plan); opencode or provider/model -> opencode run (whatever you set up in opencode)."""
    model, effort = _parse_spec(spec)
    if model == "codex" or re.match(r"(gpt|o\d|codex)", model):
        return CODEX_CMD.format(model="" if model == "codex" else f" -m {shlex.quote(model)}",
                                effort=f" -c model_reasoning_effort={shlex.quote(effort)}" if effort else "")
    if model == "opencode" or "/" in model:
        model = model.removeprefix("opencode/")
        return OPENCODE_CMD.format(model="" if model == "opencode" else f" -m {shlex.quote(model)}")
    return CLAUDE_CMD.format(m=shlex.quote(model), e=shlex.quote(effort or "medium"))


def spec_label(spec):
    model, effort = _parse_spec(spec)
    if model in ("codex", "opencode"):
        return f"{model} (its default model)"
    if model == "codex" or re.match(r"(gpt|o\d|codex)", model) or "/" in model:
        return spec
    return f"{model}[{effort or 'medium'}]"


def default_spec():
    """No --model given: use whichever plan this machine has, Claude first, then Codex, then opencode."""
    for cli, spec in (("claude", "sonnet"), ("codex", "codex"), ("opencode", "opencode")):
        if shutil.which(cli):
            return spec
    return None


HOOK_DIR = Path.home() / ".mockingbird" / "hook"
RC_FILES = (".bashrc", ".zshrc")
RC_BLOCK = ('# >>> mockingbird >>>\n'
            'export PYTHONPATH="$HOME/.mockingbird/hook${PYTHONPATH:+:$PYTHONPATH}"\n'
            '# <<< mockingbird <<<\n')
SITECUSTOMIZE = '''\
# mockingbird: loads the LLM-call hook in every Python started from your shell (via PYTHONPATH).
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
    if spec and spec.loader:
        spec.loader.exec_module(importlib.util.module_from_spec(spec))


_chain()
del _chain
'''


def hook_global(action):
    rcs = [Path.home() / f for f in RC_FILES if (Path.home() / f).exists()]
    block = re.compile(r"\n?# >>> mockingbird >>>.*?# <<< mockingbird <<<\n", re.S)
    if action == "install":
        HOOK_DIR.mkdir(parents=True, exist_ok=True)
        (HOOK_DIR / "mockingbird_hook.py").write_text(Path(__file__).resolve().with_name("mockingbird_hook.py").read_text())
        (HOOK_DIR / "sitecustomize.py").write_text(SITECUSTOMIZE)
        for rc in rcs:
            if "# >>> mockingbird >>>" not in rc.read_text():
                with rc.open("a") as f:
                    f.write("\n" + RC_BLOCK)
        print(f"global hook installed: {HOOK_DIR}\n  PYTHONPATH line added to: {', '.join(r.name for r in rcs) or 'none found'}"
              "\n  open a new terminal; then every Python you start from it routes LLM calls to mockingbird"
              "\n  while the server runs, and to the real API when it doesn't")
    elif action == "uninstall":
        for rc in rcs:
            rc.write_text(block.sub("", rc.read_text()))  # install added "\n" + block, so this restores the file exactly
        shutil.rmtree(HOOK_DIR, ignore_errors=True)
        print("global hook removed; open a new terminal")
    else:
        on_path = str(HOOK_DIR) in os.environ.get("PYTHONPATH", "").split(os.pathsep)
        print(f"global hook: {'installed' if (HOOK_DIR / 'sitecustomize.py').exists() else 'not installed'}"
              f"{', active in this shell' if on_path else ', NOT active in this shell (open a new terminal)'}")
        print(f"bridge: {'running ' + STATE.read_text() if STATE.exists() else 'stopped (real API)'}")


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
        mod.write_text(Path(__file__).resolve().with_name("mockingbird_hook.py").read_text())
        pth.write_text("import mockingbird_hook\n")
        print(f"hook installed in {site}\n  bridge running -> this environment's LLM calls go to mockingbird\n"
              f"  bridge stopped -> real API\n  never install it in a production environment")
    elif action == "uninstall":
        pth.unlink(missing_ok=True)
        mod.unlink(missing_ok=True)
        print(f"hook removed from {site}")
    else:
        print(f"hook: {'installed' if pth.exists() else 'not installed'} in {site}")
        print(f"bridge: {'running ' + STATE.read_text() if STATE.exists() else 'stopped (real API)'}")


# ---------- self-check ----------

def selftest():
    global DIR, AUTO, CACHE, JOURNAL, QUIET
    import tempfile, urllib.request, urllib.error
    DIR, AUTO, CACHE, QUIET = Path(tempfile.mkdtemp()), "", None, True
    JOURNAL = dashboard.Journal(DIR / "calls.db")  # never the real journal
    srv = serve(0)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{srv.server_port}"
    replies = iter([
        '```json\n{"tool_calls": [{"name": "get_weather", "input": {"city": "Singapore"}}]}\n```',
        '{"text": "hello"}',
        '{"error": "overloaded", "status": 529}',
    ])

    def agent():
        for _ in range(3):
            (r,) = claim(10)
            assert "Request JSON" in prompt(r["id"])
            answer(r["id"], next(replies))
    threading.Thread(target=agent, daemon=True).start()

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
    assert anthropic_body(tc, {"m": 1}, "a")["content"][0]["id"] == anthropic_body(tc, {"m": 1}, "b")["content"][0]["id"]
    s, b = post("/openai/deployments/d/chat/completions?api-version=x", {"model": "gpt", "stream": True, "messages": []})
    assert s == 200 and '"content": "hello"' in b and '"finish_reason": "stop"' in b and b.endswith("data: [DONE]\n\n"), b
    s, b = post("/v1/messages", {"model": "claude-x", "messages": []})
    assert s == 529 and "overloaded" in b, (s, b)
    assert post("/v1/embeddings", {})[0] == 501
    assert not list(DIR.glob("*/*.json")), "request/answer files must be deleted after replying"

    # auto mode: fake CLI that wraps its JSON in prose and reports whether it inherited the redirect
    fake = ('import os,sys,json; sys.stdin.read(); '
            'print("Sure! " + json.dumps({"text": "auto:" + str("ANTHROPIC_BASE_URL" in os.environ)}))')
    AUTO, CACHE = f"{sys.executable} -c {shlex.quote(fake)}", DIR / "cache"
    os.environ["ANTHROPIC_BASE_URL"] = url
    hi = {"model": "claude-x", "messages": [{"role": "user", "content": "hi"}]}
    s, b = post("/v1/messages", hi)
    assert s == 200 and json.loads(b)["content"][0]["text"] == "auto:False", (s, b)  # loop guard held
    AUTO = f"{sys.executable} -c 'raise SystemExit(3)'"  # cache must answer without running the command
    s, b = post("/v1/messages", {**hi, "stream": True})
    assert s == 200 and "auto:False" in b and "message_stop" in b, (s, b)
    s, b = post("/v1/messages", {**hi, "max_tokens": 5})  # cache miss -> failing command -> API error, not cached
    assert s == 502 and "exited 3" in b and len(list(CACHE.glob("*.json"))) == 1, (s, b)

    # cache tab: entries carry what was asked and how often they were replayed; v1.6 files still replay
    entries = dashboard.cache_entries(CACHE, JOURNAL.calls())
    assert len(entries) == 1 and entries[0].asked == "hi" and entries[0].used == 1, entries
    assert entries[0].answer == "auto:False" and not entries[0].legacy, entries
    legacy = CACHE / ("0" * 64 + ".json")
    _write(legacy, {"text": "old"})
    assert dashboard.cached_answer(json.loads(legacy.read_text())) == {"text": "old"}
    assert any(e.legacy and e.answer == "old" for e in dashboard.cache_entries(CACHE, []))
    legacy.unlink()
    assert "1 saved answers" in dashboard.cache_report(CACHE, [])
    blocks = {"messages": [{"role": "user", "content": [{"type": "text", "text": "a"}, {"type": "image"}]}]}
    assert asked_text(blocks) == "a [image]" and asked_text({"messages": []}) == ""
    result = {"messages": [{"role": "user", "content": [{"type": "tool_result", "content": "shipped"}]}]}
    assert asked_text(result) == "[tool result: shipped]", asked_text(result)
    import sqlite3  # a v1.6 journal (no key column) upgrades in place
    old = sqlite3.connect(DIR / "old.db")
    old.execute("CREATE TABLE calls (id INTEGER PRIMARY KEY, at REAL NOT NULL, model TEXT, api TEXT, source TEXT,"
                " seconds REAL, status INTEGER, label TEXT, project TEXT)")
    old.execute("INSERT INTO calls (at, model, api, source, seconds, status, label, project)"
                " VALUES (1, 'm', 'a', 'CACHE', 0, 200, 'l', 'p')")
    old.commit()
    old.close()
    upgraded = dashboard.Journal(DIR / "old.db")
    assert upgraded.calls()[0].key == ""
    upgraded.record(None, dashboard.Call(2, "m", "a", "CACHE", 0, 200, "l", "p", "k"))
    assert upgraded.calls()[-1].key == "k"
    srv.shutdown()

    # hook on/off switch: live bridge -> its port; stale file from a dead bridge or no file -> real API
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import mockingbird_hook as hk
    hk.STATE = str(DIR / "active.json")
    assert hk.bridge_port() is None
    _write(Path(hk.STATE), {"port": 9999, "pid": os.getpid()})
    assert hk.bridge_port() == 9999
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    _write(Path(hk.STATE), {"port": 9999, "pid": dead.pid})
    assert hk.bridge_port() is None

    # dashboard: every call above was recorded with the right source, and the screen data builds from it
    calls, inflight = JOURNAL.calls(), JOURNAL.inflight()
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
    assert auto_cmd("sonnet").startswith("claude -p --model sonnet --effort medium")
    assert auto_cmd("codex") == "codex exec -" and auto_cmd("gpt-5[low]") == ("codex exec -m gpt-5 "
                                                                              "-c model_reasoning_effort=low -")
    assert auto_cmd("opencode").startswith("opencode run --archived -f {prompt_file}")
    assert " -m anthropic/claude-sonnet-5-5 " in auto_cmd("anthropic/claude-sonnet-5-5")
    assert _version("1.10.0") > _version("1.9.0")
    # CLIs that read a file instead of stdin get the prompt in a temp file that is removed afterwards
    AUTO = f"{sys.executable} -c 'import sys, json; t = open(sys.argv[1]).read(); print(json.dumps({{\"text\": str(len(t) > 50)}}))' {{prompt_file}}"
    assert run_auto("anthropic", {"model": "m", "messages": []}, "pf") == {"text": "True"}
    assert not list(DIR.glob("prompt-*")), "the prompt file must be deleted"
    # cache --purge: refuses without --yes when nobody can answer the question, then empties the folder
    junk = DIR / "purge-test"
    junk.mkdir()
    for n in range(3):
        _write(junk / f"{n}.json", {"text": "x"})
    try:
        purge_cache(junk, interactive=False)  # never ask here: the installer runs this with output hidden
        raise AssertionError("purge must refuse without --yes when stdin is not a terminal")
    except SystemExit:
        pass
    assert len(list(junk.glob("*.json"))) == 3
    purge_cache(junk, yes=True)
    assert not list(junk.glob("*.json"))
    purge_cache(junk, yes=True)  # an empty cache is not an error
    assert "7 calls" in dashboard.report(JOURNAL) and dashboard.duration(9000) == "2h 30m"
    assert dashboard.sparkline([0, 1, 4], 3) == "·▂█" and dashboard.sparkline([], 4) == "····"
    assert dashboard.bar(0.5, 4) == "██░░" and dashboard.bar(1.0, 3) == "███"
    assert dashboard.bar(0.84, 18) == "███████████████▏░░"  # 15.12 cells: a left-eighth, never a "▁"
    chart = dashboard.area([0, 2, 4, 1], 4, 2)
    assert len(chart) == 2 and all(len(line) == 4 for line in chart) and chart[0] != chart[1]
    print("selftest ok")


if __name__ == "__main__":
    cmd, args = (sys.argv[1], sys.argv[2:]) if len(sys.argv) > 1 else ("", [])
    if cmd in ("", "help", "-h", "--help") or "-h" in args or "--help" in args:
        print(__doc__)
        sys.exit(0)
    if cmd == "serve":
        if "--auto" in args:
            AUTO = args[args.index("--auto") + 1]
        spec = args[args.index("--model") + 1] if "--model" in args else None
        if not spec and not AUTO and "--agent" not in args:
            spec = default_spec()
            if not spec:
                log("no claude, codex or opencode CLI found: agent mode (an agent answers via next/answer)")
        if spec:
            AUTO = auto_cmd(spec)
        # Dashboard in a real terminal; plain log lines when asked, or when an agent runs us in the background.
        live = "--plain" not in args and sys.stdout.isatty() and sys.stdin.isatty()
        JOURNAL = dashboard.Journal()
        srv = serve()
        STATE.parent.mkdir(exist_ok=True)
        _write(STATE, {"port": srv.server_port, "pid": os.getpid()})  # switches hooked apps to the bridge
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # so `kill` also runs the cleanup below
        try:
            if live:
                QUIET = True
                threading.Thread(target=srv.serve_forever, daemon=True).start()
                hooked = str(HOOK_DIR) in os.environ.get("PYTHONPATH", "").split(os.pathsep)
                dashboard.run(JOURNAL, cache_dir=CACHE, meta={
                    "mode": (f"{spec_label(spec)} answers" if spec
                             else "custom auto command" if AUTO else "agent mode (subagents answer)"),
                    "address": f"http://127.0.0.1:{srv.server_port}",
                    "cache": str(CACHE) if CACHE else "",
                    "hook": "on in this terminal" if hooked else "off here (env vars or per-venv hook)",
                })
            else:
                srv.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            if live:
                srv.shutdown()
            STATE.unlink(missing_ok=True)  # hooked apps go back to the real API
            QUIET = False
            log("stopped: hooked apps now use the real API")
    elif cmd == "hook" and len(args) in (1, 2) and args[0] in ("install", "uninstall", "status"):
        hook(*args)
    elif cmd == "cache" and not CACHE:
        sys.exit("the cache is off (BRIDGE_CACHE=off)")
    elif cmd == "cache" and ("--purge" in args or args[:1] == ["clear"]):  # "clear" kept from v1.7
        purge_cache(CACHE, yes="--yes" in args or "-y" in args)
    elif cmd == "cache":
        print(dashboard.cache_report(CACHE, dashboard.Journal().calls()))
    elif cmd == "uninstall":
        script = Path(__file__).resolve().with_name("uninstall.sh")
        os.execvp("bash", ["bash", str(script), *args])
    elif cmd == "update":
        update()
    elif cmd == "stats":
        print(dashboard.report(dashboard.Journal()))
    elif cmd == "next":
        got = claim(float(args[args.index("--wait") + 1]) if "--wait" in args else 30)
        print("\n".join(json.dumps(g) for g in got))
        sys.exit(0 if got else 1)
    elif cmd == "prompt":
        print(prompt(args[0]))
    elif cmd == "answer":
        try:
            answer(args[0], sys.stdin.read())
        except (ValueError, AssertionError) as e:
            sys.exit(f"answer rejected: {e}")
        print("accepted")
    elif cmd == "selftest":
        selftest()
    elif cmd in ("version", "--version"):
        print(VERSION)
    else:
        sys.exit(__doc__)
