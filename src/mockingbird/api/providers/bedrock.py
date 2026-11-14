"""Bedrock Runtime Converse adapters. Stdlib only; no AWS credentials or signing needed for stand-in replies."""
import copy
import hashlib
import json
import re
import struct
from urllib.parse import unquote, urlsplit
import zlib

from ...media.attachments import InputError

CONTENT_TYPE = "application/vnd.amazon.eventstream"
ROUTE = re.compile(r"/model/(.+)/(converse|converse-stream)/?$")
STOP = {"stop": "end_turn", "tool_calls": "tool_use", "length": "max_tokens"}


def matches(path):
    return ROUTE.fullmatch(urlsplit(path).path) is not None


def _source(source, mime):
    if not isinstance(source, dict):
        raise InputError("Bedrock attachment source must be an object")
    if "bytes" in source:
        data = source["bytes"]
        if not isinstance(data, str):
            raise InputError("serialized Bedrock attachment bytes must be base64 text")
        return {"type": "base64", "media_type": mime, "data": data}
    if "text" in source and isinstance(source["text"], str):
        return {"type": "text", "media_type": "text/plain", "data": source["text"]}
    raise InputError("Bedrock attachments require inline bytes/text; S3 references are unsupported", 501)


def _blocks(blocks, tool_result=False):
    if not isinstance(blocks, list):
        raise InputError("Bedrock content must be a list")
    result = []
    for block in blocks:
        if not isinstance(block, dict) or len(block) != 1:
            raise InputError("Bedrock content blocks must have exactly one content field")
        kind, value = next(iter(block.items()))
        if kind == "text":
            if not isinstance(value, str):
                raise InputError("Bedrock text content must be a string")
            result.append({"type": "text", "text": value})
        elif kind == "json" and tool_result:
            result.append({"type": "text", "text": json.dumps(value, sort_keys=True)})
        elif kind in ("image", "document"):
            if not isinstance(value, dict):
                raise InputError(f"Bedrock {kind} must be an object")
            fmt = value.get("format", "")
            if kind == "image":
                if fmt not in ("png", "jpeg", "gif", "webp"):
                    raise InputError("Bedrock images support PNG, JPEG, GIF and WebP", 501)
                mime = "image/" + fmt
            else:
                if fmt not in ("pdf", "txt", "csv", "html", "md"):
                    raise InputError("Bedrock documents support PDF and UTF-8 txt/csv/html/md", 501)
                mime = "application/pdf" if fmt == "pdf" else "text/plain"
                context = {key: value[key] for key in ("name", "context") if key in value}
                if context:
                    result.append({"type": "text", "text": "[document metadata] " + json.dumps(context)})
            result.append({"type": kind, "source": _source(value.get("source"), mime)})
        elif kind == "toolUse":
            if not isinstance(value, dict) or not isinstance(value.get("toolUseId"), str) or not isinstance(
                    value.get("name"), str) or not isinstance(value.get("input", {}), dict):
                raise InputError("Bedrock toolUse needs toolUseId, name and an input object")
            result.append({"type": "tool_use", "id": value["toolUseId"], "name": value["name"],
                           "input": value.get("input", {})})
        elif kind == "toolResult":
            if not isinstance(value, dict) or not isinstance(value.get("toolUseId"), str):
                raise InputError("Bedrock toolResult needs toolUseId")
            result.append({"type": "tool_result", "tool_use_id": value["toolUseId"],
                           "content": _blocks(value.get("content"), tool_result=True),
                           "is_error": value.get("status") == "error"})
        elif kind == "cachePoint":
            continue  # cache hints do not change the content the stand-in should answer
        else:
            raise InputError(f"Bedrock {kind} content is unsupported by the stand-in", 501)
    return result


