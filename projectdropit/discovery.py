"""LAN discovery via zeroconf (mDNS).

Each device advertises a service of type ``_projectdropit._tcp.local.`` with
the listening TCP port and a ``name`` TXT record. A background listener keeps
a live dict of currently-online peers.
"""
from __future__ import annotations

import socket
import threading
import uuid
from dataclasses import dataclass
from typing import Dict, List, Optional

from zeroconf import IPVersion, ServiceBrowser, ServiceInfo, ServiceListener, Zeroconf

SERVICE_TYPE = "_projectdropit._tcp.local."


@dataclass
class Peer:
    name: str           # human-readable device name (from TXT)
    service: str        # full zeroconf service name (unique key)
    address: str        # IPv4 string
    port: int

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"


def _primary_ipv4() -> Optional[str]:
    """Best-effort guess of the LAN IPv4 address (no packets actually sent)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        try:
            return socket.gethostbyname(socket.gethostname())
        except Exception:
            return None
    finally:
        s.close()


class _Listener(ServiceListener):
    def __init__(self, peers: Dict[str, Peer], get_self_service, lock: threading.Lock):
        self._peers = peers
        self._get_self = get_self_service
        self._lock = lock

    def _refresh(self, zc: Zeroconf, type_: str, name: str) -> None:
        info = zc.get_service_info(type_, name, timeout=1500)
        if not info:
            return
        if name == self._get_self():
            return  # ignore self (re-evaluated every callback so renames work)
        addrs = info.parsed_addresses(IPVersion.V4Only) or []
        if not addrs:
            return
        # prefer TXT 'name', fallback to service label
        txt_name = None
        try:
            props = info.properties or {}
            raw = props.get(b"name")
            if raw:
                txt_name = raw.decode("utf-8", errors="replace")
        except Exception:
            txt_name = None
        display = txt_name or name.split("." + SERVICE_TYPE[:-1].lstrip("_"))[0].rstrip(".")
        peer = Peer(name=display, service=name, address=addrs[0], port=info.port or 0)
        with self._lock:
            self._peers[name] = peer

    def add_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._refresh(zc, type_, name)

    def update_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        self._refresh(zc, type_, name)

    def remove_service(self, zc: Zeroconf, type_: str, name: str) -> None:
        with self._lock:
            self._peers.pop(name, None)


class DiscoveryService:
    """Owns the Zeroconf instance, advertises self, and tracks peers."""

    def __init__(self, device_name: str, port: int):
        self.device_name = device_name
        self.port = port
        self._zc: Optional[Zeroconf] = None
        self._info: Optional[ServiceInfo] = None
        self._browser: Optional[ServiceBrowser] = None
        self._peers: Dict[str, Peer] = {}
        self._lock = threading.Lock()
        self._service_name = self._make_service_name(device_name)
        # Diagnostics surfaced to the CLI for friendly warnings.
        self.local_ip: Optional[str] = None
        self.registered: bool = False
        self.last_error: Optional[str] = None

    @staticmethod
    def _make_service_name(device_name: str) -> str:
        # zeroconf requires a unique instance name; collisions get suffixed.
        safe = "".join(c if (c.isalnum() or c in "-_ ") else "_" for c in device_name)[:48]
        suffix = uuid.uuid4().hex[:6]
        return f"{safe or 'device'} [{suffix}].{SERVICE_TYPE}"

    def start(self) -> None:
        ip = _primary_ipv4()
        self.local_ip = ip
        addresses = []
        if ip:
            try:
                addresses = [socket.inet_aton(ip)]
            except Exception:
                addresses = []
        try:
            self._zc = Zeroconf(ip_version=IPVersion.V4Only)
        except Exception as e:
            self.last_error = f"zeroconf init failed: {e}"
            return
        self._info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=self._service_name,
            addresses=addresses,
            port=self.port,
            properties={b"name": self.device_name.encode("utf-8")},
            server=None,
        )
        try:
            self._zc.register_service(self._info)
            self.registered = True
        except Exception as e:
            # not fatal — discovery may still receive
            self.last_error = f"service register failed: {e}"
        listener = _Listener(self._peers, lambda: self._service_name, self._lock)
        try:
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, listener)
        except Exception as e:
            self.last_error = f"service browse failed: {e}"

    def update_device_name(self, new_name: str) -> None:
        """Re-register the service under a new human-readable name."""
        if not self._zc:
            self.device_name = new_name
            self._service_name = self._make_service_name(new_name)
            return
        try:
            if self._info:
                self._zc.unregister_service(self._info)
        except Exception:
            pass
        self.device_name = new_name
        # Drop the stale self-entry from the peer table if zeroconf still has it.
        with self._lock:
            self._peers.pop(self._service_name, None)
        self._service_name = self._make_service_name(new_name)
        ip = _primary_ipv4()
        addresses = [socket.inet_aton(ip)] if ip else []
        self._info = ServiceInfo(
            type_=SERVICE_TYPE,
            name=self._service_name,
            addresses=addresses,
            port=self.port,
            properties={b"name": new_name.encode("utf-8")},
            server=None,
        )
        try:
            self._zc.register_service(self._info)
        except Exception:
            pass
        # Restart the browser so the self-filter uses the new service name.
        try:
            if self._browser:
                self._browser.cancel()
        except Exception:
            pass
        listener = _Listener(self._peers, lambda: self._service_name, self._lock)
        try:
            self._browser = ServiceBrowser(self._zc, SERVICE_TYPE, listener)
        except Exception:
            pass

    def peers(self) -> List[Peer]:
        with self._lock:
            return sorted(self._peers.values(), key=lambda p: p.name.lower())

    def stop(self) -> None:
        try:
            if self._zc and self._info:
                self._zc.unregister_service(self._info)
        except Exception:
            pass
        try:
            if self._zc:
                self._zc.close()
        except Exception:
            pass
        self._zc = None
