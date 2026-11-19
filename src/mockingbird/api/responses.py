"""Anthropic and OpenAI response bodies built from a stand-in answer."""

import hashlib
import json
import time

from ..core.store import req_key
from ..media.attachments import InputError

STOP_TO_OPENAI = {"end_turn": "stop", "tool_use": "tool_calls", "max_tokens": "length", "stop_sequence": "stop"}


def normalize_responses(req):
    """Translate the supported stateless Responses input into the existing stand-in format."""
    for field in ("previous_response_id", "conversation", "tools", "tool_choice", "background"):
        if req.get(field):
            raise InputError(f"Responses {field} is not supported in stand-in mode", 501)
    if req.get("stream"):
        raise InputError("Responses streaming is not supported in stand-in mode", 501)
    incoming = req.get("input")
    if isinstance(incoming, str):
        incoming = [{"role": "user", "content": incoming}]
    if not isinstance(incoming, list) or not incoming:
        raise InputError("Responses input must be a string or a non-empty message list")
    messages = []
    for message in incoming:
        if not isinstance(message, dict) or message.get("type", "message") != "message":
            raise InputError("Responses input supports messages only", 501)
        role = message.get("role")
        if role not in ("user", "assistant", "system", "developer"):
            raise InputError("Responses message role is unsupported", 501)
        content = message.get("content")
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    raise InputError("Responses content blocks must be objects")
                kind = part.get("type")
                if kind in ("input_text", "output_text"):
                    if not isinstance(part.get("text"), str):
                        raise InputError("Responses text must be a string")
                    blocks.append({"type": "text", "text": part["text"]})
                elif kind == "input_image":
                    if part.get("file_id"):
                        raise InputError("provider file IDs are unsupported; send inline data", 501)
                    blocks.append({"type": "image_url", "image_url": part.get("image_url", "")})
                elif kind == "input_file":
                    if part.get("file_id") or part.get("file_url"):
                        raise InputError("file IDs and remote file URLs are unsupported; send inline data", 501)
                    blocks.append({"type": "file", "file": {"filename": part.get("filename", ""),
                                                               "file_data": part.get("file_data")}})
                else:
                    raise InputError(f"Responses content type {kind!r} is unsupported", 501)
            content = blocks
        elif not isinstance(content, str):
            raise InputError("Responses message content must be text or a list")
        messages.append({"role": role, "content": content})
    instructions = req.get("instructions")
    if instructions is not None:
        if not isinstance(instructions, str):
            raise InputError("Responses instructions must be text")
        messages.insert(0, {"role": "system", "content": instructions})
    normalized = {k: v for k, v in req.items() if k not in ("input", "instructions", "text")}
    normalized["messages"] = messages
    text = req.get("text") or {}
    if not isinstance(text, dict):
        raise InputError("Responses text must be an object")
    fmt = text.get("format")
    if fmt:
        if not isinstance(fmt, dict) or fmt.get("type") not in ("text", "json_object", "json_schema"):
            raise InputError("Responses text format is unsupported", 501)
        normalized["response_format"] = fmt
    return normalized


def responses_body(ans, req, rid):
    if "raw" in ans:
        return ans["raw"]
    if ans.get("tool_calls"):
        raise InputError("Responses tool calls are not supported in stand-in mode", 501)
    prompt, completion, cached, _, _ = usage_counts(ans)
    usage = {"input_tokens": prompt, "output_tokens": completion, "total_tokens": prompt + completion}
    if cached:
        usage["input_tokens_details"] = {"cached_tokens": cached}
    incomplete = ans.get("stop") == "max_tokens"
    message = {"id": "msg_bridge_" + rid, "type": "message", "role": "assistant", "status": "completed",
               "content": [{"type": "output_text", "text": ans.get("text") or "", "annotations": []}]}
    return {"id": "resp_bridge_" + rid, "object": "response", "created_at": int(time.time()),
            "model": req.get("model", "bridge"), "status": "incomplete" if incomplete else "completed",
            "output": [message], "usage": usage,
            "incomplete_details": {"reason": "max_output_tokens"} if incomplete else None}


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
