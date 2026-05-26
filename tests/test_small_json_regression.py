"""Regression test for a real bug: the old receiver loop misclassified any
encrypted frame that started with b'{' and was < 256 bytes as the protocol
'done' control message, which would silently truncate small JSON files.
"""
from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projectdropit.transfer import STATUS_DONE, ReceiverServer, send_file


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pdit_regress_"))
    src = tmp / "tiny.json"
    payload = b'{"hello":"world","n":42}'
    src.write_bytes(payload)

    dst = tmp / "downloads"
    dst.mkdir()
    rx = ReceiverServer(get_download_dir=lambda: dst, get_device_name=lambda: "r")
    rx.start()
    rx.on_offer = lambda t: rx.decide(t.id, True)

    result = send_file("127.0.0.1", rx.port, src, "s")
    deadline = time.time() + 5
    while time.time() < deadline:
        h = rx.history()
        if h and h[-1].status == STATUS_DONE:
            break
        time.sleep(0.05)
    rx.stop()

    if not result.get("ok"):
        print(f"FAIL: send_file returned {result}")
        return 1
    h = rx.history()
    if not h or h[-1].status != STATUS_DONE:
        print(f"FAIL: receiver status = {h[-1].status if h else 'none'}")
        return 1
    saved = h[-1].save_path
    got = saved.read_bytes()
    if got != payload:
        print(f"FAIL: payload mismatch  got={got!r}  want={payload!r}")
        return 1
    if not h[-1].verified:
        print("FAIL: not marked verified")
        return 1
    print(f"OK  small JSON ({len(payload)} bytes) survived round-trip and was verified.")
    return 0


if __name__ == "__main__":
    sys.exit(main())


# ---------------------------------------------------------------------------
# pytest-compatible entry point
# ---------------------------------------------------------------------------

def test_small_json_regression() -> None:
    """Small JSON file regression test under pytest."""
    assert main() == 0, "small JSON regression test failed"
