"""Persistent state (tunnels, favourites order, settings) in state.json."""

from __future__ import annotations

import fcntl
import json
import logging
import os
import tempfile
import threading
import time
from pathlib import Path
from typing import IO, Callable, Iterable, List, Optional, Tuple

from pydantic import ValidationError

from . import paths
from .models import Settings, State, Tunnel

log = logging.getLogger(__name__)


def atomic_write(path: Path, data: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


class Store:
    def __init__(self, path: Optional[Path] = None):
        self.path = path or paths.state_file()
        self._lock = threading.RLock()
        self._listeners: List[Callable[[], None]] = []
        self.state = self._load()

    # -- persistence --

    def _load(self) -> State:
        if not self.path.exists():
            return State()
        try:
            return State.model_validate_json(self.path.read_text())
        except (ValidationError, ValueError, OSError) as e:
            backup = self.path.with_name(f"state.corrupt-{int(time.time())}.json")
            log.error("state.json unreadable (%s); moved to %s and starting fresh", e, backup)
            try:
                self.path.rename(backup)
            except OSError:
                pass
            return State()

    def save(self) -> None:
        with self._lock:
            atomic_write(self.path, self.state.model_dump_json(indent=2))
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover - listener bugs must not break saves
                log.exception("state listener failed")

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    # -- queries --

    @property
    def settings(self) -> Settings:
        return self.state.settings

    def tunnels(self) -> List[Tunnel]:
        with self._lock:
            return list(self.state.tunnels)

    def get(self, tunnel_id: str) -> Optional[Tunnel]:
        with self._lock:
            return next((t for t in self.state.tunnels if t.id == tunnel_id), None)

    def find(self, ref: str) -> Optional[Tunnel]:
        """Look a tunnel up by id, exact name, or unique name prefix (for the CLI)."""
        with self._lock:
            t = self.get(ref)
            if t:
                return t
            exact = [t for t in self.state.tunnels if t.name == ref]
            if len(exact) == 1:
                return exact[0]
            pref = [t for t in self.state.tunnels if t.name.lower().startswith(ref.lower())]
            return pref[0] if len(pref) == 1 else None

    def find_duplicate(self, tunnel: Tunnel, exclude_id: Optional[str] = None) -> Optional[Tunnel]:
        key = tunnel.dedupe_key()
        with self._lock:
            return next(
                (t for t in self.state.tunnels if t.id != exclude_id and t.dedupe_key() == key), None
            )

    # -- mutations --

    def _next_fav_order(self) -> int:
        return max((t.order for t in self.state.tunnels if t.favorite), default=-1) + 1

    def add(self, tunnel: Tunnel) -> Tuple[Tunnel, bool]:
        """Add a tunnel; if an identical one exists, bump and return it instead."""
        with self._lock:
            dup = self.find_duplicate(tunnel)
            if dup:
                dup.last_used_at = time.time()
                dup.use_count += 1
                if tunnel.favorite and not dup.favorite:
                    dup.favorite, dup.order = True, self._next_fav_order()
                self.save()
                return dup, False
            if tunnel.favorite:
                tunnel.order = self._next_fav_order()
            h = tunnel.key_hash()
            if h in self.state.ignored_keys:
                self.state.ignored_keys.remove(h)
            self.state.tunnels.append(tunnel)
            self.save()
            return tunnel, True

    def update(self, tunnel_id: str, changes: dict) -> Tunnel:
        with self._lock:
            current = self.get(tunnel_id)
            if current is None:
                raise KeyError(tunnel_id)
            data = current.model_dump()
            data.update({k: v for k, v in changes.items() if k not in ("id", "created_at")})
            if changes.get("name") == "":
                data["name"] = ""  # re-derive the automatic name
            updated = Tunnel.model_validate(data)
            dup = self.find_duplicate(updated, exclude_id=tunnel_id)
            if dup:
                raise ValueError(f"an identical tunnel already exists: {dup.name}")
            if updated.favorite and not current.favorite:
                updated.order = self._next_fav_order()
            idx = self.state.tunnels.index(current)
            self.state.tunnels[idx] = updated
            self.save()
            return updated

    def touch(self, tunnel_id: str, **fields) -> None:
        """Update bookkeeping fields without re-validating the whole tunnel."""
        with self._lock:
            t = self.get(tunnel_id)
            if t is None:
                return
            for k, v in fields.items():
                setattr(t, k, v)
            self.save()

    def delete(self, tunnel_id: str) -> Optional[Tunnel]:
        with self._lock:
            t = self.get(tunnel_id)
            if t is None:
                return None
            self.state.tunnels.remove(t)
            h = t.key_hash()
            if h not in self.state.ignored_keys:
                self.state.ignored_keys.append(h)
            self.save()
            return t

    def reorder_favorites(self, ids: List[str]) -> None:
        with self._lock:
            pos = {tid: i for i, tid in enumerate(ids)}
            for t in self.state.tunnels:
                if t.favorite and t.id in pos:
                    t.order = pos[t.id]
            self.save()

    def update_settings(self, changes: dict) -> Settings:
        with self._lock:
            data = self.state.settings.model_dump()
            data.update(changes)
            self.state.settings = Settings.model_validate(data)
            self.save()
            return self.state.settings

    def merge_imported(self, candidates: Iterable[Tunnel]) -> dict:
        """Merge history candidates: new ones are added, known ones get their
        usage stats refreshed, deleted ones stay deleted."""
        added = updated = ignored = 0
        with self._lock:
            by_key = {t.dedupe_key(): t for t in self.state.tunnels}
            for c in candidates:
                existing = by_key.get(c.dedupe_key())
                if existing:
                    if c.last_used_at > existing.last_used_at or c.use_count > existing.use_count:
                        existing.last_used_at = max(existing.last_used_at, c.last_used_at)
                        existing.use_count = max(existing.use_count, c.use_count)
                        updated += 1
                    continue
                if c.key_hash() in self.state.ignored_keys:
                    ignored += 1
                    continue
                self.state.tunnels.append(c)
                by_key[c.dedupe_key()] = c
                added += 1
            self.state.history_imported_at = time.time()
            self.save()
        return {"added": added, "updated": updated, "ignored": ignored}


# --- single instance --------------------------------------------------------------


def acquire_instance_lock(port: int) -> Optional[IO[str]]:
    """Take an exclusive lock so only one manager owns the tunnels.
    Returns the open lock file (keep it referenced) or None if already locked."""
    f = open(paths.lock_file(), "a+")
    try:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        f.close()
        return None
    f.seek(0)
    f.truncate()
    f.write(json.dumps({"pid": os.getpid(), "port": port, "started": time.time()}))
    f.flush()
    return f


def running_instance() -> Optional[dict]:
    """{"pid", "port"} of the manager holding the lock, or None."""
    p = paths.lock_file()
    if not p.exists():
        return None
    with open(p, "a+") as f:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            f.seek(0)
            try:
                return json.loads(f.read() or "{}") or {"port": None}
            except ValueError:
                return {"port": None}
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        return None
