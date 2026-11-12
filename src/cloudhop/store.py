"""Where cloudhop keeps its files, and small helpers for reading/writing them safely."""

from __future__ import annotations

import json
import os
import secrets
import sys
import threading
from pathlib import Path
from typing import Any

_write_lock = threading.Lock()


def default_data_dir() -> Path:
    if sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local" / "share")
    return base / "cloudhop"


def read_json(path: Path, default: Any) -> Any:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except ValueError:
        # Keep the broken file for inspection instead of silently losing it
        path.replace(path.with_suffix(path.suffix + ".corrupt"))
        return default


def write_json(path: Path, data: Any) -> None:
    """Atomic write: never leaves a half-written file behind."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{os.getpid()}.tmp")
    with _write_lock:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, path)


class DataDir:
    """Layout of the data directory."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self.bin = root / "bin"
        self.runs = root / "runs"
        self.runs.mkdir(exist_ok=True)
        self.settings_file = root / "settings.json"
        self.jobs_file = root / "jobs.json"
        self.default_rclone_config = root / "rclone.conf"

    def access_token(self) -> str:
        """Persistent token so a bookmarked address keeps working across restarts."""
        path = self.root / "access-token"
        try:
            token = path.read_text().strip()
            if token:
                return token
        except FileNotFoundError:
            pass
        token = secrets.token_urlsafe(18)
        path.write_text(token)
        try:
            path.chmod(0o600)
        except OSError:
            pass
        return token


DEFAULT_SETTINGS = {
    "rclone_path": "",        # empty: managed download or rclone on PATH
    "rclone_config": "",      # empty: <data dir>/rclone.conf
    "max_concurrent_runs": 1,
    "keep_awake": True,
}


class Settings:
    def __init__(self, data: DataDir):
        self.data = data
        self._values = {**DEFAULT_SETTINGS, **read_json(data.settings_file, {})}

    def get(self, key: str) -> Any:
        return self._values.get(key, DEFAULT_SETTINGS.get(key))

    def all(self) -> dict:
        return dict(self._values)

    def update(self, changes: dict) -> dict:
        for key, value in changes.items():
            if key not in DEFAULT_SETTINGS:
                raise ValueError(f"Unknown setting: {key}")
            if key == "max_concurrent_runs":
                value = max(1, min(8, int(value)))
            elif key == "keep_awake":
                value = bool(value)
            else:
                value = str(value).strip()
            self._values[key] = value
        write_json(self.data.settings_file, self._values)
        return self.all()

    @property
    def rclone_config(self) -> Path:
        configured = self.get("rclone_config")
        return Path(configured).expanduser() if configured else self.data.default_rclone_config
