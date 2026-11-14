#!/usr/bin/env python3
"""Installed entry point for mockingbird. The implementation lives in the mockingbird/ package."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # the package sits beside us, even via the ~/.local/bin link
from mockingbird.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
