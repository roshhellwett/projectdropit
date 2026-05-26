"""Persistent user settings stored at ~/.projectdropit/config.json."""
from __future__ import annotations

import json
import os
import socket
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

CONFIG_DIR = Path.home() / ".projectdropit"
CONFIG_PATH = CONFIG_DIR / "config.json"
# IMPORTANT: must NOT be ~/projectdropit — that would shadow the package name
# when Python is run from the home directory ('' in sys.path resolves to ~/).
DEFAULT_DOWNLOAD_DIR = Path.home() / "projectdropit_files"


def _default_device_name() -> str:
    try:
        host = socket.gethostname() or "device"
    except Exception:
        host = "device"
    # keep it short and clean
    host = host.split(".")[0]
    return host[:32] or "device"


class Config:
    def __init__(self, data: Dict[str, Any]):
        self._data = data
        # Set when load() detects a corrupt config file so the CLI can warn.
        self.load_warning: Optional[str] = None

    @classmethod
    def load(cls) -> "Config":
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        if CONFIG_PATH.exists():
            try:
                with CONFIG_PATH.open("r", encoding="utf-8") as f:
                    raw = f.read()
                data = json.loads(raw)
                if not isinstance(data, dict):
                    raise ValueError("config root must be a JSON object")
                return cls(data)
            except Exception as e:
                # Config is corrupt — start fresh but surface a warning.
                cfg = cls({})
                cfg.load_warning = (
                    f"Config file is corrupt and was reset ({e}). "
                    f"Your previous settings have been lost."
                )
                return cfg
        return cls({})

    def save(self) -> bool:
        """Persist settings to disk. Returns True on success, False on failure."""
        try:
            CONFIG_DIR.mkdir(parents=True, exist_ok=True)
            tmp = CONFIG_PATH.with_suffix(".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(self._data, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
            return True
        except Exception:
            return False

    # device name
    @property
    def device_name(self) -> str:
        return self._data.get("device_name") or _default_device_name()

    @device_name.setter
    def device_name(self, value: str) -> None:
        v = (value or "").strip()
        if v:
            self._data["device_name"] = v[:48]

    @property
    def has_device_name(self) -> bool:
        return bool(self._data.get("device_name"))

    # download dir
    @property
    def download_dir(self) -> Path:
        raw = self._data.get("download_dir")
        return Path(raw).expanduser() if raw else DEFAULT_DOWNLOAD_DIR

    @download_dir.setter
    def download_dir(self, value: str | Path) -> None:
        # Always store an absolute path so future launches resolve identically
        # regardless of the current working directory.
        p = Path(value).expanduser()
        try:
            p = p.resolve(strict=False)
        except Exception:
            p = Path(os.path.abspath(str(p)))
        self._data["download_dir"] = str(p)

    def ensure_download_dir(self) -> Path:
        d = self.download_dir
        d.mkdir(parents=True, exist_ok=True)
        return d

    @staticmethod
    def migrate_legacy_download_dir() -> Optional[str]:
        """Rename ~/projectdropit → ~/projectdropit_files if it exists and is not
        already the new default. Returns a message if migration happened, else None.

        This is a one-time migration for users who had the old default.
        The old directory name shadowed the package when Python was run from ~.
        """
        old = Path.home() / "projectdropit"
        new = Path.home() / "projectdropit_files"
        if old.exists() and old.is_dir() and not new.exists():
            try:
                old.rename(new)
                return (
                    f"Moved download folder: {old} → {new}\n"
                    f"(The old name conflicted with the package when running from your home directory.)"
                )
            except Exception:
                pass
        return None
