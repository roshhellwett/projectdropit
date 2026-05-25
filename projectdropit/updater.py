"""Background PyPI update check.

Runs in a daemon thread with a hard timeout so a slow network never blocks
launch. Reports a newer version (if any) via ``latest_if_newer()``.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from typing import Optional

from . import __version__

_PYPI_URL = "https://pypi.org/pypi/projectdropit/json"
_TIMEOUT_S = 4.0

_state = {"latest": None, "checked": False, "error": None}
_lock = threading.Lock()


def _parse_version(v: str) -> tuple:
    """Loose semver compare. Strips suffixes like '-rc1', 'a1', 'b2', '+meta'."""
    v = v.strip()
    # split off any pre-release / build metadata
    v = re.split(r"[+\-]", v, maxsplit=1)[0]
    out = []
    for part in v.split("."):
        m = re.match(r"^(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out)


def _run() -> None:
    try:
        req = urllib.request.Request(
            _PYPI_URL,
            headers={"User-Agent": f"projectdropit/{__version__} (update-check)"},
        )
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        latest = (data.get("info") or {}).get("version")
        if latest and _parse_version(latest) > _parse_version(__version__):
            with _lock:
                _state["latest"] = latest
    except Exception as e:  # offline, 404 (not yet published), etc. — silent
        with _lock:
            _state["error"] = str(e)
    finally:
        with _lock:
            _state["checked"] = True


def check_async() -> threading.Thread:
    """Kick off a single background check. Idempotent."""
    t = threading.Thread(target=_run, name="pdit-updater", daemon=True)
    t.start()
    return t


def latest_if_newer() -> Optional[str]:
    with _lock:
        return _state["latest"]


def has_checked() -> bool:
    with _lock:
        return _state["checked"]
