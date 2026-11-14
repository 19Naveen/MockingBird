"""Put the checkout's ``src/`` directory on sys.path before importing mockingbird.

Each test module imports this first, so the suite runs the same from the
repository root, from ``tests/``, or through ``python -m unittest discover``.
"""

import sys
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