def normalize(path, req):
    route = ROUTE.fullmatch(urlsplit(path).path)
    if not route:
        raise InputError("only Bedrock Converse and ConverseStream are supported", 501)
    if not isinstance(req, dict) or not isinstance(req.get("messages"), list):
        raise InputError("Bedrock Converse requires messages; managed prompt references are unsupported", 501)
    result = {"model": unquote(route.group(1)), "stream": route.group(2) == "converse-stream", "messages": []}
    for message in req["messages"]:
        if not isinstance(message, dict) or message.get("role") not in ("user", "assistant"):
            raise InputError("Bedrock message roles must be user or assistant")
        result["messages"].append({"role": message["role"], "content": _blocks(message.get("content"))})
    if "system" in req:
        result["system"] = _blocks(req["system"])
    config = req.get("inferenceConfig", {})
    if not isinstance(config, dict):
        raise InputError("Bedrock inferenceConfig must be an object")
    for source, target in (("maxTokens", "max_tokens"), ("temperature", "temperature"), ("topP", "top_p"),
                           ("stopSequences", "stop_sequences")):
        if source in config:
            result[target] = config[source]
    tool_config = req.get("toolConfig", {})
    if not isinstance(tool_config, dict) or not isinstance(tool_config.get("tools", []), list):
        raise InputError("Bedrock toolConfig must contain a tools list")
    if tool_config.get("tools"):
        result["tools"] = []
        for tool in tool_config["tools"]:
            spec = tool.get("toolSpec") if isinstance(tool, dict) else None
            if not isinstance(spec, dict) or not isinstance(spec.get("name"), str):
                raise InputError("only Bedrock toolSpec tools are supported", 501)
            schema = spec.get("inputSchema", {})
            if not isinstance(schema, dict) or not isinstance(schema.get("json", {}), dict):
                raise InputError("Bedrock tool inputSchema.json must be an object")
            result["tools"].append({"name": spec["name"], "description": spec.get("description", ""),
                                    "input_schema": copy.deepcopy(schema.get("json", {}))})
    choice = tool_config.get("toolChoice")
    if choice is not None:
        if not isinstance(choice, dict) or len(choice) != 1 or next(iter(choice)) not in ("auto", "any", "tool"):
            raise InputError("invalid Bedrock toolChoice")
        kind, value = next(iter(choice.items()))
        result["tool_choice"] = {"type": kind}
        if kind == "tool":
            if not isinstance(value, dict) or not isinstance(value.get("name"), str):
                raise InputError("Bedrock forced toolChoice needs a name")
            result["tool_choice"]["name"] = value["name"]
    # Preserve provider-specific controls in prompts and cache identity without duplicating media bytes.
    result["bedrock_options"] = {k: copy.deepcopy(v) for k, v in req.items()
                                 if k not in ("messages", "system", "inferenceConfig", "toolConfig")}
    result["bedrock_request_hash"] = hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()
    return result


def response(ans, req, rid):
    if "raw" in ans:
        return ans["raw"]
    content = [{"text": ans["text"]}] if ans.get("text") else []
    seed = json.dumps({k: v for k, v in req.items() if k != "stream"}, sort_keys=True)
    for i, tool in enumerate(ans.get("tool_calls") or []):
        tool_id = "tooluse_" + hashlib.sha256(f"{seed}:{i}".encode()).hexdigest()[:22]
        content.append({"toolUse": {"toolUseId": tool_id, "name": tool["name"], "input": tool.get("input", {})}})
    stop = ans.get("stop") or ("tool_use" if ans.get("tool_calls") else "end_turn")
    return {"output": {"message": {"role": "assistant", "content": content}}, "stopReason": STOP.get(stop, stop),
            "usage": {"inputTokens": 0, "outputTokens": 0, "totalTokens": 0}, "metrics": {"latencyMs": 0}}


def _event(name, value):
    headers = b""
    for key, val in ((":message-type", "event"), (":event-type", name), (":content-type", "application/json")):
        key, val = key.encode(), val.encode()
        headers += bytes([len(key)]) + key + b"\x07" + struct.pack(">H", len(val)) + val
    payload = json.dumps(value, separators=(",", ":")).encode()
    prelude = struct.pack(">II", 16 + len(headers) + len(payload), len(headers))
    frame = prelude + struct.pack(">I", zlib.crc32(prelude)) + headers + payload
    return frame + struct.pack(">I", zlib.crc32(frame))


def stream(body):
    events = [_event("messageStart", {"role": "assistant"})]
    for i, block in enumerate(body["output"]["message"]["content"]):
        if "text" in block:
            delta = {"text": block["text"]}
        elif "toolUse" in block:
            tool = block["toolUse"]
            events.append(_event("contentBlockStart", {"contentBlockIndex": i, "start": {
                "toolUse": {"toolUseId": tool["toolUseId"], "name": tool["name"]}}}))
            delta = {"toolUse": {"input": json.dumps(tool.get("input", {}))}}
        else:
            raise InputError("raw Bedrock streaming supports text and toolUse blocks only", 501)
        events.append(_event("contentBlockDelta", {"contentBlockIndex": i, "delta": delta}))
        events.append(_event("contentBlockStop", {"contentBlockIndex": i}))
    events.extend([_event("messageStop", {"stopReason": body["stopReason"]}),
                   _event("metadata", {"usage": body["usage"], "metrics": body["metrics"]})])
    return b"".join(events)


def error(status, message):
    kind = {400: "ValidationException", 401: "UnrecognizedClientException", 403: "AccessDeniedException",
            409: "ConflictException", 413: "ValidationException", 429: "ThrottlingException",
            500: "InternalServerException", 502: "ServiceUnavailableException", 503: "ServiceUnavailableException",
            504: "ModelTimeoutException"}.get(status, "ValidationException")
    return {"__type": kind, "message": message}
