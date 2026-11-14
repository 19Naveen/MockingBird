"""Gemini Developer API and Vertex Gemini generateContent compatibility (stdlib only)."""
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

from ...media.attachments import InputError

CONTENT_TYPE = "text/event-stream"
_ROUTE = re.compile(
    r"^/(?:v1(?:beta1?)?/)?(?:(?:projects/[^/]+/locations/[^/]+/)?publishers/google/)?"
    r"models/(?P<model>[^/:]+):(?P<method>generateContent|streamGenerateContent)/?$"
)


def matches(path):
    return bool(_ROUTE.fullmatch(urlsplit(path).path))


def _object(value, label):
    if not isinstance(value, dict):
        raise InputError(f"Google {label} must be an object")
    return value


def _list(value, label):
    if not isinstance(value, list):
        raise InputError(f"Google {label} must be a list")
    return value


def _field(obj, *names, default=None):
    """Gemini Developer JSON uses camelCase; the Vertex SDK can emit snake_case aliases."""
    for name in names:
        if name in obj:
            return obj[name]
    return default


def normalize(path, req):
    route = _ROUTE.fullmatch(urlsplit(path).path)
    if not route:
        raise InputError("only Gemini generateContent and streamGenerateContent are supported", 501)
    _object(req, "request")
    known_calls = {}

    def parts(values, location):
        blocks = []
        for index, part in enumerate(_list(values, "parts")):
            part = _object(part, "part")
            text = _field(part, "text")
            inline = _field(part, "inlineData", "inline_data")
            call = _field(part, "functionCall", "function_call")
            result = _field(part, "functionResponse", "function_response")
            if text is not None:
                if not isinstance(text, str):
                    raise InputError("Google part text must be a string")
                blocks.append({"type": "text", "text": text})
            elif inline is not None:
                inline = _object(inline, "inlineData")
                mime, data = _field(inline, "mimeType", "mime_type"), inline.get("data")
                if not isinstance(mime, str) or not isinstance(data, str):
                    raise InputError("Google inlineData needs string mimeType and base64 data")
                # Protobuf JSON bytes also accept URL-safe/unpadded base64; attachments use strict standard base64.
                data = data.translate(str.maketrans("-_", "+/"))
                data += "=" * (-len(data) % 4)
                kind = "image" if mime.startswith("image/") else "document"
                blocks.append({"type": kind, "source": {"type": "base64", "media_type": mime, "data": data}})
            elif call is not None:
                call = _object(call, "functionCall")
                name, arguments = call.get("name"), call.get("args", {})
                if not isinstance(name, str) or not isinstance(arguments, dict):
                    raise InputError("Google functionCall needs string name and object args")
                token = json.dumps([location, index, name, arguments], sort_keys=True)
                call_id = call.get("id") or "call_" + hashlib.sha256(token.encode()).hexdigest()[:24]
                if not isinstance(call_id, str):
                    raise InputError("Google functionCall id must be a string")
                known_calls[name] = call_id
                blocks.append({"type": "tool_use", "id": call_id, "name": name, "input": arguments})
            elif result is not None:
                result = _object(result, "functionResponse")
                name, value = result.get("name"), result.get("response")
                if not isinstance(name, str) or not isinstance(value, dict):
                    raise InputError("Google functionResponse needs string name and object response")
                content = [{"type": "text", "text": json.dumps(value, sort_keys=True)}]
                if "parts" in result:
                    content.extend(parts(result["parts"], f"{location}.{index}"))
                blocks.append({"type": "tool_result", "tool_use_id": result.get("id") or known_calls.get(name, name),
                               "content": content})
            elif _field(part, "fileData", "file_data") is not None:
                raise InputError("Google fileData URIs are not fetched; send inlineData instead", 501)
            else:
                raise InputError("unsupported Google part; send text, inlineData, functionCall or functionResponse", 501)
        return blocks

    messages = []
    for index, content in enumerate(_list(req.get("contents", []), "contents")):
        content = _object(content, "content")
        role = content.get("role", "user")
        if role not in ("user", "model"):
            raise InputError("Google content role must be user or model")
        messages.append({"role": "assistant" if role == "model" else "user", "content": parts(content.get("parts", []), index)})
    canonical = {"model": unquote(route["model"]), "messages": messages,
                 "stream": route["method"] == "streamGenerateContent",
                 "_google_route": urlsplit(path).path.replace(":streamGenerateContent", ":generateContent").rstrip("/"),
                 "_google_request_hash": hashlib.sha256(json.dumps(req, sort_keys=True).encode()).hexdigest()}
    if "systemInstruction" in req or "system_instruction" in req:
        system = _object(_field(req, "systemInstruction", "system_instruction"), "systemInstruction")
        canonical["system"] = parts(system.get("parts", []), "system")
        if any(block["type"] != "text" for block in canonical["system"]):
            raise InputError("Google systemInstruction supports text parts only", 501)
    tools = []
    for tool in _list(req.get("tools", []), "tools"):
        tool = _object(tool, "tool")
        if set(tool) - {"functionDeclarations", "function_declarations"}:
            raise InputError("Google built-in tools are unsupported; use functionDeclarations", 501)
        for declaration in _list(_field(tool, "functionDeclarations", "function_declarations", default=[]),
                                 "functionDeclarations"):
            declaration = _object(declaration, "function declaration")
            if not isinstance(declaration.get("name"), str):
                raise InputError("Google function declaration needs a string name")
            schema = _field(declaration, "parametersJsonSchema", "parameters_json_schema", "parameters",
                            default={"type": "object", "properties": {}})
            _object(schema, "function parameters")
            tools.append({"name": declaration["name"], "description": declaration.get("description", ""), "input_schema": schema})
    if tools:
        canonical["tools"] = tools
    config = _object(_field(req, "generationConfig", "generation_config", default={}), "generationConfig")
    if _field(config, "candidateCount", "candidate_count", default=1) != 1:
        raise InputError("Google stand-in mode supports candidateCount=1 only", 501)
    for source, dest in (("maxOutputTokens", "max_tokens"), ("max_output_tokens", "max_tokens"),
                         ("temperature", "temperature"), ("topP", "top_p"), ("top_p", "top_p"),
                         ("topK", "top_k"), ("top_k", "top_k"),
                         ("stopSequences", "stop_sequences"), ("stop_sequences", "stop_sequences")):
        if source in config:
            canonical[dest] = config[source]
    if _field(config, "responseMimeType", "response_mime_type") == "application/json":
        schema = _field(config, "responseJsonSchema", "response_json_schema", "responseSchema", "response_schema")
        canonical["response_format"] = ({"type": "json_schema", "json_schema": {"name": "response", "schema": schema}}
                                         if schema is not None else {"type": "json_object"})
    tool_config = _object(_field(req, "toolConfig", "tool_config", default={}), "toolConfig")
    calling = _object(_field(tool_config, "functionCallingConfig", "function_calling_config", default={}),
                      "functionCallingConfig")
    mode = calling.get("mode", "AUTO")
    allowed = _field(calling, "allowedFunctionNames", "allowed_function_names", default=[])
    if not isinstance(allowed, list) or any(not isinstance(name, str) for name in allowed):
        raise InputError("Google allowedFunctionNames must be a list of strings")
    if mode == "NONE":
        canonical["tool_choice"] = {"type": "none"}
    elif mode == "ANY":
        canonical["tool_choice"] = ({"type": "tool", "name": allowed[0]} if len(allowed) == 1
                                     else {"type": "any", "allowed_tools": allowed})
    elif mode in ("AUTO", "VALIDATED"):
        canonical["tool_choice"] = {"type": "auto"}
    else:
        raise InputError("unsupported Google function calling mode", 501)
    # Keep tuning/safety/schema options visible; hash contents separately to avoid duplicating base64 blobs.
    canonical["google_options"] = {k: v for k, v in req.items() if k not in (
        "contents", "systemInstruction", "system_instruction", "tools", "generationConfig", "generation_config",
        "toolConfig", "tool_config")}
    return canonical


