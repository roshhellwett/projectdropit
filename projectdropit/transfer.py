"""Transfer protocol on top of the encrypted channel.

Session lifecycle (after `crypto.handshake` completes):

  sender → receiver : JSON {"type":"offer","sender":"<name>","filename":"...","size":N}
  receiver → sender : JSON {"type":"response","accept":bool,"reason":"..."}
  if accepted:
      sender → receiver : N bytes of file content (split into many encrypted frames)
      sender → receiver : JSON {"type":"done","sha256":"<hex>"}
      receiver → sender : JSON {"type":"ack","ok":bool,"reason":"..."}

All control messages are UTF-8 JSON inside a single encrypted frame. File
content travels as raw bytes inside encrypted frames; the receiver knows the
total size from the offer, so it stops reading once it has reassembled `size`
bytes.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import socket
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

from .crypto import SecureChannel, handshake

CHUNK_SIZE = 64 * 1024
OFFER_TIMEOUT = 120.0       # seconds the sender waits for accept/reject
DATA_TIMEOUT = 300.0        # seconds allowed for the full data transfer (5 min)
SENDER_NAME_MAX = 128       # cap peer-supplied sender name to prevent DoS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _send_json(ch: SecureChannel, obj: dict) -> None:
    ch.send(json.dumps(obj, separators=(",", ":")).encode("utf-8"))


def _recv_json(ch: SecureChannel) -> dict:
    """Receive one encrypted frame and decode it as a JSON object (dict)."""
    raw = ch.recv()
    parsed = json.loads(raw.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object, got {type(parsed).__name__}")
    return parsed


_WIN_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def sanitize_filename(name: str) -> str:
    """Make a peer-supplied filename safe to write to disk on any OS.

    - Strips path separators from both Windows and POSIX so a Windows sender
      cannot escape the download dir on a Linux receiver (or vice versa).
    - Removes leading dots ('.', '..', and 'dotfiles' that could overwrite
      configuration), null bytes, and ASCII control chars.
    - Replaces characters Windows forbids in filenames with '_'.
    - Falls back to 'file.bin' if nothing usable remains.
    - Caps the length to 200 characters (filesystems vary; 255 is typical).
    """
    if not isinstance(name, str):
        name = str(name or "")
    # Reduce any path-like input to its tail component on either OS.
    name = name.replace("\\", "/").split("/")[-1]
    # Strip ASCII control chars (incl. tabs/newlines) and null bytes.
    name = "".join(c for c in name if ord(c) >= 32)
    name = name.strip().strip(".").strip()
    # Replace characters Windows forbids: <>:"/\|?*
    name = re.sub(r'[<>:"/\\|?*]', "_", name)
    if not name or name in (".", ".."):
        name = "file.bin"
    # Reserved Windows device names (case-insensitive, with or without ext).
    stem = name.split(".", 1)[0].upper()
    if stem in _WIN_RESERVED:
        name = "_" + name
    return name[:200]


def human_size(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024.0 or unit == "TB":
            return f"{f:.1f} {unit}" if unit != "B" else f"{int(f)} {unit}"
        f /= 1024.0
    return f"{n} B"  # unreachable, satisfies type checkers


# ---------------------------------------------------------------------------
# Records
# ---------------------------------------------------------------------------

STATUS_PENDING = "pending"
STATUS_ACTIVE = "active"
STATUS_DONE = "done"
STATUS_REJECTED = "rejected"
STATUS_FAILED = "failed"


@dataclass
class IncomingTransfer:
    id: str
    sender_name: str
    sender_addr: str
    filename: str
    size: int
    started_at: float
    status: str = STATUS_PENDING
    received: int = 0
    save_path: Optional[Path] = None
    error: Optional[str] = None
    verified: bool = False
    finished_at: Optional[float] = None
    # internal:
    _channel: Optional[SecureChannel] = field(default=None, repr=False)
    _decision: threading.Event = field(default_factory=threading.Event, repr=False)
    _accepted: bool = field(default=False, repr=False)

    @property
    def speed_bps(self) -> float:
        if not self.received or not self.started_at:
            return 0.0
        end = self.finished_at or time.time()
        elapsed = max(end - self.started_at, 1e-6)
        return self.received / elapsed


# ---------------------------------------------------------------------------
# Sender
# ---------------------------------------------------------------------------

def send_file(
    peer_addr: str,
    peer_port: int,
    filepath: Path,
    sender_name: str,
    progress: Optional[Callable[[int, int], None]] = None,
) -> dict:
    """Send `filepath` to peer. Blocking. Returns a result dict.

    Result keys: ok (bool), reason (str), bytes_sent (int), sha256 (str, on success).
    """
    filepath = Path(filepath)
    if not filepath.is_file():
        return {"ok": False, "reason": f"not a file: {filepath}", "bytes_sent": 0}
    size = filepath.stat().st_size

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(10.0)
    try:
        sock.connect((peer_addr, peer_port))
    except Exception as e:
        sock.close()
        return {"ok": False, "reason": f"connect failed: {e}", "bytes_sent": 0}

    try:
        sock.settimeout(30.0)
        ch = handshake(sock)
        _send_json(ch, {
            "type": "offer",
            "sender": sender_name[:SENDER_NAME_MAX],
            "filename": filepath.name,
            "size": size,
        })
        sock.settimeout(OFFER_TIMEOUT)
        resp = _recv_json(ch)
        if resp.get("type") != "response" or not resp.get("accept"):
            return {
                "ok": False,
                "reason": resp.get("reason") or "rejected by peer",
                "bytes_sent": 0,
            }

        # Use a generous but finite timeout for the data transfer phase.
        sock.settimeout(DATA_TIMEOUT)
        sent = 0
        hasher = hashlib.sha256()
        with filepath.open("rb") as f:
            while sent < size:
                chunk = f.read(CHUNK_SIZE)
                if not chunk:
                    break
                ch.send(chunk)
                hasher.update(chunk)
                sent += len(chunk)
                if progress:
                    try:
                        progress(sent, size)
                    except Exception:
                        pass
        _send_json(ch, {"type": "done", "sha256": hasher.hexdigest()})
        # best-effort wait for receiver's ack so we know if verification failed
        try:
            sock.settimeout(15.0)
            ack = _recv_json(ch)
            if ack.get("type") == "ack" and ack.get("ok") is False:
                return {
                    "ok": False,
                    "reason": ack.get("reason") or "receiver verification failed",
                    "bytes_sent": sent,
                }
        except Exception:
            pass
        return {"ok": True, "reason": "", "bytes_sent": sent, "sha256": hasher.hexdigest()}
    except Exception as e:
        return {"ok": False, "reason": f"transfer error: {e}", "bytes_sent": 0}
    finally:
        try:
            sock.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Receiver
# ---------------------------------------------------------------------------

class ReceiverServer:
    """TCP listener. Each accepted socket runs a session in its own thread.

    Pending offers wait for a user decision via ``decide(transfer_id, accept)``.
    The CLI consumes ``transfers`` for both pending and historical entries.
    """

    def __init__(self, get_download_dir: Callable[[], Path], get_device_name: Callable[[], str]):
        self._get_dir = get_download_dir
        self._get_name = get_device_name
        self._sock: Optional[socket.socket] = None
        self.port: int = 0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()
        self.transfers: List[IncomingTransfer] = []
        self.on_offer: Optional[Callable[[IncomingTransfer], None]] = None
        self.on_update: Optional[Callable[[IncomingTransfer], None]] = None

    # ------------------------------------------------------------------
    def start(self) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("0.0.0.0", 0))
        s.listen(8)
        self.port = s.getsockname()[1]
        self._sock = s
        self._thread = threading.Thread(target=self._accept_loop, name="pdit-accept", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        try:
            if self._sock:
                self._sock.close()
        except Exception:
            pass
        # Wait briefly for the accept thread to exit cleanly.
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    def pending(self) -> List[IncomingTransfer]:
        with self._lock:
            return [t for t in self.transfers if t.status == STATUS_PENDING]

    def history(self) -> List[IncomingTransfer]:
        with self._lock:
            return list(self.transfers)

    def download_dir(self) -> Path:
        """Public accessor for the configured download directory."""
        return self._get_dir()

    def decide(self, transfer_id: str, accept: bool) -> bool:
        with self._lock:
            t = next((x for x in self.transfers if x.id == transfer_id), None)
        if not t or t.status != STATUS_PENDING:
            return False
        t._accepted = accept
        t._decision.set()
        return True

    # ------------------------------------------------------------------
    def _accept_loop(self) -> None:
        assert self._sock is not None
        while not self._stop.is_set():
            try:
                conn, addr = self._sock.accept()
            except OSError:
                break
            except Exception:
                continue
            threading.Thread(
                target=self._handle, args=(conn, addr), name="pdit-session", daemon=True
            ).start()

    def _handle(self, conn: socket.socket, addr) -> None:
        sender_addr = f"{addr[0]}:{addr[1]}"
        t: Optional[IncomingTransfer] = None
        try:
            conn.settimeout(30.0)
            ch = handshake(conn)
            offer = _recv_json(ch)
            if offer.get("type") != "offer":
                return

            filename = sanitize_filename(str(offer.get("filename") or "file.bin"))

            # Validate size: must be a non-negative integer within a sane range.
            raw_size = offer.get("size")
            try:
                size = int(raw_size)
            except (TypeError, ValueError):
                return
            if size < 0 or size > 1024 ** 4:  # 1 TB sanity cap
                return

            # Cap sender name to prevent memory abuse.
            sender_name = str(offer.get("sender") or "unknown")[:SENDER_NAME_MAX]

            t = IncomingTransfer(
                id=uuid.uuid4().hex[:8],
                sender_name=sender_name,
                sender_addr=sender_addr,
                filename=filename,
                size=size,
                started_at=time.time(),
                _channel=ch,
            )
            with self._lock:
                self.transfers.append(t)
            if self.on_offer:
                try:
                    self.on_offer(t)
                except Exception:
                    pass

            # wait for decision
            conn.settimeout(OFFER_TIMEOUT + 5)
            decided = t._decision.wait(timeout=OFFER_TIMEOUT)
            if not decided:
                t.status = STATUS_REJECTED
                t.error = "timed out waiting for user decision"
                try:
                    _send_json(ch, {"type": "response", "accept": False, "reason": "timeout"})
                except Exception:
                    pass
                if self.on_update:
                    self.on_update(t)
                return

            if not t._accepted:
                t.status = STATUS_REJECTED
                try:
                    _send_json(ch, {"type": "response", "accept": False, "reason": "rejected"})
                except Exception:
                    pass
                if self.on_update:
                    self.on_update(t)
                return

            # accepted — prepare destination
            dest_dir = self._get_dir()
            dest_dir.mkdir(parents=True, exist_ok=True)
            save_path = _unique_path(dest_dir / filename)
            t.save_path = save_path
            t.status = STATUS_ACTIVE
            if self.on_update:
                self.on_update(t)
            _send_json(ch, {"type": "response", "accept": True})

            # Use a generous but finite timeout for the data transfer phase.
            conn.settimeout(DATA_TIMEOUT)
            received = 0
            hasher = hashlib.sha256()
            with save_path.open("wb") as out:
                while received < size:
                    frame = ch.recv()
                    if not frame:
                        continue
                    remaining = size - received
                    if len(frame) > remaining:
                        # Defensive: protocol shouldn't allow this; trim & stop.
                        frame = frame[:remaining]
                    out.write(frame)
                    hasher.update(frame)
                    received += len(frame)
                    t.received = received
                    if self.on_update:
                        self.on_update(t)

            # tail "done" with sha256
            expected_sha: Optional[str] = None
            try:
                conn.settimeout(15.0)
                tail = _recv_json(ch)
                if tail.get("type") == "done":
                    expected_sha = tail.get("sha256")
            except Exception:
                pass

            if received < size:
                t.status = STATUS_FAILED
                t.error = f"truncated: got {received}/{size} bytes"
                # Clean up the incomplete file.
                try:
                    save_path.unlink()
                except Exception:
                    pass
            elif expected_sha and hasher.hexdigest() != expected_sha:
                # Delete corrupted file so user is not misled.
                try:
                    save_path.unlink()
                except Exception:
                    pass
                t.status = STATUS_FAILED
                t.error = "integrity check failed (sha256 mismatch)"
                try:
                    _send_json(ch, {"type": "ack", "ok": False, "reason": t.error})
                except Exception:
                    pass
            else:
                t.status = STATUS_DONE
                t.verified = bool(expected_sha)
                try:
                    _send_json(ch, {"type": "ack", "ok": True})
                except Exception:
                    pass
            t.finished_at = time.time()
            if self.on_update:
                self.on_update(t)
        except Exception as e:
            if t is not None:
                t.status = STATUS_FAILED
                t.error = str(e)
                t.finished_at = time.time()
                if self.on_update:
                    self.on_update(t)
        finally:
            try:
                conn.close()
            except Exception:
                pass


def _unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    stem, suffix = p.stem, p.suffix
    i = 1
    while True:
        cand = p.with_name(f"{stem} ({i}){suffix}")
        if not cand.exists():
            return cand
        i += 1
