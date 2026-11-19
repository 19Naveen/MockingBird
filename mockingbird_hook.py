"""mockingbird hook, loaded at Python startup by mockingbird_hook.pth in one environment (venv).

With MOCKINGBIRD_ENABLED=1, while `bridge.py serve` runs, httpx calls from this environment to the Anthropic, OpenAI and
Azure OpenAI APIs go to the bridge instead. When the bridge stops, calls go to the real API again, even in a
process that is already running. Covers the official `anthropic` and `openai` SDKs and anything built on them.
Install or remove it with `bridge.py hook install|uninstall <python>`. Never install it in a production environment.
"""
import json
import os
import re
import sys
from urllib.parse import urlsplit, urlunsplit

STATE = os.environ.get("MOCKINGBIRD_STATE", os.path.join(os.path.expanduser("~"), ".mockingbird", "active.json"))
HOSTS = ("api.anthropic.com", "api.openai.com", "generativelanguage.googleapis.com", "aiplatform.googleapis.com")
SUFFIXES = (".openai.azure.com", ".services.ai.azure.com", ".models.ai.azure.com", ".inference.ai.azure.com",
            "-aiplatform.googleapis.com")
MODULES = ("httpx", "httpx2", "requests.sessions", "aiohttp.client", "botocore.httpsession", "google.auth._default")
_announced = False


def bridge_port():
    """Port of the running bridge, or None. Checked on every call, so stopping the bridge takes effect at once."""
    try:
        with open(STATE) as f:
            state = json.load(f)
        os.kill(state["pid"], 0)  # file left behind by a crashed bridge -> treat as stopped
        return int(state["port"])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _provider(host):
    return (host in HOSTS or host.endswith(SUFFIXES) or
            bool(re.fullmatch(r"bedrock-runtime(?:-fips)?\.[a-z0-9-]+\.amazonaws\.com(?:\.cn)?", host)))


def _announce(port):
    global _announced
    if not _announced:
        _announced = True
        suffix = "" if os.environ.get("MOCKINGBIRD_REQUIRE_BRIDGE") == "1" else " (stop the bridge to use the real API)"
        print(f"[mockingbird] LLM calls -> bridge on 127.0.0.1:{port}{suffix}", file=sys.stderr)


def _redirect_port():
    port = bridge_port()
    if port is None and os.environ.get("MOCKINGBIRD_REQUIRE_BRIDGE") == "1":
        raise ConnectionError("Mockingbird bridge is unavailable; refusing a direct provider call")
    return port


def _redirect_url(url, headers):
    if os.environ.get("MOCKINGBIRD_ENABLED") != "1":
        return url
    parsed = urlsplit(str(url))
    if not parsed.hostname or not _provider(parsed.hostname):
        return url
    port = _redirect_port()
    if port is None:
        return url
    headers["x-mockingbird-upstream"] = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
    headers["host"] = f"127.0.0.1:{port}"
    _announce(port)
    return urlunsplit(("http", f"127.0.0.1:{port}", parsed.path, parsed.query, parsed.fragment))


def _redirect(request):
    if isinstance(request.url, str):
        request.url = _redirect_url(request.url, request.headers)
        return
    if os.environ.get("MOCKINGBIRD_ENABLED") != "1" or not _provider(request.url.host):
        return
    port = _redirect_port()
    if port is None:
        return
    host, original_port = request.url.host, request.url.port
    request.headers["x-mockingbird-upstream"] = (f"{request.url.scheme}://{host}"
                                                 + (f":{original_port}" if original_port else ""))
    request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
    request.headers["host"] = f"127.0.0.1:{port}"
    _announce(port)


def _patch(httpx):
    name = httpx.__name__
    if name == "google.auth._default":
        original = httpx.default
        if getattr(original, "_mockingbird", False):
            return
        def default(*args, **kw):
            if os.environ.get("MOCKINGBIRD_ENABLED") == "1" and bridge_port() is not None:
                from google.auth.credentials import AnonymousCredentials
                credentials = AnonymousCredentials()
                credentials.token = "bridge-dummy"
                return credentials, os.environ.get("GOOGLE_CLOUD_PROJECT", "bridge-test-project")
            return original(*args, **kw)
        default._mockingbird = True
        httpx.default = default
        parent = sys.modules.get("google.auth")
        if parent and getattr(parent, "default", None) is original:
            parent.default = default
        return
    if name in ("requests.sessions", "botocore.httpsession"):
        cls = httpx.Session if name == "requests.sessions" else httpx.URLLib3Session
        send = cls.send
        if getattr(send, "_mockingbird", False):
            return
        def patched(self, request, *args, **kw):
            _redirect(request)
            return send(self, request, *args, **kw)
        patched._mockingbird = True
        cls.send = patched
        return
    if name == "aiohttp.client":
        send = httpx.ClientSession._request
        if getattr(send, "_mockingbird", False):
            return
        async def patched(self, method, url, *args, **kw):
            headers = dict(kw.get("headers") or {})
            redirected = _redirect_url(url, headers)
            if redirected != url:
                kw["headers"] = headers
            return await send(self, method, redirected, *args, **kw)
        patched._mockingbird = True
        httpx.ClientSession._request = patched
        return
    if getattr(httpx.Client.send, "_mockingbird", False):
        return
    send, asend = httpx.Client.send, httpx.AsyncClient.send

    def patched_send(self, request, **kw):
        _redirect(request)
        return send(self, request, **kw)

    async def patched_asend(self, request, **kw):
        _redirect(request)
        return await asend(self, request, **kw)

    setattr(patched_send, "_mockingbird", True)
    setattr(patched_asend, "_mockingbird", True)
    httpx.Client.send, httpx.AsyncClient.send = patched_send, patched_asend


class _PatchOnImport:
    """Patches httpx/httpx2 right after the app imports them, so processes that never use them pay nothing."""
    busy = False

    def find_spec(self, name, path=None, target=None):
        if name not in MODULES or self.busy:
            return None
        self.busy = True  # the lookup below walks sys.meta_path again and must skip us
        try:
            import importlib.util
            spec = importlib.util.find_spec(name)
        finally:
            self.busy = False
        if spec and spec.loader:
            run = spec.loader.exec_module

            def exec_module(module):
                run(module)
                _patch(module)
            spec.loader.exec_module = exec_module
        return spec


try:
    if os.environ.get("MOCKINGBIRD_ENABLED") == "1":
        for _name in MODULES:
            if _name in sys.modules:
                _patch(sys.modules[_name])
        sys.meta_path.insert(0, _PatchOnImport())
except Exception as e:  # a broken hook must never break the app
    print(f"[mockingbird] hook disabled: {e!r}", file=sys.stderr)
