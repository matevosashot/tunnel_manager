"""Runs and supervises one ssh process per tunnel.

* ssh is started in its own session with output going to logs/<id>.log, so a
  tunnel keeps running if the manager restarts; on startup the manager
  re-adopts it (pid + create time + exact argv must match runtime.json).
* A polling loop moves each tunnel through
  stopped -> starting -> up -> (retrying | offline | failed).
* With keep-alive on, dropped tunnels are restarted with exponential backoff;
  auth / host-key / config errors give up after a few attempts.
* Network changes and wake-from-sleep trigger immediate retries, and up
  tunnels whose ssh socket is bound to an address that no longer exists are
  restarted right away instead of waiting for ServerAlive to time out.
"""

from __future__ import annotations

import collections
import errno
import ipaddress
import json
import logging
import os
import re
import signal
import socket
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Deque, Dict, List, Optional

import psutil

from . import paths
from .models import Tunnel
from .netwatch import NetWatch
from .sshcmd import SshError, build_argv, classify_error, display_command
from .store import Store, atomic_write

log = logging.getLogger(__name__)

TICK = 0.5
MAX_BACKOFF = 30.0
STABLE_AFTER = 60.0        # seconds up before the backoff resets
MAX_PERMANENT_FAILURES = 3
KILL_GRACE = 2.0
LOG_ROTATE_BYTES = 1_000_000
R_ONLY_GRACE = 1.5         # -R only: seconds after auth without exit => up

_AUTH_RE = re.compile(r"Authenticated to (\S+)")
_TRANSFER_RE = re.compile(r"Transferred: sent (\d+), received (\d+) bytes")


@dataclass
class Runtime:
    status: str = "stopped"  # stopped | starting | up | retrying | offline | failed
    proc: Optional[subprocess.Popen] = None
    pid: Optional[int] = None
    argv: List[str] = field(default_factory=list)
    adopted: bool = False
    spawned_at: Optional[float] = None
    authenticated_at: Optional[float] = None
    connected_at: Optional[float] = None
    connect_ms: Optional[int] = None
    attempts: int = 0              # consecutive failed attempts
    permanent_failures: int = 0
    reconnects: int = 0            # automatic restarts since the user turned it on
    next_retry_at: Optional[float] = None
    stopping: bool = False
    kill_deadline: Optional[float] = None
    fast_retry: bool = False
    last_error: Optional[dict] = None
    target_warning: Optional[dict] = None
    last_transfer: Optional[dict] = None
    log_offset: int = 0
    attempt_lines: Deque[str] = field(default_factory=lambda: collections.deque(maxlen=60))

    def public(self) -> dict:
        return {
            "status": self.status,
            "pid": self.pid,
            "adopted": self.adopted,
            "spawned_at": self.spawned_at,
            "connected_at": self.connected_at,
            "connect_ms": self.connect_ms,
            "attempts": self.attempts,
            "reconnects": self.reconnects,
            "next_retry_at": self.next_retry_at,
            "last_error": self.last_error,
            "target_warning": self.target_warning,
            "last_transfer": self.last_transfer,
        }


def _err_dict(e: SshError) -> dict:
    return {"kind": e.kind, "message": e.message, "hint": e.hint, "permanent": e.permanent, "at": time.time()}


def _connections(proc: psutil.Process) -> list:
    fn = getattr(proc, "net_connections", None) or proc.connections
    return fn(kind="inet")


