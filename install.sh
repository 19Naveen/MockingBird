#!/usr/bin/env bash
# Install mockingbird.
#   curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/v1.12.0/install.sh | bash
# Env: MOCKINGBIRD_BASE    (raw URL to download from; defaults to this release's tag)
#      MOCKINGBIRD_SRC     (local folder to copy from instead of downloading)
#      MOCKINGBIRD_NO_HOOK (set to 1 to skip the hook, e.g. on any machine that runs production code)
set -euo pipefail

NAME=mockingbird
VERSION=1.12.0
REPO_URL=https://github.com/19Naveen/MockingBird
BASE="${MOCKINGBIRD_BASE:-https://raw.githubusercontent.com/19Naveen/MockingBird/v$VERSION}"
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
for f in SKILL.md bridge.py dashboard.py mockingbird_hook.py uninstall.sh; do
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
doing "Running the self-check"
limit=""
command -v timeout >/dev/null && limit="timeout 180"
$limit python3 "$DEST/bridge.py" selftest </dev/null >/dev/null 2>&1 \
  || fail "The self-check failed. See why with: python3 $DEST/bridge.py selftest"
done_ "Self-check passed"

if [ -z "${MOCKINGBIRD_NO_HOOK:-}" ]; then
  python3 "$DEST/bridge.py" hook install </dev/null >/dev/null
  shells=""
  for rc in .bashrc .zshrc; do
    grep -q '# >>> mockingbird >>>' "$HOME/$rc" 2>/dev/null && shells="$shells${shells:+ and }~/$rc"
  done
  done_ "Hook added to ${shells:-your shell}"
else
  note "Hook skipped (MOCKINGBIRD_NO_HOOK is set)"
fi

mkdir -p "$HOME/.local/bin"
ln -sfn "$DEST/bridge.py" "$HOME/.local/bin/$NAME"
done_ "Command ${B}mockingbird${X} added to ~/.local/bin"
case ":$PATH:" in *":$HOME/.local/bin:"*) ;; *) note "Add ~/.local/bin to your PATH to use it" ;; esac

done_ "Skill in ~/.agents/skills, the shared folder Codex and opencode read"

cat <<EOF

  ${B}Next${X}
    1. Open a new terminal
    2. Run ${B}mockingbird serve${X}
    3. Run your app or its tests in another terminal

  ${D}Docs  $REPO_URL${X}

EOF
