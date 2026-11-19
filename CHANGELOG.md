# Unreleased

- Fresh installs and upgrades never add, refresh, or remove shell hooks. Existing users can remove the legacy global hook with `mockingbird hook uninstall`; manual hook installation is still available.
- Live and Cache distinguish the app's requested model from the answering backend. Cache details wrap inside the panel and indent JSON answers.
- Cooperating Azure projects can start a project-local dashboard with just `mockingbird`; the app discovers it without a hook or environment flag. The dashboard starts with real Azure forwarding, and keeps provider switching, caching, and analytics.
- `mockingbird run --hook-only` preserves configured endpoint URLs for Python apps that require HTTPS. Its temporary hook redirects supported provider calls locally and refuses direct fallback if the private bridge stops.
- OpenAI/Azure Responses supports stateless text and inline image/file inputs with JSON output in stand-in mode; Real API mode forwards Responses requests unchanged. Responses streaming, tools and conversation state remain unsupported in stand-in mode. PDFs require Poppler.
- The Cache tab shows input and output token counts for the selected original answer when usage is available; replay does not spend those tokens again.
- Press `p` in the dashboard to rotate between the real API and installed Claude Code, Codex and opencode stand-ins. Switching waits for active calls to finish and is disabled during offline replay.
- The mode indicator shows the answering model when it can be determined, and `(default)` when it cannot.
- The installer now picks the coding agent that answers requests with no `--model`: Claude Code if installed, otherwise Codex, otherwise opencode. It prints which one, and keeps any choice already saved, so updates do not change it. Install never prompts. `mockingbird agent` shows the choice, and `mockingbird agent codex` (or `claude`, `opencode`, `clear`) changes it. `serve` uses the saved agent when it is still installed, and falls back to the first one found; with none installed it stays in agent mode.
- The dashboard's compact server status moved from the top row to the bottom-right footer. It shows the port and mode without the routine hook notice.
- Built-in CLI stand-ins now expose available answering-CLI token usage in provider-shaped responses, including streaming where supported. These figures describe the stand-in, not the target production model; replay repeats the recorded usage. Changing the built-in commands to request structured output changes their cache identities, so existing stand-in fixtures must be recorded again.
- Moved the implementation into a standard `src/` layout with `pyproject.toml`, and grouped the package by concern (`core/`, `api/`, `providers/`, `media/`, `ui/`). `bridge.py` remains the installed entry point, `mockingbird_hook.py` stays standalone, and the opt-in provider checks moved to `tests/smoke/`. No command, environment variable or wire-format changes.
- Claude Code image and PDF input now uses the stream-json output that its stream-json input requires, so multimodal requests work with current Claude Code releases.

# v1.14.0

Run AI integration tests with less setup and repeat them offline.

- `mockingbird run [serve options] -- COMMAND` starts a private bridge, configures SDK URLs and a process-scoped Python hook, preserves the command's exit status, and stops the bridge and its CLI children afterward.
- `--replay` serves cache hits without invoking a CLI or upstream API. Cache misses return HTTP 409. Use the same `--model` and `BRIDGE_NAMESPACE` as the recording run; the CLI does not have to be installed to replay.
- Stand-in cache keys include the provider API and selected backend command, including explicit model and effort. `BRIDGE_NAMESPACE` can name a fixed fixture set when a CLI uses a configurable default model.
- `--faults FILE` injects a finite sequence of HTTP errors, delayed responses, or malformed bodies. It is off unless explicitly requested and never writes injected responses into the cache.
- Hooks are optional and require `MOCKINGBIRD_ENABLED=1`. The command runner needs no shell-file or virtual-environment installation.
- `--redact` disables disk caching and hides details in new logs/history. Cache and queue JSON files are written atomically with owner-only permissions.
- Identical successful concurrent cache misses share one generation within a server. Real-response cache keys distinguish deployment paths and API-version queries.
- JSON replies use incremental decoding and explicit validation, including under optimized Python. Legitimate local provider gateways remain in CLI environments.
- Inline PNG/JPEG/GIF/WebP images, UTF-8 files and text documents now reach the stand-in as content. PDFs use optional Poppler to render pages into images. Each built-in CLI receives native image attachments; temporary media is private and cleaned up. Unsupported references, oversized data and unavailable PDF tooling return explicit errors.
- Azure AI Foundry works through its OpenAI v1, Azure Inference and Anthropic routes, including sync/async SDKs and hard-coded Azure host redirects.
- Amazon Bedrock Runtime Converse and ConverseStream are supported with native tool calls/results, image and document blocks, throttling errors, and runner endpoint routing.
- Gemini Developer API and Vertex AI `generateContent`/`streamGenerateContent` are supported, including camelCase and Vertex snake_case fields, system instructions, tools, JSON mode, inline images/documents and usage metadata.
- Added provider regression tests and deterministic SDK smoke checks for Foundry, Bedrock and Gemini; all replay passes assert zero fresh generations, and CI runs the provider checks alongside the Python 3.9–3.14 matrix.
- Updates download the installer and payloads from the same published release tag. Public curl install commands continue to use `main` without a version in the URL.
- Added a Python 3.9–3.14 regression matrix, opt-in SDK checks for Claude Code, Codex and opencode, and an MIT license. Only a final version-tag push publishes a release after the test matrix passes.

Compatibility notes: pre-1.14 stand-in cache entries are still viewable but are not automatically replayed because their answering backend is unknown. Replay compares request JSON exactly apart from stand-in `stream`; use stable fixture inputs. Default models configured outside mockingbird cannot be detected automatically: pass an explicit model or change `BRIDGE_NAMESPACE` when changing CLI defaults. Native Bedrock and Gemini adapters are stand-in/replay only: `--real` returns 501 rather than forwarding them. Stand-in responses remain a subset of provider behavior, and prompt injection remains possible with untrusted requests.
