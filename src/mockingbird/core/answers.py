"""Answer lookup: replay from cache, generate through the auto CLI, or wait for an agent."""

import json
import time

from ..ui import dashboard
from . import agent, log, state, store


def get_answer(api, path, req, rid):
    """Returns (answer or None, source): CACHE, FRESH (auto CLI) or AGENT (subagent)."""
    with store.cache_lock(store.request_key(req, api)):
        return _get_answer(api, path, req, rid)


def _get_answer(api, path, req, rid):
    cf = store.cache_file(req, api)
    if cf and cf.exists():
        log.log(f"{rid} replayed from cache")
        data = json.loads(cf.read_text())
        return dashboard.cached_answer(data), "CACHE"
    if state.REPLAY:
        return {"error": "offline replay cache miss", "status": 409}, "CACHE"
    ans = agent.run_auto(api, req, rid) if state.AUTO else agent.wait_for_agent(api, path, req, rid)
    if cf and ans is not None and "error" not in ans:  # never cache failures
        cf.parent.mkdir(parents=True, exist_ok=True)
        (cf.parent / ".gitignore").write_text("*\n")  # answers may echo test data: opt in to committing them
        store.write(cf, {"meta": {"saved": time.time(), "model": str(req.get("model", "")), "api": api,
                                  "project": state.PROJECT, "asked": store.asked_text(req),
                                  "label": store.describe(ans), "backend": state.AUTO or "agent",
                                  "namespace": state.NAMESPACE},
                         "answer": ans})
    return ans, "FRESH" if state.AUTO else "AGENT"
