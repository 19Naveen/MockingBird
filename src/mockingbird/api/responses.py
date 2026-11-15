"""Anthropic and OpenAI response bodies built from a stand-in answer."""

import hashlib
import json
import time

from ..core.store import req_key

STOP_TO_OPENAI = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def _ids(req, prefix, n):
    # Same request -> same tool ids. Random ids made a replayed turn 1 change turn 2, so multi-turn loops missed the cache.
    seed = hashlib.sha256(req_key(req).encode()).hexdigest()
    return [prefix + hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:24] for i in range(n)]


def _usage(ans):
    usage = ans.get("usage")
    if not isinstance(usage, dict):
        return {}
    return usage


def _counts(usage):
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = int(usage.get("cached_tokens") or 0)
    cache_write = int(usage.get("cache_write_tokens") or 0)
    cost = usage.get("cost_usd")
    return prompt, completion, cached, cache_write, cost if isinstance(cost, (int, float)) else None


def usage_counts(ans):
    return _counts(_usage(ans))


def anthropic_usage(ans):
    prompt, completion, cached, cache_write, cost = _counts(_usage(ans))
    usage = {"input_tokens": max(0, prompt - cached - cache_write), "output_tokens": completion}
    if cache_write:
        usage["cache_creation_input_tokens"] = cache_write
    if cached:
        usage["cache_read_input_tokens"] = cached
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


def openai_usage(ans):
    prompt, completion, cached, _cache_write, cost = _counts(_usage(ans))
    usage = {"prompt_tokens": prompt, "completion_tokens": completion, "total_tokens": prompt + completion}
    if cached:
        usage["prompt_tokens_details"] = {"cached_tokens": cached}
    if cost is not None:
        usage["cost_usd"] = cost
    return usage


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
            "usage": anthropic_usage(ans)}


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
            "usage": openai_usage(ans)}


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
    delta_usage = {k: body["usage"][k] for k in ("output_tokens", "cost_usd") if k in body["usage"]}
    ev.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": body["stop_reason"], "stop_sequence": None},
                                 "usage": delta_usage}))
    ev.append(("message_stop", {"type": "message_stop"}))
    return "".join(f"event: {n}\ndata: {json.dumps(d)}\n\n" for n, d in ev)


def openai_sse(body, req=None):
    c = body["choices"][0]
    m = c["message"]
    delta = {"role": "assistant", "content": m.get("content")}
    if m.get("tool_calls"):
        delta["tool_calls"] = [{"index": i, **t} for i, t in enumerate(m["tool_calls"])]
    chunk = lambda d, f: {"id": body["id"], "object": "chat.completion.chunk", "created": body["created"],
                          "model": body["model"], "choices": [{"index": 0, "delta": d, "finish_reason": f}]}
    parts = [chunk(delta, None), chunk({}, c["finish_reason"])]
    options = (req or {}).get("stream_options")
    if isinstance(options, dict) and options.get("include_usage"):
        parts.append({"id": body["id"], "object": "chat.completion.chunk", "created": body["created"],
                      "model": body["model"], "choices": [], "usage": body["usage"]})
    return "".join(f"data: {json.dumps(x)}\n\n" for x in parts) + "data: [DONE]\n\n"
