#!/usr/bin/env bash
# Remove mockingbird from this machine. Keeps your call history and saved answers unless you add --purge.
#   mockingbird uninstall [--purge] [--yes]
#   curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/main/uninstall.sh | bash -s -- --yes
set -euo pipefail

NAME=mockingbird
DEST="$HOME/.agents/skills/$NAME"
STATE_DIR="${XDG_STATE_HOME:-$HOME/.local/state}/$NAME"
PURGE=0
YES=0
for arg in "$@"; do
  case "$arg" in
    --purge) PURGE=1 ;;
    --yes|-y) YES=1 ;;
    *) echo "unknown option: $arg (use --purge, --yes)" >&2; exit 2 ;;
  esac
done

# A running server keeps hooked apps pointed at it, so it has to stop first.
if [ -f "$HOME/.mockingbird/active.json" ]; then
  pid=$(python3 -c 'import json, sys; print(json.load(open(sys.argv[1]))["pid"])' "$HOME/.mockingbird/active.json" 2>/dev/null || true)
  if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
    echo "mockingbird is running (pid $pid). Press q on its dashboard to stop it, then run this again." >&2
    exit 1
  fi
fi

echo "This removes the mockingbird command, the hook line in ~/.bashrc and ~/.zshrc,"
echo "and the skill in ~/.agents/skills."
if [ "$PURGE" = 1 ]; then
  echo "It also deletes your call history and saved answers in $STATE_DIR."
else
  echo "Your call history and saved answers stay in $STATE_DIR. Add --purge to delete them too."
fi
if [ "$YES" = 0 ]; then
  if [ -t 0 ]; then
    read -r -p "Remove mockingbird? [y/N] " answer
    case "$answer" in y|Y|yes) ;; *) echo "Nothing was removed."; exit 0 ;; esac
  else
    echo "Run it again with --yes to confirm." >&2
    exit 1
  fi
fi

# Take the hook line out of the shell files, leaving them exactly as they were before the install.
python3 - "$HOME/.bashrc" "$HOME/.zshrc" <<'PY'
import re, sys
from pathlib import Path
for name in sys.argv[1:]:
    path = Path(name)
    if not path.exists():
        continue
    text = path.read_text()
    restored = re.sub(r"\n?# >>> mockingbird >>>.*?# <<< mockingbird <<<\n", "", text, flags=re.S)
    if restored != text:
        path.write_text(restored)
        print(f"restored {path}")
PY

rm -rf "$HOME/.mockingbird"
if [ -L "$HOME/.local/bin/$NAME" ] || grep -q "mockingbird launcher" "$HOME/.local/bin/$NAME" 2>/dev/null; then
  rm "$HOME/.local/bin/$NAME"
  echo "removed ~/.local/bin/$NAME"
fi
rm -rf "$DEST"
if [ "$PURGE" = 1 ]; then
  rm -rf "$STATE_DIR"
  echo "deleted $STATE_DIR"
fi

echo "mockingbird is removed. Open a new terminal so your shell drops the hook."
echo "If you ran 'mockingbird hook install <python>' for a single environment, that hook stays but does nothing"
echo "now. Delete mockingbird_hook.py and mockingbird_hook.pth from its site-packages to remove it."
