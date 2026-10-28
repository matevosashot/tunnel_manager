"""Locations of the cache directory and the files inside it."""

from __future__ import annotations

import os
from pathlib import Path

APP_NAME = "tunnel-manager"


def home() -> Path:
    """Root of all persisted data.

    $TUNNEL_MANAGER_HOME wins (used by tests and demo mode), then
    $XDG_CACHE_HOME/tunnel-manager, then ~/.cache/tunnel-manager.
    """
    override = os.environ.get("TUNNEL_MANAGER_HOME")
    if override:
        root = Path(override).expanduser()
    else:
        xdg = os.environ.get("XDG_CACHE_HOME")
        base = Path(xdg).expanduser() if xdg else Path.home() / ".cache"
        root = base / APP_NAME
    root.mkdir(parents=True, exist_ok=True)
    return root


def state_file() -> Path:
    return home() / "state.json"


def runtime_file() -> Path:
    return home() / "runtime.json"


def lock_file() -> Path:
    return home() / "manager.lock"


def manager_log() -> Path:
    return home() / "manager.log"


def service_log() -> Path:
    return home() / "service.log"


def logs_dir() -> Path:
    d = home() / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def tunnel_log(tunnel_id: str) -> Path:
    return logs_dir() / f"{tunnel_id}.log"


def ssh_dir() -> Path:
    return Path(os.environ.get("TUNNEL_MANAGER_SSH_DIR", str(Path.home() / ".ssh"))).expanduser()


def ssh_binary() -> str:
    """The ssh executable. Overridable so tests can substitute a fake ssh."""
    return os.environ.get("TUNNEL_MANAGER_SSH", "ssh")
