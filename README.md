<pre align="center">
                         __   _             __    _          __
   ____ ___  ____  _____/ /__(_)___  ____ _/ /_  (_)________/ /
  / __ `__ \/ __ \/ ___/ //_/ / __ \/ __ `/ __ \/ / ___/ __  /
 / / / / / / /_/ / /__/ ,< / / / / / /_/ / /_/ / / /  / /_/ /
/_/ /_/ /_/\____/\___/_/|_/_/_/ /_/\__, /_.___/_/_/   \__,_/
                                  /____/
</pre>

<p align="center">
  <em>It mimics the API, so your tests don't need it.</em>
</p>

<p align="center">
  <img alt="v1.12.1" src="https://img.shields.io/badge/version-1.12.1-black">
  <img alt="Python 3.9+" src="https://img.shields.io/badge/python-3.9%2B-black">
  <img alt="zero dependencies" src="https://img.shields.io/badge/dependencies-0-black">
  <img alt="Claude Code, Codex, opencode" src="https://img.shields.io/badge/works%20with-Claude%20Code%20%7C%20Codex%20%7C%20opencode-black">
</p>

Your app's AI features call Anthropic or OpenAI. In tests, that costs money, needs keys, and gives a different answer every run. mockingbird catches those calls on your machine, lets Sonnet answer them through your Claude Code login, and saves every answer. The next run of the same test is instant and gives the same result.

Your code doesn't change. Stop the server and everything goes back to the real API.

<p align="center"><img src="docs/live.png" alt="The Live tab: a chart of calls, a row per model, and what each recent call asked" width="100%"></p>

## Install

```bash
curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/v1.12.1/install.sh | bash
```

Then open a new terminal. The installer:

1. Puts the tool in `~/.agents/skills/mockingbird` and runs its self-check.
2. Adds the `mockingbird` command to `~/.local/bin`.
3. Installs the **hook**: one marked `PYTHONPATH` line in `~/.bashrc` and `~/.zshrc`. It lets every Python app you start from a terminal be switched to mockingbird, with no per-project setup.
4. Puts the skill in the shared folder `~/.agents/skills`, which Codex and opencode read. Nothing goes into agent-specific folders.

On a machine that runs production code, skip the hook: `curl -fsSL … | MOCKINGBIRD_NO_HOOK=1 bash`.

The URL is pinned to the `v1.12.1` tag, so a later push to `main` can't change what it installs. Read `install.sh` before you pipe it to bash.

## Use

```bash
mockingbird serve     # on: the dashboard opens, and your apps' AI calls go to Sonnet
pytest                # in another terminal, run your app or tests as usual
                      # press q on the dashboard: everything talks to the real API again
```

| Command | What it does |
|---|---|
| `serve` | Starts the server and the dashboard. Sonnet answers, at medium effort |
| `serve --model opus[max]` | Picks the model and effort, such as `sonnet[high]` or `haiku` |
| `serve --plain` | Logs plain lines instead of drawing the dashboard (automatic with no terminal) |
| `serve --agent` | Lets a coding agent's subagents answer instead (see [SKILL.md](SKILL.md)) |
| `stats` | Prints the analytics for every call so far |
| `cache` | Lists the saved answers |
| `cache --purge` | Deletes every saved answer after asking (`--yes` skips the question). Your history stays |
| `update` | Installs the newest release. Your history and saved answers stay |
| `uninstall` | Removes mockingbird after asking. `--purge` also deletes your history and saved answers |
| `hook status` | Shows whether the hook is active in this terminal, and whether the server is on |
| `hook uninstall` | Removes the hook and restores your shell files exactly |
| `selftest`, `version` | Runs the built-in checks, prints the version |

## Which model answers

mockingbird answers through an AI plan you already have, using that plan's own command-line tool. You need no API key and pay no API bills. With no `--model`, it uses the first tool it finds:

| You have | mockingbird runs | Model it uses |
|---|---|---|
| Claude Code (a Claude plan) | `claude -p` | Sonnet at medium effort |
| Codex (a ChatGPT plan) | `codex exec` | The default model in your Codex config |
| opencode | `opencode run` | The default model in your opencode config, from any provider you set up there |
| None of these | agent mode | A coding agent's subagents answer instead (see [SKILL.md](SKILL.md)) |

To choose, pass `--model`: `sonnet[high]`, `opus[max]`, `haiku`, `codex`, `gpt-5[low]`, `opencode`, or `opencode/<provider>/<model>`.

**Why Sonnet by default:** mockingbird has to imitate an API exactly. It must call the right tools with valid arguments, return valid JSON when asked, and do it fast enough that a test suite doesn't drag. Sonnet does that reliably in about 6 to 8 seconds per fresh answer. Opus is slower and costs more of your plan's usage for little gain here. Haiku is faster, but more likely to get a tool call or JSON shape wrong. Replayed answers take no time whichever model wrote them.

The Codex and opencode paths are covered by the self-check, but haven't yet been tried against a real Codex or opencode account. If one misbehaves, the dashboard shows the error.

## The dashboard

Every call ends one of three ways, and the dashboard uses the same word and colour for it everywhere:

