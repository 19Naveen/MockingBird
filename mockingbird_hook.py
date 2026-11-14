"""mockingbird hook, loaded at Python startup by mockingbird_hook.pth in one environment (venv).

While `bridge.py serve` runs on this machine, httpx calls from this environment to the Anthropic, OpenAI and
Azure OpenAI APIs go to the bridge instead. When the bridge stops, calls go to the real API again, even in a
process that is already running. Covers the official `anthropic` and `openai` SDKs and anything built on them.
Install or remove it with `bridge.py hook install|uninstall <python>`. Never install it in a production environment.
"""
import json
import os
import sys

STATE = os.path.join(os.path.expanduser("~"), ".mockingbird", "active.json")
HOSTS = ("api.anthropic.com", "api.openai.com")
MODULES = ("httpx", "httpx2")  # older SDKs use httpx; anthropic>=2 and openai>=3 use httpx2 (same API)
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


def _redirect(request):
    global _announced
    host = request.url.host
    if host not in HOSTS and not host.endswith(".openai.azure.com"):
        return
    port = bridge_port()
    if port is None:
        return
    request.url = request.url.copy_with(scheme="http", host="127.0.0.1", port=port)
    request.headers["host"] = f"127.0.0.1:{port}"
    if not _announced:
        _announced = True
        print(f"[mockingbird] LLM calls -> bridge on 127.0.0.1:{port} (stop the bridge to use the real API)",
              file=sys.stderr)


def _patch(httpx):
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
    for _name in MODULES:
        if _name in sys.modules:
            _patch(sys.modules[_name])
    sys.meta_path.insert(0, _PatchOnImport())
except Exception as e:  # a broken hook must never break the app
    print(f"[mockingbird] hook disabled: {e!r}", file=sys.stderr)
