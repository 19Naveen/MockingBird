"""HTTP shim: identify the provider, consult the answer cache, and shape the reply."""

import json
import os
import re
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from ..core import answers, log, state, store
from ..media import attachments
from ..ui import dashboard
from .providers import PROVIDERS
from .responses import anthropic_body, anthropic_sse, openai_body, openai_sse

UPSTREAM_DEFAULT = {"anthropic": "https://api.anthropic.com", "openai": "https://api.openai.com"}
# Keys are forwarded only to the providers themselves (or to an upstream you name explicitly).
ALLOWED_UPSTREAM = re.compile(r"https://(api\.anthropic\.com|api\.openai\.com|(?:[A-Za-z0-9-]+\.)+(?:openai|services\.ai|models\.ai|inference\.ai)\.azure\.com)"
                              r"(:\d+)?")
# Hop-by-hop and local-only headers; everything else (x-api-key, authorization, anthropic-version...) is forwarded.
DROP_HEADERS = {"host", "content-length", "connection", "keep-alive", "transfer-encoding", "te", "upgrade",
                "proxy-connection", "accept-encoding", "x-mockingbird-upstream"}
KEYLIKE = re.compile(r"\b(sk|rk|pk|key|api[-_]?key)[-_][A-Za-z0-9*_-]{4,}", re.I)


def upstream_for(api, headers):
    """Where to forward: the env override, else the host the hook says the app was calling, else the provider."""
    override = os.environ.get(f"BRIDGE_UPSTREAM_{api.upper()}")
    if override:
        return override.rstrip("/")
    hinted = (headers.get("x-mockingbird-upstream") or "").rstrip("/")
    if hinted:
        return hinted if ALLOWED_UPSTREAM.fullmatch(hinted) else None
    return UPSTREAM_DEFAULT[api]


def forward(base, path, raw, headers):
    """POST to the real API with the app's headers. Returns (status, content type, body bytes)."""
    import urllib.request, urllib.error
    sent = {k: v for k, v in headers.items() if k.lower() not in DROP_HEADERS}
    request = urllib.request.Request(base + path, data=raw, headers=sent, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=state.TIMEOUT) as r:
            return r.status, r.headers.get("Content-Type", "application/json"), r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "application/json"), e.read()
    except OSError as e:
        msg = f"mockingbird could not reach {base}: {e}"
        return 502, "application/json", json.dumps({"error": {"type": "api_error", "message": msg}}).encode()


def scrub(text):
    """Providers echo part of a bad key in their errors ("sk-proj-ab*****xyz"): never let that reach the journal."""
    return KEYLIKE.sub("[key]", text)


def http_label(ctype, body):
    """Journal label for a real answer: tool names or text length, never the text itself."""
    text = dashboard.http_answer_text(ctype, body)
    calls = re.findall(r"→ calls ([\w.-]+)\(", text)
    return "tool_use " + ", ".join(calls) if calls else f"text · {len(text):,} chars"