def response(ans, canonical_req, rid):
    if "raw" in ans:
        return ans["raw"]
    content = [{"text": ans["text"]}] if ans.get("text") else []
    identity = {k: v for k, v in canonical_req.items() if k != "stream"}
    seed = json.dumps(identity, sort_keys=True)
    for index, call in enumerate(ans.get("tool_calls") or []):
        call_id = "call_" + hashlib.sha256(f"{seed}:{index}".encode()).hexdigest()[:24]
        content.append({"functionCall": {"id": call_id, "name": call["name"], "args": call.get("input", {})}})
    stop = "MAX_TOKENS" if ans.get("stop") == "max_tokens" else "STOP"
    return {"candidates": [{"index": 0, "content": {"role": "model", "parts": content}, "finishReason": stop}],
            "usageMetadata": {"promptTokenCount": 0, "candidatesTokenCount": 0, "totalTokenCount": 0},
            "modelVersion": canonical_req.get("model", "bridge"), "responseId": "bridge-" + rid}


def stream(body):
    return "data: " + json.dumps(body) + "\n\n"


def error(status, msg):
    name = {400: "INVALID_ARGUMENT", 401: "UNAUTHENTICATED", 403: "PERMISSION_DENIED", 404: "NOT_FOUND",
            409: "ABORTED", 413: "RESOURCE_EXHAUSTED", 429: "RESOURCE_EXHAUSTED", 500: "INTERNAL",
            501: "UNIMPLEMENTED", 502: "UNAVAILABLE", 503: "UNAVAILABLE", 504: "DEADLINE_EXCEEDED"}.get(status, "UNKNOWN")
    return {"error": {"code": status, "message": msg, "status": name}}
