#!/usr/bin/env bash
# Install the mockingbird skill for Claude Code, Codex and opencode.
#   curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/v1.9.0/install.sh | bash
# Env: MOCKINGBIRD_BASE    (raw URL to download from; defaults to this release's tag)
#      MOCKINGBIRD_SRC     (local folder to copy from instead of downloading)
#      MOCKINGBIRD_NO_HOOK (set to 1 to skip the global hook, e.g. on any machine that runs production code)
set -euo pipefail

NAME=mockingbird
VERSION=1.9.0
BASE="${MOCKINGBIRD_BASE:-https://raw.githubusercontent.com/19Naveen/MockingBird/v$VERSION}"
DEST="$HOME/.agents/skills/$NAME"

command -v python3 >/dev/null || { echo "$NAME needs python3 (3.9+)" >&2; exit 1; }
mkdir -p "$DEST"

for f in SKILL.md bridge.py dashboard.py mockingbird_hook.py uninstall.sh; do
  if [ -n "${MOCKINGBIRD_SRC:-}" ]; then cp "$MOCKINGBIRD_SRC/$f" "$DEST/$f.tmp"
  else curl -fsSL "$BASE/$f" -o "$DEST/$f.tmp"; fi
  mv "$DEST/$f.tmp" "$DEST/$f"   # never leave a half-downloaded file in place
done
chmod +x "$DEST/bridge.py" "$DEST/uninstall.sh"

python3 "$DEST/bridge.py" selftest >/dev/null 2>&1 || { echo "$NAME: selftest failed, run: python3 $DEST/bridge.py selftest" >&2; exit 1; }

# Global hook: every Python started from your shell routes LLM calls to mockingbird while its server runs.
if [ -z "${MOCKINGBIRD_NO_HOOK:-}" ]; then
  python3 "$DEST/bridge.py" hook install
else
  echo "skipped global hook (MOCKINGBIRD_NO_HOOK set)"
fi

# `mockingbird` command, so the tool works without any agent.
mkdir -p "$HOME/.local/bin"
ln -sfn "$DEST/bridge.py" "$HOME/.local/bin/$NAME"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) echo "add ~/.local/bin to PATH to use the '$NAME' command" >&2 ;; esac

# Link into every agent that is installed on this machine.
for d in "$HOME/.claude" "$HOME/.codex" "$HOME/.config/opencode"; do
  [ -d "$d" ] || continue
  link="$d/skills/$NAME"
  mkdir -p "$d/skills"
  if [ -e "$link" ] && [ ! -L "$link" ]; then echo "skip $link: a real folder is there, remove it to link" >&2; continue; fi
  ln -sfn "$DEST" "$link"
  echo "linked $link"
done

# opencode: slash command wrapper so /mockingbird works there too.
if [ -d "$HOME/.config/opencode" ]; then
  mkdir -p "$HOME/.config/opencode/commands"
  cat > "$HOME/.config/opencode/commands/$NAME.md" <<'EOF'
---
description: Run AI tests with LLM calls answered by a subagent. Args: model[effort] [test command]
---
Load the `mockingbird` skill and follow it. Arguments: $ARGUMENTS
EOF
fi

cat <<'EOF'
                         __   _             __    _          __
   ____ ___  ____  _____/ /__(_)___  ____ _/ /_  (_)________/ /
  / __ `__ \/ __ \/ ___/ //_/ / __ \/ __ `/ __ \/ / ___/ __  /
 / / / / / / /_/ / /__/ ,< / / / / / /_/ / /_/ / / /  / /_/ /
/_/ /_/ /_/\____/\___/_/|_/_/_/ /_/\__, /_.___/_/_/   \__,_/
                                  /____/
EOF
echo "installed $NAME v$(python3 "$DEST/bridge.py" version) to $DEST"
echo "open a new terminal, then:  $NAME serve --model sonnet[high]   (Ctrl+C = back to the real API)"
echo "or in an agent:  /$NAME sonnet[high] <test command>   (Codex: \$$NAME ...)"
