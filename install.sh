#!/usr/bin/env bash
# Install mockingbird.
#   curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/main/install.sh | bash
# Env: MOCKINGBIRD_BASE    (raw URL to download from; defaults to main; update pins it to a release tag)
#      MOCKINGBIRD_SRC     (local folder to copy from instead of downloading)
#      MOCKINGBIRD_INSTALL_HOOK (set to 1 to install the optional, opt-in shell hook)
set -euo pipefail

NAME=mockingbird
VERSION=1.14.0
REPO_URL=https://github.com/19Naveen/MockingBird
BASE="${MOCKINGBIRD_BASE:-https://raw.githubusercontent.com/19Naveen/MockingBird/main}"
# The skill lives only in the shared skills folder; nothing goes into agent-specific folders.
DEST="$HOME/.agents/skills/$NAME"

if [ -t 1 ]; then
  B=$'\e[1m'; D=$'\e[2m'; G=$'\e[32m'; Y=$'\e[33m'; R=$'\e[31m'; C=$'\e[36m'; X=$'\e[0m'
else
  B=; D=; G=; Y=; R=; C=; X=
fi
doing() { [ -t 1 ] && printf '  %s…%s %s' "$D" "$X" "$1"; return 0; }
done_() { [ -t 1 ] && printf '\r\e[2K'; printf '  %s✓%s %s\n' "$G" "$X" "$1"; }
note()  { printf '  %s!%s %s\n' "$Y" "$X" "$1"; }
fail()  { [ -t 1 ] && printf '\r\e[2K'; printf '  %s✗%s %s\n' "$R" "$X" "$1" >&2; exit 1; }

command -v python3 >/dev/null || fail "mockingbird needs python3 (3.9 or newer). Install it, then run this again."

cat <<EOF
${C}                         __   _             __    _          __
   ____ ___  ____  _____/ /__(_)___  ____ _/ /_  (_)________/ /
  / __ \`__ \\/ __ \\/ ___/ //_/ / __ \\/ __ \`/ __ \\/ / ___/ __  /
 / / / / / / /_/ / /__/ ,< / / / / / /_/ / /_/ / / /  / /_/ /
/_/ /_/ /_/\\____/\\___/_/|_/_/_/ /_/\\__, /_.___/_/_/   \\__,_/
                                  /____/${X}

  ${B}Installing mockingbird ${VERSION}${X}

EOF

mkdir -p "$DEST"
doing "Downloading"
FILES="SKILL.md bridge.py mockingbird/__init__.py mockingbird/state.py mockingbird/log.py mockingbird/store.py
mockingbird/responses.py mockingbird/agent.py mockingbird/answers.py mockingbird/server.py mockingbird/cli.py
mockingbird/dashboard.py mockingbird/attachments.py mockingbird/providers/__init__.py
mockingbird/providers/bedrock.py mockingbird/providers/google.py mockingbird_hook.py uninstall.sh LICENSE"
for f in $FILES; do
  mkdir -p "$DEST/$(dirname "$f")"
  if [ -n "${MOCKINGBIRD_SRC:-}" ]; then
    cp "$MOCKINGBIRD_SRC/$f" "$DEST/$f.tmp"
  elif ! curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 "$BASE/$f" -o "$DEST/$f.tmp"; then
    rm -f "$DEST/$f.tmp"
    fail "Couldn't download $f from GitHub. Check your connection, then run this again."
  fi
  mv "$DEST/$f.tmp" "$DEST/$f"   # never leave a half-downloaded file in place
done
chmod +x "$DEST/bridge.py" "$DEST/uninstall.sh"
done_ "Downloaded"

# Child steps get no stdin: under `curl | bash` stdin is the rest of this script, and nothing may wait on a key.
doing "Checking Python compatibility"
python3 "$DEST/bridge.py" version </dev/null \
  || fail "Could not start mockingbird. Python 3.9 or newer is required."
done_ "Startup check passed (the test suite runs in CI)"

if [ "${MOCKINGBIRD_INSTALL_HOOK:-}" = 1 ] && [ -z "${MOCKINGBIRD_NO_HOOK:-}" ]; then
  python3 "$DEST/bridge.py" hook install </dev/null >/dev/null
  shells=""
  for rc in .bashrc .zshrc; do
    grep -q '# >>> mockingbird >>>' "$HOME/$rc" 2>/dev/null && shells="$shells${shells:+ and }~/$rc"
  done
  done_ "Hook added to ${shells:-your shell}"
else
  # Refresh any older global hook so an upgrade also gets the opt-in guard.
  if [ -f "$HOME/.mockingbird/hook/sitecustomize.py" ]; then
    python3 "$DEST/bridge.py" hook install </dev/null >/dev/null
  fi
  note "Hook is optional; use SDK base URLs, or run mockingbird hook install"
fi

mkdir -p "$HOME/.local/bin"
# A launcher, not a symlink: the command keeps working even if bridge.py loses its execute bit.
rm -f "$HOME/.local/bin/$NAME"
cat > "$HOME/.local/bin/$NAME" <<EOF
#!/bin/sh
# mockingbird launcher (written by install.sh; uninstall removes it)
exec python3 "$DEST/bridge.py" "\$@"
EOF
chmod +x "$HOME/.local/bin/$NAME"
done_ "Command ${B}mockingbird${X} added to ~/.local/bin"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) note "Add ~/.local/bin to your PATH to use it" ;; esac

done_ "Skill in ~/.agents/skills, the shared folder Codex and opencode read"

cat <<EOF

  ${B}Next${X}
    1. Run ${B}mockingbird serve${X}
    2. Point your test SDK base URL at http://127.0.0.1:8787 (OpenAI: /v1)
    3. Run your tests with a dummy API key (see README)

  ${D}Docs  $REPO_URL${X}

EOF
