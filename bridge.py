#!/usr/bin/env python3
"""Installed entry point for mockingbird. The implementation lives in src/mockingbird/."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT / "src"))  # the package sits in src/, even via the ~/.local/bin link
from mockingbird.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
