"""stderr logging that never fights the dashboard for the terminal."""

import sys

from . import state


def log(msg):
    if not state.QUIET:  # the dashboard owns the screen; stray stderr would tear it
        print(f"[bridge] {'activity (details redacted)' if state.REDACT else msg}", file=sys.stderr)
