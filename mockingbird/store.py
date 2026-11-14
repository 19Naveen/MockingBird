"""Cache keys, atomic JSON files and the small text helpers used by the server."""

import hashlib
import json
import os
import threading
import uuid

from . import state

# shortcut: fixed lock stripes may serialize unrelated keys; use per-key locks if throughput needs it.
CACHE_LOCKS = [threading.Lock() for _ in range(64)]


def dirs():
    for d in ("queue", "claimed", "answers"):
        (state.DIR / d).mkdir(parents=True, exist_ok=True)
    (state.DIR / ".gitignore").write_text("*\n")  # prompts may hold personal data: never commit them


def write(path, obj):
    tmp = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with open(tmp, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as f:
            json.dump(obj, f)
        os.replace(tmp, path)  # atomic, so readers never see half a file
    finally:
        tmp.unlink(missing_ok=True)


def cache_lock(key):
    return CACHE_LOCKS[int(key[:8], 16) % len(CACHE_LOCKS)]


def req_key(req):
    return json.dumps({k: v for k, v in req.items() if k != "stream"}, sort_keys=True)  # streamed or not, same answer


def request_key(req, api=""):
    identity = json.dumps([api, state.AUTO or "agent", state.NAMESPACE, req_key(req)])
    return hashlib.sha256(identity.encode()).hexdigest()


def live_key(req, base, path=""):
    # Real answers never share keys with stand-in answers, and streamed and plain replies are stored apart.
    salt = f"|live|{base}|{path}|{state.NAMESPACE}|stream={bool(req.get('stream'))}"
    return hashlib.sha256((req_key(req) + salt).encode()).hexdigest()


def cache_file(req, api=""):
    if not state.CACHE or state.REDACT:
        return None
    return state.CACHE / (request_key(req, api) + ".json")


def block_text(block):
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
            content = " ".join(block_text(b) for b in content if isinstance(b, dict))
        return " ".join(str(content or "").split())[:500]
    return ""


def describe(ans):
    """Journal label: tool names or text length only, never the answer itself (it can echo personal data)."""
    if "raw" in ans:
        return "raw response"
    calls = [t["name"] for t in ans.get("tool_calls") or []]
    if calls:
        return "tool_use " + ", ".join(calls)
    return f"text · {len(ans.get('text', '')):,} chars"
