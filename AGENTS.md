# AGENTS.md

## What this is

`mockingbird` is a stdlib-only Python 3.9+ HTTP shim that stands in for Anthropic, OpenAI/Azure/Foundry, Bedrock and Gemini APIs during tests. `bridge.py` at the repository root is the installed entry point; it adds `src/` to `sys.path` and calls `mockingbird.cli.main`.

## Commands

```bash
python3 -m unittest discover -s tests -v            # full unit suite
python3 tests/test_bridge.py                        # one test module
python3 tests/test_bridge.py BridgeTests.test_name  # one test
python3 -m unittest discover -s tests -p 'test_google.py' -v
python3 bridge.py version                           # source entry point
PYTHONPATH=src python3 -m mockingbird version       # module entry point
bash -n install.sh uninstall.sh                     # CI shell syntax check
```

The unit suite is stdlib-only; provider smokes need `uv` and optional SDKs. They stay entirely local; run them from the repo root:

```bash
uv run --with google-genai python tests/smoke/google_smoke.py
uv run --with azure-ai-inference --with aiohttp --with openai --with anthropic python tests/smoke/foundry_smoke.py
uv run --with boto3 python tests/smoke/bedrock_smoke.py
```

There is no lint, typecheck or formatter config. CI (`.github/workflows/tests.yml`) runs the unit matrix on Python 3.9–3.14, then those three provider smokes; only a `v*` tag publishes a release.

Installer smoke, when changing `install.sh` or the payload (it writes `~/.agents/skills/mockingbird` and `~/.local/bin/mockingbird`):

```bash
MOCKINGBIRD_SRC=. bash install.sh
```

## Layout and wiring

- `src/mockingbird/cli.py`: commands, `serve`, `run`, hook install, selftest.
- `src/mockingbird/core/`: `state` (mutable settings), `store` (cache keys, atomic JSON), `agent` (prompt, queue, auto CLI), `answers` (cache/auto/agent choice), `log`.
- `src/mockingbird/api/`: `server` (HTTP entry), `responses` (Anthropic/OpenAI bodies and SSE), `providers/` (Bedrock/Gemini adapters).
- `src/mockingbird/media/attachments.py`: images, text documents and PDF rendering.
- `src/mockingbird/ui/dashboard.py`: journal, analytics and curses dashboard.

A request enters `api/server.py`, is identified and normalized, checked in `core/answers.py`, answered by `core/agent.run_auto` or the `next`/`prompt`/`answer` agent queue, then shaped by `api/responses.py` or a provider adapter.

## Non-obvious constraints

- Keep `bridge.py`, `mockingbird_hook.py`, `pyproject.toml`, `install.sh`, `uninstall.sh` and `CHANGELOG.md` at the repository root. Installs fetch the root `bridge.py` and hook, setuptools reads `pyproject.toml` there, the release workflow reads `CHANGELOG.md`, and `PROJECT_ROOT` assumes the root entry point. `install.sh` downloads an explicit file list, so add every new package file (including `__init__.py`) there and to the README layout table.
- `state.PROJECT_ROOT` is `parents[3]` from `src/mockingbird/core/state.py`. Moving the package deeper or shallower breaks `run`, hook install, updates and selftest. Keep the `src/mockingbird/<subpackage>/` depth; `src/mockingbird/` is the package, not a second repository root.
- `pyproject.toml` supplies the standard src layout and console script, but `install.sh` is the supported install.
- All mutable settings live in `src/mockingbird/core/state.py`; read them through the module (`from . import state`) and patch `state` in tests, never copy values into callers.
- Tests are plain `unittest`. `tests/_bootstrap.py` puts `src/` on `sys.path`; import it before `mockingbird` in every test module. `tests/smoke/` deliberately has no `__init__.py`, so unit discovery skips it; its scripts add `src/` and `tests/` to `sys.path` themselves.
- Cache and queue JSON go through `core/store.write` (temp file, `0600`, `os.replace`); don't write those files directly.
- A user-level hook in `PYTHONPATH` can shadow the checkout. Tests load the root `mockingbird_hook.py` with `spec_from_file_location`; keep that pattern.
- The hook is opt-in with `MOCKINGBIRD_ENABLED=1`; never enable it implicitly. Tests that exercise hook install must patch `Path.home()` / `HOOK_DIR`, not touch real shell files. Never write requests or keys to logs or the journal. Cache entries contain prompts and answers, and `--redact` disables caching.
- Provider smokes must stay offline and deterministic: dummy keys, local fake stand-ins, and zero fresh generations during replay. Native Bedrock/Gemini are stand-in/replay only; `--real` returns 501 by design.
- Keep the package stdlib-only and Python 3.9-compatible. Optional SDKs belong only in smoke commands.
- The version is duplicated in `src/mockingbird/core/state.py`, `pyproject.toml`, `install.sh`, the README badge and `SKILL.md` metadata. `update()` rejects an installer whose `VERSION` does not match the release tag. Bump them together, add release notes to `CHANGELOG.md` (the tag workflow uses it as the release body), and only tag `vX.Y.Z` when a release is requested.