class Shim(BaseHTTPRequestHandler):
    def _send(self, status, payload, ctype="application/json", headers=None):
        data = (payload if isinstance(payload, bytes) else payload.encode() if isinstance(payload, str)
                else json.dumps(payload).encode())
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass  # a timeout test may deliberately abandon a delayed response

    def _error(self, api, status, msg):
        if api in PROVIDERS:
            body = PROVIDERS[api].error(status, msg)
            headers = {"x-amzn-errortype": body["__type"]} if api == "bedrock" else None
            return self._send(status, body, headers=headers)
        body = ({"type": "error", "error": {"type": "api_error", "message": msg}} if api == "anthropic"
                else {"error": {"message": msg, "type": "api_error", "code": status}})
        self._send(status, body)

    def do_GET(self):
        self._send(200, {"ok": True}) if self.path.startswith("/health") else self._error("openai", 404, "not found")

    def _record(self, rid, model, api, source, t0, label, status=200):
        if state.JOURNAL:
            if state.REDACT:
                model, label = "[redacted]", "[redacted]"
            state.JOURNAL.record(rid, dashboard.Call(time.time(), model, api, source, time.time() - t0, status, label,
                                                     "[redacted]" if state.REDACT else state.PROJECT, "" if state.REDACT else self.key))

    def _real(self, api, raw, req, rid, model, t0):
        """--real: replay an identical earlier call, otherwise forward it to the real API with the app's own
        headers (its API key) and save a successful answer. Headers are never stored or logged."""
        base = upstream_for(api, self.headers)
        if not base:
            self._error(api, 400, "bridge: --real only forwards to api.anthropic.com, api.openai.com or "
                                  "*.openai.azure.com, or to BRIDGE_UPSTREAM_ANTHROPIC / BRIDGE_UPSTREAM_OPENAI")
            return self._record(rid, model, api, "ERROR", t0, "upstream not allowed", 400)
        self.key = store.live_key(req, base, self.path)
        with store.cache_lock(self.key):
            return self._real_cached(api, raw, req, rid, model, t0, base)

    def _real_cached(self, api, raw, req, rid, model, t0, base):
        cf = state.CACHE / f"{self.key}.json" if state.CACHE and not state.REDACT else None
        if cf and cf.exists():
            http = json.loads(cf.read_text())["http"]
            self._send(http["status"], http["body"].encode(), http["content_type"])
            log.log(f"{rid} replayed from cache (real API answer)")
            return self._record(rid, model, api, "CACHE", t0, http_label(http["content_type"], http["body"]))
        if state.REPLAY:
            self._error(api, 409, "bridge: offline replay cache miss")
            return self._record(rid, model, api, "ERROR", t0, "offline replay cache miss", 409)
        status, ctype, body = forward(base, self.path, raw, self.headers)
        self._send(status, body, ctype)
        text = body.decode("utf-8", "replace")
        if status != 200:  # passed through as is, never cached: the app's own retry and error handling run
            log.log(f"{rid} real API answered {status}")
            message = text
            try:
                err = json.loads(text).get("error")
                message = err.get("message", text) if isinstance(err, dict) else str(err or text)
            except (ValueError, AttributeError):
                pass
            return self._record(rid, model, api, "ERROR", t0, scrub(" ".join(message.split()))[:80], status)
        if cf:
            cf.parent.mkdir(parents=True, exist_ok=True)
            (cf.parent / ".gitignore").write_text("*\n")
            store.write(cf, {"meta": {"saved": time.time(), "model": model, "api": api, "project": state.PROJECT,
                                      "asked": store.asked_text(req), "label": http_label(ctype, text), "via": base},
                             "http": {"status": status, "content_type": ctype, "body": text}})
        log.log(f"{rid} answered by the real API ({time.time() - t0:.1f}s)")
        self._record(rid, model, api, "LIVE", t0, http_label(ctype, text))

    def do_POST(self):
        with state.MODE_LOCK:
            state.ACTIVE_REQUESTS += 1
        try:
            self._handle_post()
        finally:
            with state.MODE_LOCK:
                state.ACTIVE_REQUESTS -= 1

    def _handle_post(self):
        t0 = time.time()
        self.key = ""  # the request's cache key, so the Cache tab can count replays per answer
        path = self.path.split("?")[0].rstrip("/")
        api = "anthropic" if path.endswith("/messages") else "openai" if path.endswith("/chat/completions") else None
        api = api or next((name for name, adapter in PROVIDERS.items() if adapter.matches(path)), None)
        if not api:  # count_tokens, /responses, embeddings, other vendors
            self._error("openai", 501, f"bridge: {path} not supported; use Messages, Chat Completions, Bedrock Converse or Gemini generateContent")
            return self._record(None, "(no model)", "unknown", "ERROR", t0, f"unsupported {path}", 501)
        try:
            size = int(self.headers.get("Content-Length", 0))
            if size < 0:
                raise ValueError("negative length")
        except ValueError:
            return self._error(api, 400, "bridge: invalid Content-Length")
        if size > 32 * 1024 * 1024:
            return self._error(api, 413, "bridge: request body exceeds 32 MiB")
        raw = self.rfile.read(size)
        try:
            req = json.loads(raw or b"{}")
            if not isinstance(req, dict):
                raise ValueError("expected an object")
        except ValueError:
            self._error(api, 400, "bridge: request body is not JSON")
            return self._record(None, "(no model)", api, "ERROR", t0, "request body is not JSON", 400)
        if api in PROVIDERS:
            if state.REAL:
                return self._error(api, 501, f"bridge: --real forwarding is not supported for native {api}; use stand-in or offline replay mode")
            try:
                req = PROVIDERS[api].normalize(self.path, req)
            except attachments.InputError as e:
                return self._error(api, e.status, "bridge: " + str(e))
            except (ValueError, TypeError, KeyError) as e:
                return self._error(api, 400, f"bridge: invalid {api} request: {e}")
        rid = f"{time.time_ns()}-{uuid.uuid4().hex[:6]}"  # sortable, so `next` serves oldest first
        model = str(req.get("model", "(no model)"))
        self.key = store.request_key(req, api)
        if state.JOURNAL:
            state.JOURNAL.start(rid, "[redacted]" if state.REDACT else model, api)
        log.log(f"{rid} received ({api}, model={model}, stream={bool(req.get('stream'))})")
        with state.FAULT_LOCK:
            fault = state.FAULTS.pop(0) if state.FAULTS else {}
        if fault.get("delay"):
            time.sleep(fault["delay"])
        if "body" in fault:
            self._send(fault.get("status", 200), fault["body"])
            return self._record(rid, model, api, "ERROR", t0, "scripted raw response", fault.get("status", 200))
        if "status" in fault or "error" in fault:
            status = fault.get("status", 500)
            self._error(api, status, fault.get("error", "scripted failure"))
            return self._record(rid, model, api, "ERROR", t0, "scripted failure", status)
        if state.REAL:
            return self._real(api, raw, req, rid, model, t0)
        ans, source = answers.get_answer(api, path, req, rid)
        if ans is None:
            status, msg = 504, f"bridge: no subagent answer within {state.TIMEOUT:.0f}s"
        elif "error" in ans:
            status, msg = int(ans.get("status", 500)), "bridge: " + str(ans["error"])
        else:
            try:
                adapter = PROVIDERS.get(api)
                body = adapter.response(ans, req, rid) if adapter else (anthropic_body if api == "anthropic" else openai_body)(ans, req, rid)
                if req.get("stream"):
                    if adapter:
                        stream = adapter.stream(body)
                    elif api == "anthropic":
                        stream = anthropic_sse(body)
                    else:
                        stream = openai_sse(body, req)
                    self._send(200, stream, adapter.CONTENT_TYPE if adapter else "text/event-stream")
                else:
                    self._send(200, body)
                log.log(f"{rid} answered ({source}, {time.time() - t0:.1f}s)")
                return self._record(rid, model, api, source, t0, store.describe(ans))
            except (KeyError, TypeError, IndexError, attachments.InputError) as e:
                status, msg = 500, f"bridge: answer could not be shaped into a {api} response: {e!r}"
        self._error(api, status, msg)
        log.log(f"{rid} error {status}: {msg}")
        self._record(rid, model, api, "ERROR", t0, msg.removeprefix("bridge: ")[:80], status)

    def log_message(self, format, *args):
        pass


def serve(port=None):
    port = state.PORT if port is None else port
    if state.REPLAY and (not state.CACHE or state.REDACT):
        raise ValueError("offline replay requires an enabled cache and cannot use --redact")
    store.dirs()
    srv = ThreadingHTTPServer(("127.0.0.1", port), Shim)  # loopback only
    state.BRIDGE_PORTS.add(srv.server_port)
    mode = ("offline replay" if state.REPLAY else "real API (repeats replayed)" if state.REAL
            else f"auto: {state.AUTO}" if state.AUTO else "agent serves via next/answer")
    log.log(f"v{state.VERSION} listening on http://127.0.0.1:{srv.server_port}  mode={mode}  cache={state.CACHE or 'off'}")
    return srv
