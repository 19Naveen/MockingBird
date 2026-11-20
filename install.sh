#!/usr/bin/env bash
# Install mockingbird.
#   curl -fsSL https://raw.githubusercontent.com/19Naveen/MockingBird/main/install.sh | bash
# Env: MOCKINGBIRD_BASE    (raw URL to download from; defaults to main; update pins it to a release tag)
#      MOCKINGBIRD_SRC     (local folder to copy from instead of downloading)
set -euo pipefail

NAME=mockingbird
VERSION=1.14.2
REPO=19Naveen/MockingBird
REPO_URL="https://github.com/$REPO"
RAW="https://raw.githubusercontent.com/$REPO"
BASE="${MOCKINGBIRD_BASE:-$RAW/main}"
SOURCE="${MOCKINGBIRD_SRC:-}"
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

FILES="SKILL.md bridge.py pyproject.toml
src/mockingbird/__init__.py src/mockingbird/__main__.py src/mockingbird/cli.py
src/mockingbird/core/__init__.py src/mockingbird/core/state.py src/mockingbird/core/log.py
src/mockingbird/core/store.py src/mockingbird/core/agent.py src/mockingbird/core/answers.py
src/mockingbird/api/__init__.py src/mockingbird/api/responses.py src/mockingbird/api/server.py
src/mockingbird/api/providers/__init__.py src/mockingbird/api/providers/bedrock.py
src/mockingbird/api/providers/google.py src/mockingbird/media/__init__.py
src/mockingbird/media/attachments.py src/mockingbird/ui/__init__.py
src/mockingbird/ui/dashboard.py mockingbird_hook.py uninstall.sh LICENSE"

# The standard GitHub installer and updater use one archive; custom mirrors still
# serve raw files. Keep the archive on the same branch or tag as the installer.
ref="${BASE#"$RAW/"}"
archive_ref=
if [ "$BASE" = "$RAW/main" ]; then
  archive_ref=refs/heads/main
elif [[ "$BASE" == "$RAW/"* && "$ref" =~ ^v[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
  archive_ref="refs/tags/$ref"
fi
if [ -z "$SOURCE" ] && [ -n "$archive_ref" ]; then
  doing "Downloading archive"
  bundle=$(mktemp -d)
  trap 'rm -rf -- "$bundle"' EXIT
  if curl -fsSL --connect-timeout 5 --max-time 30 \
    "https://codeload.github.com/$REPO/tar.gz/$archive_ref" -o "$bundle/repo.tar.gz"; then
    if ! python3 -I - "$bundle" "$VERSION" $FILES <<'PY'
import shutil
import sys
import tarfile
from pathlib import Path

staging = Path(sys.argv[1])
version = sys.argv[2]
try:
    with tarfile.open(staging / "repo.tar.gz", "r:gz") as archive:
        root = archive.next()
        if root is None or not root.isdir():
            raise ValueError("missing source directory")

        def open_file(name):
            entry = archive.getmember(f"{root.name}/{name}")
            if not entry.isfile():
                raise ValueError(f"{name} is not a regular file")
            return archive.extractfile(entry)

        with open_file("install.sh") as installer:
            if f"VERSION={version}".encode() not in installer.read().splitlines():
                raise ValueError("archive version does not match the installer")
        # Only copy the listed files; never extract archive paths or links.
        for name in sys.argv[3:]:
            dest = staging / name
            dest.parent.mkdir(parents=True, exist_ok=True)
            with open_file(name) as source, dest.open("wb") as target:
                shutil.copyfileobj(source, target)
except (OSError, EOFError, KeyError, ValueError, tarfile.TarError) as exc:
    sys.exit(f"Could not unpack the MockingBird archive: {exc}")
PY
    then
      fail "Installation files were not changed. Try again or install from a local checkout."
    fi
    SOURCE="$bundle"
    done_ "Downloaded archive"
  else
    [ ! -t 1 ] || printf '\r\e[2K'
    note "Archive unavailable; downloading files individually"
  fi
fi

if [ -n "$SOURCE" ]; then doing "Installing files"; else doing "Downloading files"; fi
mkdir -p "$DEST"
for f in $FILES; do
  mkdir -p "$DEST/$(dirname "$f")"
  if [ -n "$SOURCE" ]; then
    cp "$SOURCE/$f" "$DEST/$f.tmp"
  elif ! curl -fsSL --connect-timeout 15 --max-time 120 --retry 2 "$BASE/$f" -o "$DEST/$f.tmp"; then
    rm -f "$DEST/$f.tmp"
    fail "Couldn't download $f from GitHub. Check your connection, then run this again."
  fi
  mv "$DEST/$f.tmp" "$DEST/$f"   # never leave a half-downloaded file in place
done
chmod +x "$DEST/bridge.py" "$DEST/uninstall.sh"
done_ "Installed files"

# Child steps get no stdin: under `curl | bash` stdin is the rest of this script, and nothing may wait on a key.
doing "Checking Python compatibility"
installed_version=$(python3 "$DEST/bridge.py" version </dev/null) \
  || fail "Could not start mockingbird. Python 3.9 or newer is required."
done_ "Startup check passed (v$installed_version; the test suite runs in CI)"

# The coding agent that answers requests with no --model: the first one installed (Claude Code, Codex, opencode),
# unless a choice is already saved. Never prompts: nothing here may wait on a key.
agent_line=$(python3 "$DEST/bridge.py" agent --default </dev/null) || fail "Could not set the coding agent."
case "$agent_line" in
  "No coding agent"*) note "$agent_line" ;;
  *) done_ "$agent_line" ;;
esac

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
    In a project with Mockingbird integration: run ${B}mockingbird${X} from its root.
    For a private test run: ${B}mockingbird run -- <test command>${X}.
    For manual SDK routing: run ${B}mockingbird serve${X} and see the README.

  ${D}Docs  $REPO_URL${X}

EOF