def port_owner(port: int) -> Optional[str]:
    """'<command> (pid N)' of whoever listens on a local TCP port, via lsof."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fpc"],
            capture_output=True, text=True, timeout=3,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    pid = cmd = None
    for line in out.splitlines():
        if line.startswith("p"):
            pid = line[1:]
        elif line.startswith("c"):
            cmd = line[1:]
            break
    return f"{cmd} (pid {pid})" if pid else None


def port_free(bind: str, port: int) -> bool:
    host = "127.0.0.1" if bind in ("", "localhost", "*") else bind.strip("[]")
    if bind == "*":
        host = "0.0.0.0"
    fam = socket.AF_INET6 if ":" in host else socket.AF_INET
    s = socket.socket(fam, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # same as ssh
        s.bind((host, port))
        return True
    except OSError as e:
        return e.errno not in (errno.EADDRINUSE, errno.EACCES)
    finally:
        s.close()


class Supervisor:
    def __init__(self, store: Store, netwatch: Optional[NetWatch] = None):
        self.store = store
        self.netwatch = netwatch
        # recent drops / recoveries / failures; the web UI turns new ones into
        # browser notifications
        self.events: Deque[dict] = collections.deque(maxlen=50)
        self._event_seq = 0
        self.rt: Dict[str, Runtime] = {}
        self.online = True
        self._lock = threading.RLock()
        self._listeners: List[Callable[[], None]] = []
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._dirty = False

    # ------------------------------------------------------------------ public

    def on_change(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def snapshot(self) -> Dict[str, dict]:
        with self._lock:
            return {tid: r.public() for tid, r in self.rt.items()}

    def recent_events(self) -> List[dict]:
        with self._lock:
            return list(self.events)

    def _event(self, kind: str, t: Tunnel, message: str) -> None:
        """kind: 'down' | 'up' | 'failed'"""
        self._event_seq += 1
        self.events.append({"seq": self._event_seq, "at": time.time(), "kind": kind,
                            "tunnel_id": t.id, "name": t.name, "message": message})

    def runtime(self, tunnel_id: str) -> Runtime:
        with self._lock:
            return self.rt.setdefault(tunnel_id, Runtime())

    def start_loop(self) -> None:
        if self.netwatch is None:
            self.netwatch = NetWatch(self.network_event)
        self.online = self.netwatch.online
        self.netwatch.start()
        self._thread = threading.Thread(target=self._run, name="supervisor", daemon=True)
        self._thread.start()

    def shutdown(self, stop_tunnels: bool = False) -> int:
        """Stop the loop. Tunnels keep running (and get re-adopted next start)
        unless ``stop_tunnels``. Returns the number of tunnels left running."""
        if stop_tunnels:
            self.stop_all(wait=True)
        self._stop.set()
        if self.netwatch:
            self.netwatch.stop()
        if self._thread:
            self._thread.join(timeout=3)
        with self._lock:
            self._save_runtime()
            return sum(1 for r in self.rt.values() if r.pid)

    def start(self, tunnel_id: str) -> None:
        t = self.store.get(tunnel_id)
        if t is None:
            raise KeyError(tunnel_id)
        with self._lock:
            r = self.runtime(tunnel_id)
            if r.pid and not r.stopping:
                return  # already running
            r.attempts = r.permanent_failures = r.reconnects = 0
            r.last_error = r.target_warning = None
            r.next_retry_at = None
            self.store.touch(tunnel_id, desired="running", last_used_at=time.time(), use_count=t.use_count + 1)
            self._spawn(self.store.get(tunnel_id), r)
        self._changed()

    def stop(self, tunnel_id: str, wait: bool = False) -> None:
        with self._lock:
            r = self.runtime(tunnel_id)
            if self.store.get(tunnel_id):
                self.store.touch(tunnel_id, desired="stopped")
            r.next_retry_at = None
            if r.pid:
                self._terminate(r)
            else:
                r.status = "stopped"
        if wait:
            self._wait_dead(tunnel_id)
        self._changed()

    def restart(self, tunnel_id: str) -> None:
        r = self.runtime(tunnel_id)
        if r.pid:
            with self._lock:
                self._terminate(r)
            self._wait_dead(tunnel_id)
        self.start(tunnel_id)

    def forget(self, tunnel_id: str) -> None:
        """Stop and drop all runtime state (tunnel deleted)."""
        self.stop(tunnel_id, wait=True)
        with self._lock:
            self.rt.pop(tunnel_id, None)
            self._save_runtime()
        try:
            for p in (paths.tunnel_log(tunnel_id), paths.tunnel_log(tunnel_id).with_suffix(".log.1")):
                p.unlink(missing_ok=True)
        except OSError:
            pass
        self._changed()

    def stop_all(self, wait: bool = False) -> None:
        for tid in [tid for tid, r in self.rt.items() if r.pid or r.status in ("retrying", "offline")]:
            self.stop(tid, wait=wait)

    def restore(self) -> None:
        """Adopt ssh processes left by a previous manager and restart
        keep-alive tunnels that should be running."""
        records = self._load_runtime()
        with self._lock:
            for tid, rec in records.items():
                t = self.store.get(tid)
                proc = self._verify(rec)
                if proc is None:
                    continue
                if t is None:
                    log.info("killing orphaned ssh pid %s (tunnel %s no longer exists)", rec.get("pid"), tid)
                    self._kill_pid(proc.pid)
                    continue
                r = self.runtime(tid)
                r.pid, r.adopted, r.argv = proc.pid, True, list(rec["argv"])
                r.spawned_at = rec.get("spawned_at")
                r.connected_at = r.authenticated_at = rec.get("connected_at")
                r.connect_ms = rec.get("connect_ms")
                r.status = "up" if r.connected_at else "starting"
                # re-read this attempt's log so e.g. "Authenticated to" is seen
                r.log_offset = self._last_attempt_offset(tid)
                if t.desired != "running":
                    self.store.touch(tid, desired="running")
                log.info("adopted %s (pid %s)", t.name, proc.pid)
            for t in self.store.tunnels():
                r = self.runtime(t.id)
                if t.desired == "running" and not r.pid:
                    if t.keep_alive:
                        self._spawn(t, r)
                    else:
                        self.store.touch(t.id, desired="stopped")
            self._save_runtime()
        self._changed()

    def network_event(self, event: str, online: bool) -> None:
        """Called by NetWatch on 'change' or 'wake'."""
        now = time.time()
        addresses = self.netwatch.addresses if self.netwatch else frozenset()
        with self._lock:
            self.online = online
            for tid, r in self.rt.items():
                if not online:
                    if r.status == "retrying":
                        r.status, r.next_retry_at = "offline", None
                elif r.status in ("retrying", "offline"):
                    r.status, r.next_retry_at = "retrying", now
                elif r.pid and r.status in ("up", "starting") and self._stale(r, addresses):
                    log.info("restarting %s: its connection uses an address that is gone", tid)
                    r.fast_retry = True
                    self._kill_pid(r.pid, sig=signal.SIGTERM)
        self._changed()

    # ------------------------------------------------------------------ loop

    def _run(self) -> None:
        while not self._stop.wait(TICK):
            try:
                self.tick()
            except Exception:  # pragma: no cover - keep supervising no matter what
                log.exception("supervisor tick failed")

    def tick(self) -> None:
        now = time.time()
        with self._lock:
            for tid, r in list(self.rt.items()):
                t = self.store.get(tid)
                if r.pid:
                    self._check_process(t, r, now)
                elif r.status == "retrying" and r.next_retry_at is not None and now >= r.next_retry_at:
                    if t is None or t.desired != "running":
                        r.status = "stopped"
                        continue
                    r.reconnects += 1
                    self._spawn(t, r)
            if self._dirty:
                self._dirty = False
                self._save_runtime()
                changed = True
            else:
                changed = False
        if changed:
            self._changed()

    def _check_process(self, t: Optional[Tunnel], r: Runtime, now: float) -> None:
        for line in self._read_log(t.id if t else None, r):
            r.attempt_lines.append(line)
            if _AUTH_RE.search(line) and not r.authenticated_at:
                r.authenticated_at = now
                self._dirty = True
            m = _TRANSFER_RE.search(line)
            if m:
                r.last_transfer = {"sent": int(m.group(1)), "received": int(m.group(2)), "at": now}
            if "open failed:" in line:
                err = classify_error([line])
                if err:
                    r.target_warning = _err_dict(err)
                    self._dirty = True

        if self._alive(r):
            if r.kill_deadline and now > r.kill_deadline:
                self._kill_pid(r.pid, sig=signal.SIGKILL)
                r.kill_deadline = None
            if r.status == "starting" and t is not None and r.authenticated_at and self._forwards_ready(t, r, now):
                r.status, r.connected_at = "up", now
                if r.spawned_at:
                    r.connect_ms = int((r.authenticated_at - r.spawned_at) * 1000)
                if r.reconnects or r.attempts:
                    self._event("up", t, "Reconnected.")
                self._dirty = True
            elif r.status == "starting" and r.spawned_at and now - r.spawned_at > self._start_timeout():
                log.warning("tunnel %s stuck connecting; killing", t.name if t else r.pid)
                r.attempt_lines.append("Connection timed out (no authentication within the time limit)")
                self._kill_pid(r.pid, sig=signal.SIGKILL)
            elif r.status == "up" and r.connected_at and now - r.connected_at > STABLE_AFTER:
                r.attempts = r.permanent_failures = 0
            return
        self._on_exit(t, r, now)

    def _start_timeout(self) -> float:
        s = self.store.settings
        return s.connect_timeout * 3 + 15

    def _on_exit(self, t: Optional[Tunnel], r: Runtime, now: float, err: Optional[SshError] = None) -> None:
        code = None
        if r.proc is not None:
            code = r.proc.poll()
        # drain the last log lines written before exit
        if t is not None:
            for line in self._read_log(t.id, r):
                r.attempt_lines.append(line)
                m = _TRANSFER_RE.search(line)
                if m:
                    r.last_transfer = {"sent": int(m.group(1)), "received": int(m.group(2)), "at": now}
        was_up = r.status == "up"
        up_for = now - r.connected_at if (was_up and r.connected_at) else 0
        r.proc, r.pid, r.adopted, r.kill_deadline = None, None, False, None
        r.connected_at = None
        self._dirty = True

        if r.stopping or t is None:
            r.stopping, r.status = False, "stopped"
            return

        if err is None:
            err = classify_error(r.attempt_lines)
        if err is None or err.kind == "target":
            err = SshError("network", f"ssh exited (code {code})" if code is not None else "ssh exited",
                           "The connection ended unexpectedly.", False)
        r.last_error = _err_dict(err)

        if not (t.desired == "running" and t.keep_alive):
            r.status = "failed"
            self.store.touch(t.id, desired="stopped")
            if was_up:
                self._event("down", t, err.message)
            return

        if err.permanent:
            r.permanent_failures += 1
            # retrying never fixes a broken definition; auth/host-key get a few tries
            if err.kind == "config" or r.permanent_failures >= MAX_PERMANENT_FAILURES:
                r.status = "failed"
                self.store.touch(t.id, desired="stopped")
                self._event("failed", t, err.message)
                return
        if was_up and up_for >= 5:
            self._event("down", t, f"{err.message} - reconnecting…")
        if not self.online:
            r.status, r.next_retry_at = "offline", None
            return
        if r.fast_retry or (was_up and up_for >= STABLE_AFTER):
            delay = 0.0 if r.fast_retry else 1.0
            r.attempts = 0
        else:
            r.attempts += 1
            delay = min(MAX_BACKOFF, 2.0 ** (r.attempts - 1))
        r.fast_retry = False
        r.status, r.next_retry_at = "retrying", now + delay

    # ------------------------------------------------------------------ helpers

    def _spawn(self, t: Tunnel, r: Runtime) -> None:
        now = time.time()
        r.stopping, r.fast_retry = False, False
        r.attempt_lines.clear()
        r.authenticated_at = r.connected_at = None
        r.next_retry_at = None

        err = self._preflight(t)
        if err is not None:
            r.spawned_at, r.status, r.pid = now, "starting", None
            self._on_exit(t, r, now, err)
            return

        argv = build_argv(t, self.store.settings, paths.ssh_binary())
        logp = paths.tunnel_log(t.id)
        try:
            if logp.exists() and logp.stat().st_size > LOG_ROTATE_BYTES:
                os.replace(logp, logp.with_suffix(".log.1"))
        except OSError:
            pass
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n=== {stamp}  starting: {display_command(t)}\n"
        with open(logp, "ab") as lf:
            lf.write(header.encode())
            lf.flush()
            offset = lf.tell()
            try:
                proc = subprocess.Popen(
                    argv,
                    stdin=subprocess.DEVNULL,
                    stdout=lf,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                )
            except OSError as e:
                r.spawned_at, r.status = now, "starting"
                self._on_exit(t, r, now, SshError("config", f"could not run ssh: {e}",
                                                  "Is OpenSSH installed and on PATH?", True))
                return
        r.proc, r.pid, r.argv, r.adopted = proc, proc.pid, argv, False
        r.spawned_at, r.status, r.log_offset = now, "starting", offset
        self._dirty = True
        log.info("started %s (pid %s)", t.name, proc.pid)

    def _preflight(self, t: Tunnel) -> Optional[SshError]:
        if t.identity_file and not os.path.exists(os.path.expanduser(t.identity_file)):
            return SshError("config", f"Identity file not found: {t.identity_file}",
                            "Pick an existing private key in the tunnel settings.", True)
        if any(f.socket_has_tilde for f in t.forwards):
            return SshError("config", "Socket path starts with '~', which sshd does not expand",
                            "Edit the tunnel and use an absolute path such as /home/<user>/...", True)
        for bind, port in t.local_ports():
            if not port_free(bind, port):
                owner = port_owner(port)
                mine = self._owner_tunnel(owner)
                who = f"tunnel '{mine}'" if mine else (owner or "another process")
                return SshError("local_port", f"Local port {port} is already in use by {who}",
                                "Stop the other process/tunnel or change the local port.", False)
        return None

    def _owner_tunnel(self, owner: Optional[str]) -> Optional[str]:
        if not owner:
            return None
        m = re.search(r"pid (\d+)", owner)
        if not m:
            return None
        pid = int(m.group(1))
        for tid, r in self.rt.items():
            if r.pid == pid:
                t = self.store.get(tid)
                return t.name if t else tid
        return None

    def _forwards_ready(self, t: Tunnel, r: Runtime, now: float) -> bool:
        wanted = {port for _, port in t.local_ports()}
        if not wanted:
            return now - (r.authenticated_at or now) >= R_ONLY_GRACE
        try:
            listening = {c.laddr.port for c in _connections(psutil.Process(r.pid)) if c.status == psutil.CONN_LISTEN}
        except (psutil.Error, OSError):
            return now - (r.authenticated_at or now) >= 1.0
        return wanted <= listening

    def _stale(self, r: Runtime, addresses) -> bool:
        """True if the ssh connection is bound to a local IP that disappeared."""
        if not r.pid or not addresses:
            return False
        try:
            p = psutil.Process(r.pid)
            procs = [p] + p.children(recursive=True)
            for proc in procs:
                for c in _connections(proc):
                    if c.status != psutil.CONN_ESTABLISHED or not c.laddr:
                        continue
                    ip = c.laddr.ip.split("%", 1)[0]
                    try:
                        obj = ipaddress.ip_address(ip)
                    except ValueError:
                        continue
                    if obj.is_loopback or obj.is_link_local:
                        continue
                    if ip not in addresses:
                        return True
        except (psutil.Error, OSError):
            return False
        return False

    def _alive(self, r: Runtime) -> bool:
        if r.proc is not None:
            return r.proc.poll() is None
        if r.pid is None:
            return False
        try:
            p = psutil.Process(r.pid)
            return p.is_running() and p.status() != psutil.STATUS_ZOMBIE
        except psutil.Error:
            return False

    def _read_log(self, tunnel_id: Optional[str], r: Runtime) -> List[str]:
        if tunnel_id is None:
            return []
        try:
            with open(paths.tunnel_log(tunnel_id), "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                if size < r.log_offset:  # rotated/truncated
                    r.log_offset = 0
                if size == r.log_offset:
                    return []
                f.seek(r.log_offset)
                data = f.read(size - r.log_offset)
        except OSError:
            return []
        # only consume complete lines
        cut = data.rfind(b"\n")
        if cut < 0:
            return []
        r.log_offset += cut + 1
        return data[: cut + 1].decode("utf-8", errors="replace").splitlines()

    @staticmethod
    def _last_attempt_offset(tunnel_id: str) -> int:
        """Offset of the first line after the last '=== ... starting' header."""
        p = paths.tunnel_log(tunnel_id)
        try:
            with open(p, "rb") as f:
                f.seek(0, os.SEEK_END)
                size = f.tell()
                start = max(0, size - 65536)
                f.seek(start)
                data = f.read()
        except OSError:
            return 0
        idx = data.rfind(b"\n=== ")
        if idx < 0:
            return size
        eol = data.find(b"\n", idx + 1)
        return start + (eol + 1 if eol >= 0 else len(data))

    def _terminate(self, r: Runtime) -> None:
        r.stopping = True
        r.kill_deadline = time.time() + KILL_GRACE
        self._kill_pid(r.pid, sig=signal.SIGTERM)

    def _kill_pid(self, pid: Optional[int], sig: int = signal.SIGTERM) -> None:
        if not pid:
            return
        try:
            # ssh runs in its own session: signal the group so a ProxyJump
            # helper ssh dies too
            os.killpg(pid, sig)
        except (ProcessLookupError, PermissionError):
            try:
                os.kill(pid, sig)
            except OSError:
                pass

    def _wait_dead(self, tunnel_id: str, timeout: float = KILL_GRACE + 1) -> None:
        r = self.runtime(tunnel_id)
        deadline = time.time() + timeout
        while time.time() < deadline:
            with self._lock:
                if not r.pid:
                    return
                t = self.store.get(tunnel_id)
                if not self._alive(r):
                    self._on_exit(t, r, time.time())
                    return
                if r.kill_deadline and time.time() > r.kill_deadline:
                    self._kill_pid(r.pid, sig=signal.SIGKILL)
            time.sleep(0.05)

    def _changed(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # pragma: no cover
                log.exception("supervisor listener failed")

    # ------------------------------------------------------------------ runtime.json

    def _save_runtime(self) -> None:
        data = {}
        for tid, r in self.rt.items():
            if not r.pid:
                continue
            try:
                ctime = psutil.Process(r.pid).create_time()
            except psutil.Error:
                continue
            data[tid] = {
                "pid": r.pid,
                "create_time": ctime,
                "argv": r.argv,
                "spawned_at": r.spawned_at,
                "connected_at": r.connected_at,
                "connect_ms": r.connect_ms,
            }
        try:
            atomic_write(paths.runtime_file(), json.dumps(data, indent=2))
        except OSError:
            log.exception("could not write runtime.json")

    def _load_runtime(self) -> Dict[str, dict]:
        try:
            return json.loads(paths.runtime_file().read_text())
        except (OSError, ValueError):
            return {}

    @staticmethod
    def _verify(rec: dict) -> Optional[psutil.Process]:
        """The process from a runtime.json record, if it is still exactly the
        ssh we started (guards against pid reuse)."""
        try:
            p = psutil.Process(int(rec["pid"]))
            if abs(p.create_time() - float(rec["create_time"])) > 1.0:
                return None
            argv, cmd = list(rec["argv"]), p.cmdline()
            # compare the arguments (they contain every forward and the host);
            # argv[0] may be shown differently (e.g. resolved path / wrapper)
            if len(argv) < 2 or cmd[-(len(argv) - 1):] != argv[1:]:
                return None
            if p.status() == psutil.STATUS_ZOMBIE:
                return None
            return p
        except (psutil.Error, KeyError, ValueError, TypeError):
            return None