| | |
|---|---|
| **replayed** (green) | Answered from the cache, instantly |
| **fresh** (amber) | Answered by the model just now |
| **failed** (rose) | Returned an error to your app |

It has three tabs. Switch with `1` `2` `3` or `l` `a` `c`, the arrow keys, Tab, or a click.

**1 Live** shows what's happening now. It has a chart of calls (replayed stacked under fresh), a row per model with its state (`asking 4s`, `failed`, `idle`), and the last ten calls with what each one asked. Press `w` to switch between the last 60 minutes, 24 hours and 7 days. The top right shows the server's state: `● listening`, or `● asking` while a call is being answered.

**2 Analytics** covers every call so far: the replay rate, time saved, the last 14 days, how fast fresh answers are per model, how often each model is replayed, failed calls, and projects.

<p align="center"><img src="docs/analytics.png" alt="The Analytics tab: totals, the last 14 days, speed and replay rate per model, failed calls" width="100%"></p>

**3 Cache** lists every saved answer: when it was saved, the model, what was asked, the answer, and how often it was replayed. Use ↑ ↓ to select one and read it in full below. Press `x` twice to delete it, and the next identical call is asked fresh.

<p align="center"><img src="docs/cache.png" alt="The Cache tab: saved answers, with the selected one shown in full" width="100%"></p>

The screenshots come from a real run of the demo test suite.

## Let an agent run it

```
/mockingbird sonnet[high] pytest tests/ai
/mockingbird haiku                         # effort defaults to medium
```

The agent starts the server, routes your app to it, runs the tests, reports the results and cleans up. Codex and opencode find the skill in `~/.agents/skills`; in Codex, type `$mockingbird`. Claude Code doesn't read that folder, so there, run the `mockingbird` command yourself. The full steps are in [SKILL.md](SKILL.md).

## How it works

```
 your app (unchanged) ── Anthropic / OpenAI / Azure SDK
        │
        │  hook: is the mockingbird server running?
        ├── no  ─► the real API
        └── yes ─► mockingbird on 127.0.0.1:8787
                     1. seen this exact call before? replay the saved answer
                     2. otherwise ask the model: one lean `claude -p` call
                     3. check the reply, and retry once if it's malformed
                     4. return it as a real API response, JSON or streaming
```

- **No code changes.** Your shell sets `PYTHONPATH`, so every Python app loads a small startup hook. Before each AI call, the hook checks whether the server is running and, if it is, sends the call there. Claude Code itself isn't Python, so it's never redirected.
- **The model writes only the content.** mockingbird builds the exact API response: ids, stop reasons, tool calls and streaming events.
- **Other languages:** point the SDK at the server instead, with `ANTHROPIC_BASE_URL=http://127.0.0.1:8787` or `OPENAI_BASE_URL=http://127.0.0.1:8787/v1`.

## Cache and history

- **Saved answers** live in `~/.local/state/mockingbird/cache/`, one cache for all projects. Delete one on the Cache tab, or all of them with `mockingbird cache --purge`. `BRIDGE_CACHE=off` turns the cache off. `BRIDGE_CACHE=./fixtures` keeps a separate cache in that folder, for example to commit for CI.
- **Call history** lives in `~/.local/state/mockingbird/calls.db`. It powers the dashboard and keeps only metadata: time, model, outcome, duration, project, and tool names. It never stores prompts or answers.

## Speed

Measured on 14/11/2026: a demo app using the official `anthropic` and `openai` SDKs, with six tests making 8 AI calls (forced tool choice, two multi-turn tool loops, JSON mode and streaming). Sonnet answered at high effort.

| Run | Result | Time |
|---|---|---|
| First run, every answer fresh | 6 of 6 passed, about 7 s per call | 62 s |
| Second run, every answer replayed | 6 of 6 passed | **under 2 s** |

## Limits

| What | What happens |
|---|---|
| Answers come from a stand-in model | Fresh answers vary and only imitate your real model. Test your app's flow and parsing, not answer quality |
| Unsupported endpoints | Responses API, `count_tokens`, embeddings, Gemini, Bedrock and Vertex get a clear 501 error |
| Unusual requests | Images arrive as data the model can't see, very long contexts can overflow it, and token usage is always 0 |
| Calls that change every run | Requests containing timestamps or random ids never match a saved answer, so they're always asked fresh |
| Apps not started from a terminal | IDEs, services and containers don't read your shell files. Use `mockingbird hook install /path/to/python`, or the env vars above |

## Safety and data

- **Never install the hook where production code runs.** While a server runs there, its AI calls would get mockingbird's answers.
- The server only accepts connections from your own machine.
- Prompts go to the model provider behind the Claude Code CLI. Saved answers include the start of what was asked, so use synthetic test data, never real personal data.

## Update and uninstall

```bash
mockingbird update               # installs the newest release from GitHub
mockingbird uninstall            # asks first, then removes everything; keeps history and saved answers
mockingbird uninstall --purge    # also deletes your history and saved answers
```

Uninstall restores `~/.bashrc` and `~/.zshrc` exactly as they were. If the `mockingbird` command itself is broken, run the uninstaller straight from GitHub:

```bash
curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/main/uninstall.sh | bash -s -- --yes
```
