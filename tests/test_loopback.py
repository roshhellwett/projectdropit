"""Loopback smoke test: spin up a ReceiverServer, push a file through `send_file`,
auto-accept the offer, and verify byte-for-byte equality.

Run:  python -m tests.test_loopback
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

# Allow running from repo root without install.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from projectdropit.transfer import (  # noqa: E402
    STATUS_DONE,
    ReceiverServer,
    send_file,
)


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="pdit_test_"))
    src = tmp / "payload.bin"
    dst_dir = tmp / "downloads"
    dst_dir.mkdir()
    # ~1.5 MB of pseudo-random bytes (multiple chunks)
    src.write_bytes(os.urandom(1_500_000))
    src_hash = _sha256(src)

    rx = ReceiverServer(
        get_download_dir=lambda: dst_dir,
        get_device_name=lambda: "loopback-receiver",
    )
    rx.start()

    # auto-accept any incoming offer
    def on_offer(t):
        rx.decide(t.id, True)

    rx.on_offer = on_offer

    result = send_file(
        peer_addr="127.0.0.1",
        peer_port=rx.port,
        filepath=src,
        sender_name="loopback-sender",
    )
    if not result.get("ok"):
        print(f"FAIL: send_file returned {result}")
        rx.stop()
        return 1

    # wait for receiver to finish writing
    deadline = time.time() + 10
    while time.time() < deadline:
        history = rx.history()
        if history and history[-1].status == STATUS_DONE:
            break
        time.sleep(0.05)
    rx.stop()

    history = rx.history()
    if not history or history[-1].status != STATUS_DONE:
        print(f"FAIL: receiver status = {history[-1].status if history else 'none'} "
              f"err={history[-1].error if history else None}")
        return 1

    rec = history[-1]
    saved = rec.save_path
    if not saved or not saved.is_file():
        print(f"FAIL: saved path missing: {saved}")
        return 1
    dst_hash = _sha256(saved)
    if dst_hash != src_hash:
        print(f"FAIL: hash mismatch  src={src_hash} dst={dst_hash}")
        return 1
    if not rec.verified:
        print("FAIL: receiver did not record sha256 verification")
        return 1
    sent_sha = result.get("sha256")
    if sent_sha != src_hash:
        print(f"FAIL: sender sha256 mismatch  sent={sent_sha} src={src_hash}")
        return 1

    print(
        f"OK  {result['bytes_sent']} bytes  "
        f"sha256={src_hash[:16]}…  verified={rec.verified}  "
        f"speed={rec.speed_bps/1e6:.1f} MB/s  saved={saved}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
