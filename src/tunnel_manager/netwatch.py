"""Detect network changes and wake-from-sleep so tunnels can reconnect at once
instead of waiting for ssh's ServerAlive timeout."""

from __future__ import annotations

import ipaddress
import logging
import re
import subprocess
import sys
import threading
import time
from typing import Callable, FrozenSet, Optional, Tuple

import psutil

log = logging.getLogger(__name__)

Fingerprint = Tuple[Optional[str], FrozenSet[str]]


def local_addresses() -> FrozenSet[str]:
    """Non-loopback, non-link-local IPs of interfaces that are up."""
    out = set()
    try:
        stats = psutil.net_if_stats()
        for name, addrs in psutil.net_if_addrs().items():
            st = stats.get(name)
            if st is not None and not st.isup:
                continue
            for a in addrs:
                ip = a.address.split("%", 1)[0]
                try:
                    obj = ipaddress.ip_address(ip)
                except ValueError:
                    continue  # MAC addresses etc.
                if obj.is_loopback or obj.is_link_local:
                    continue
                out.add(ip)
    except Exception:  # pragma: no cover - psutil quirks on exotic systems
        log.debug("could not list interfaces", exc_info=True)
    return frozenset(out)


def default_route() -> Optional[str]:
    """'<interface> via <gateway>' for the default route, or None if offline."""
    try:
        if sys.platform == "darwin":
            out = subprocess.run(["route", "-n", "get", "default"], capture_output=True, text=True, timeout=3).stdout
            iface = re.search(r"interface:\s*(\S+)", out)
            gw = re.search(r"gateway:\s*(\S+)", out)
            if not iface:
                return None
            return f"{iface.group(1)} via {gw.group(1) if gw else '?'}"
        with open("/proc/net/route") as f:
            for line in f.readlines()[1:]:
                parts = line.split()
                if len(parts) > 2 and parts[1] == "00000000":
                    return f"{parts[0]} via {parts[2]}"
    except Exception:
        log.debug("could not read default route", exc_info=True)
        return "unknown"  # don't claim we're offline if we simply can't tell
    return None


def fingerprint() -> Fingerprint:
    return default_route(), local_addresses()


class NetWatch:
    """Polls the network state and reports ``(event, online)`` where event is
    'change' (addresses / default route changed) or 'wake' (system slept)."""

    def __init__(
        self,
        callback: Callable[[str, bool], None],
        interval: float = 2.0,
        fingerprint_fn: Callable[[], Fingerprint] = fingerprint,
        wall: Callable[[], float] = time.time,
        mono: Callable[[], float] = time.monotonic,
        sleep_threshold: float = 8.0,
    ):
        self.callback = callback
        self.interval = interval
        self.fingerprint_fn = fingerprint_fn
        self.wall, self.mono = wall, mono
        self.sleep_threshold = sleep_threshold
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self.last: Fingerprint = fingerprint_fn()
        self.addresses: FrozenSet[str] = self.last[1]
        self._last_wall, self._last_mono = wall(), mono()

    @property
    def online(self) -> bool:
        return self.last[0] is not None

    def check(self) -> None:
        """One polling step (public so tests can drive it)."""
        wall, mono = self.wall(), self.mono()
        slept = (wall - self._last_wall) - (mono - self._last_mono)
        self._last_wall, self._last_mono = wall, mono
        fp = self.fingerprint_fn()
        changed = fp != self.last
        self.last, self.addresses = fp, fp[1]
        if slept > self.sleep_threshold:
            log.info("wake from sleep detected (%.0fs)", slept)
            self.callback("wake", self.online)
        elif changed:
            log.info("network changed: route=%s addrs=%s", fp[0], sorted(fp[1]))
            self.callback("change", self.online)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="netwatch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.check()
            except Exception:  # pragma: no cover
                log.exception("netwatch check failed")
