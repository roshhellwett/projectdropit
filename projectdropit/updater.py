"""Background PyPI update check.

Two entry points:
- ``check_async()``  — fire-and-forget daemon thread (used at startup).
- ``check_sync(timeout)`` — blocking fresh check (used by the Settings menu).

``latest_if_newer()`` returns the latest PyPI version string if it is newer
than the running version, or ``None`` if up-to-date / not yet checked.
``has_checked()`` returns True once any check has completed.
"""
from __future__ import annotations

import json
import re
import threading
import urllib.request
from typing import Optional

from . import __version__

_PYPI_URL = "https://pypi.org/pypi/projectdropit/json"
_ASYNC_TIMEOUT_S = 5.0   # network timeout for the background startup check
_SYNC_TIMEOUT_S  = 8.0   # network timeout for the manual (blocking) check

# Module-level state — protected by _lock.
_state = {
    "latest":  None,   # str | None — newer version found on PyPI
    "checked": False,  # True once any check has completed (success or error)
    "running": False,  # True while a background thread is in-flight
    "error":   None,   # last error string, for diagnostics
}
_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_version(v: str) -> tuple:
    """Loose semver compare. Strips suffixes like '-rc1', 'a1', 'b2', '+meta'."""
    v = v.strip()
    if not v:
        return (0,)
    # strip pre-release / build metadata
    v = re.split(r"[+\-]", v, maxsplit=1)[0]
    out = []
    for part in v.split("."):
        m = re.match(r"^(\d+)", part)
        out.append(int(m.group(1)) if m else 0)
    return tuple(out) if out else (0,)


def _fetch_latest(timeout: float) -> Optional[str]:
    """Hit PyPI and return the latest version string, or None on any error."""
    try:
        req = urllib.request.Request(
            _PYPI_URL,
            headers={"User-Agent": f"projectdropit/{__version__} (update-check)"},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return (data.get("info") or {}).get("version") or None
    except Exception:
        return None


def _apply_result(latest_version: Optional[str], error: Optional[str] = None) -> None:
    """Write a completed check result into _state (must be called with _lock held)."""
    if latest_version and _parse_version(latest_version) > _parse_version(__version__):
        _state["latest"] = latest_version
    _state["error"]   = error
    _state["checked"] = True
    _state["running"] = False


# ---------------------------------------------------------------------------
# Background (async) check — used at startup
# ---------------------------------------------------------------------------

def _run_async() -> None:
    latest = _fetch_latest(_ASYNC_TIMEOUT_S)
    with _lock:
        _apply_result(latest)


def check_async() -> Optional[threading.Thread]:
    """Kick off a background update check. Idempotent — no-op if already running or done."""
    with _lock:
        if _state["checked"] or _state["running"]:
            return None
        _state["running"] = True
    t = threading.Thread(target=_run_async, name="pdit-updater", daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# Foreground (sync) check — used by the Settings menu
# ---------------------------------------------------------------------------

def check_sync(timeout: float = _SYNC_TIMEOUT_S) -> Optional[str]:
    """Perform a fresh, blocking PyPI check and return the newer version or None.

    Always hits the network regardless of whether a previous check has run.
    Updates the shared state so ``latest_if_newer()`` reflects the new result.
    """
    # Wait for any in-flight background check to finish first so we don't
    # race against it writing to _state.
    _wait_for_running(timeout=3.0)

    try:
        latest = _fetch_latest(timeout)
    except Exception:
        latest = None

    with _lock:
        # Reset so the new result is authoritative.
        _state["latest"] = None
        _apply_result(latest)
    return _state["latest"]


def _wait_for_running(timeout: float) -> None:
    """Block until any in-flight background thread finishes (or timeout)."""
    import time
    deadline = time.monotonic() + timeout
    while True:
        with _lock:
            if not _state["running"]:
                return
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        threading.Event().wait(min(0.1, remaining))


# ---------------------------------------------------------------------------
# Accessors
# ---------------------------------------------------------------------------

def latest_if_newer() -> Optional[str]:
    """Return the newer PyPI version string, or None if up-to-date / not checked."""
    with _lock:
        return _state["latest"]


def has_checked() -> bool:
    """True once any check (async or sync) has completed."""
    with _lock:
        return _state["checked"]


def last_error() -> Optional[str]:
    """Return the last network/parse error string, or None if no error."""
    with _lock:
        return _state["error"]
