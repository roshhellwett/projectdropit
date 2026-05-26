"""Shared pytest configuration for projectdropit tests."""
from __future__ import annotations

import sys
from pathlib import Path

# Ensure the repo root is on sys.path so tests work both installed and from source.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
