"""Anthropic and OpenAI response bodies built from a stand-in answer."""

import hashlib
import json
import time

from .store import req_key

STOP_TO_OPENAI = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def _ids(req, prefix, n):
    # Same request -> same tool ids. Random ids made a replayed turn 1 change turn 2, so multi-turn loops missed the cache.
    seed = hashlib.sha256(req_key(req).encode()).hexdigest()
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
