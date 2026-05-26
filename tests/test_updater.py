"""Unit tests for projectdropit.updater.

Tests are fully offline — _fetch_latest is monkey-patched so no real
network requests are made.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import projectdropit.updater as updater


def _reset():
    """Reset module state between tests."""
    updater._state.update(
        {"latest": None, "checked": False, "running": False, "error": None}
    )


# ---------------------------------------------------------------------------
# _parse_version
# ---------------------------------------------------------------------------

def test_parse_version_basic():
    assert updater._parse_version("1.0.1") == (1, 0, 1)
    assert updater._parse_version("2.0.0") == (2, 0, 0)
    assert updater._parse_version("0.9.99") == (0, 9, 99)


def test_parse_version_strips_prerelease():
    assert updater._parse_version("2.0.0-rc1") == (2, 0, 0)
    assert updater._parse_version("1.0.1a1") == (1, 0, 1)
    assert updater._parse_version("1.0.1b2") == (1, 0, 1)
    assert updater._parse_version("1.0.1+local") == (1, 0, 1)


def test_parse_version_empty_and_invalid():
    assert updater._parse_version("") == (0,)
    assert updater._parse_version("invalid") == (0,)
    assert updater._parse_version("   ") == (0,)


def test_parse_version_ordering():
    assert updater._parse_version("1.0.2") > updater._parse_version("1.0.1")
    assert updater._parse_version("2.0.0") > updater._parse_version("1.9.9")
    assert updater._parse_version("1.0.1") == updater._parse_version("1.0.1")


# ---------------------------------------------------------------------------
# check_async — idempotency
# ---------------------------------------------------------------------------

def test_check_async_idempotent(_monkeypatch_fetch=None):
    _reset()
    t1 = updater.check_async()
    t2 = updater.check_async()
    assert t1 is not None, "first call should return a thread"
    assert t2 is None, "second call should be a no-op"
    # clean up
    if t1:
        t1.join(timeout=6.0)


def test_check_async_noop_when_already_checked():
    _reset()
    updater._state["checked"] = True
    result = updater.check_async()
    assert result is None, "should be no-op when already checked"


# ---------------------------------------------------------------------------
# check_sync — always does a fresh check
# ---------------------------------------------------------------------------

def test_check_sync_up_to_date(monkeypatch):
    _reset()
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout: (None, None))
    result = updater.check_sync()
    assert result is None
    assert updater.has_checked()
    assert updater.latest_if_newer() is None
    assert updater.last_error() is None


def test_check_sync_finds_newer(monkeypatch):
    _reset()
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout: ("99.0.0", None))
    result = updater.check_sync()
    assert result == "99.0.0"
    assert updater.latest_if_newer() == "99.0.0"
    assert updater.has_checked()
    assert updater.last_error() is None


def test_check_sync_resets_stale_result(monkeypatch):
    """check_sync must overwrite a stale 'latest' from a previous check."""
    _reset()
    updater._state["latest"] = "99.0.0"
    updater._state["checked"] = True
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout: (None, None))
    result = updater.check_sync()
    assert result is None
    assert updater.latest_if_newer() is None


def test_check_sync_ignores_same_version(monkeypatch):
    """check_sync must not report the current version as an update."""
    _reset()
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout: (updater.__version__, None))
    result = updater.check_sync()
    assert result is None


def test_check_sync_handles_network_error(monkeypatch):
    """check_sync must not raise on network failure, and must set last_error."""
    _reset()

    def _fail(timeout):
        raise ConnectionError("no network")

    monkeypatch.setattr(updater, "_fetch_latest", _fail)
    result = updater.check_sync()
    assert result is None
    assert updater.has_checked()
    # last_error must be set so the CLI can show a proper message
    assert updater.last_error() is not None


def test_check_sync_works_after_startup_check(monkeypatch):
    """The core bug: manual check must work even after startup check already ran."""
    _reset()
    # Simulate startup check completing with no update found.
    updater._state.update({"latest": None, "checked": True, "running": False})
    # Now a new version is available — check_sync must detect it.
    monkeypatch.setattr(updater, "_fetch_latest", lambda timeout: ("2.0.0", None))
    result = updater.check_sync()
    assert result == "2.0.0", (
        "check_sync must perform a fresh check even after startup check ran"
    )


# ---------------------------------------------------------------------------
# pytest entry points (also runnable directly)
# ---------------------------------------------------------------------------

def main() -> int:
    import traceback
    tests = [
        test_parse_version_basic,
        test_parse_version_strips_prerelease,
        test_parse_version_empty_and_invalid,
        test_parse_version_ordering,
        test_check_async_idempotent,
        test_check_async_noop_when_already_checked,
    ]
    failures = 0
    for fn in tests:
        try:
            fn()
            print(f"OK  {fn.__name__}")
        except Exception:
            print(f"FAIL  {fn.__name__}")
            traceback.print_exc()
            failures += 1

    # Tests that need monkeypatching — run inline
    original = updater._fetch_latest
    try:
        for label, patch, expected in [
            ("check_sync up-to-date",       lambda t: None,    None),
            ("check_sync finds newer",       lambda t: "99.0.0", "99.0.0"),
            ("check_sync network error",     _make_raiser(),    None),
        ]:
            _reset()
            updater._fetch_latest = patch
            try:
                result = updater.check_sync()
                assert result == expected, f"got {result!r}, expected {expected!r}"
                print(f"OK  {label}")
            except Exception:
                print(f"FAIL  {label}")
                traceback.print_exc()
                failures += 1

        # Core bug regression
        _reset()
        updater._state.update({"latest": None, "checked": True, "running": False})
        updater._fetch_latest = lambda t: "2.0.0"
        result = updater.check_sync()
        if result == "2.0.0":
            print("OK  check_sync works after startup check (core bug regression)")
        else:
            print(f"FAIL  core bug regression: got {result!r}")
            failures += 1
    finally:
        updater._fetch_latest = original

    if failures:
        print(f"\n{failures} failure(s)")
        return 1
    print(f"\nAll updater tests passed.")
    return 0


def _make_raiser():
    def _fail(timeout):
        raise ConnectionError("no network")
    return _fail


if __name__ == "__main__":
    sys.exit(main())
