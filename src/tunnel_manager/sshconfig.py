"""Reading ~/.ssh/config: host aliases, effective settings (`ssh -G`) and keys."""

from __future__ import annotations

import glob
import os
import shlex
import subprocess
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Set

from . import paths

_PATTERN_CHARS = set("*?!")


def _expand_include(arg: str, base: Path) -> List[Path]:
    out: List[Path] = []
    for item in shlex.split(arg):
        p = os.path.expanduser(item)
        if not os.path.isabs(p):
            p = str(base / p)
        out += [Path(x) for x in sorted(glob.glob(p))]
    return out


def list_hosts(config: Optional[Path] = None) -> List[str]:
    """Concrete Host aliases from the ssh config (following Include), in file
    order, without wildcard patterns or negations."""
    ssh_dir = paths.ssh_dir()
    config = config or ssh_dir / "config"
    seen: Set[Path] = set()
    hosts: List[str] = []

    def walk(path: Path) -> None:
        try:
            real = path.resolve()
        except OSError:
            return
        if real in seen or not path.is_file():
            return
        seen.add(real)
        try:
            text = path.read_text(errors="replace")
        except OSError:
            return
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            key, _, value = line.replace("=", " ", 1).partition(" ")
            key, value = key.lower(), value.strip()
            if key == "include":
                for inc in _expand_include(value, ssh_dir):
                    walk(inc)
            elif key == "host":
                try:
                    names = shlex.split(value)
                except ValueError:
                    names = value.split()
                for name in names:
                    if not (_PATTERN_CHARS & set(name)) and name not in hosts:
                        hosts.append(name)

    walk(config)
    return hosts


# --- ssh -G ------------------------------------------------------------------------

_RESOLVE_KEYS = ("hostname", "user", "port", "identityfile", "proxyjump", "proxycommand", "controlmaster")
_cache: Dict[tuple, tuple] = {}
_cache_lock = threading.Lock()
_CACHE_TTL = 60.0


def _config_mtime() -> float:
    try:
        return (paths.ssh_dir() / "config").stat().st_mtime
    except OSError:
        return 0.0


def resolve(host: str, user: Optional[str] = None, port: Optional[int] = None) -> Dict[str, object]:
    """Effective configuration for a host as ssh itself sees it (`ssh -G`).

    Returns a dict with hostname, user, port, identityfile (list), proxyjump,
    or {"error": ...} if ssh could not evaluate it.
    """
    key = (host, user, port, _config_mtime())
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL:
            return dict(hit[1])
    argv = [paths.ssh_binary(), "-G"]
    if user:
        argv += ["-l", user]
    if port:
        argv += ["-p", str(port)]
    argv.append(host)
    result: Dict[str, object]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=5)
        if proc.returncode != 0:
            result = {"error": (proc.stderr or "ssh -G failed").strip()}
        else:
            result = {"identityfile": []}
            for line in proc.stdout.splitlines():
                k, _, v = line.partition(" ")
                if k == "identityfile":
                    result["identityfile"].append(v)  # type: ignore[union-attr]
                elif k in _RESOLVE_KEYS:
                    result[k] = v
            if result.get("proxyjump") == "none":
                result.pop("proxyjump")
            if result.get("proxycommand") == "none":
                result.pop("proxycommand")
            existing = [p for p in result["identityfile"] if os.path.exists(os.path.expanduser(p))]  # type: ignore[union-attr]
            result["identityfile"] = existing
    except (OSError, subprocess.TimeoutExpired) as e:
        result = {"error": str(e)}
    with _cache_lock:
        _cache[key] = (now, result)
    return dict(result)


def remote_home(host: str, user: Optional[str] = None) -> Optional[str]:
    """Best guess of the remote home directory (used to fix `~/` socket paths)."""
    who = user or resolve(host).get("user")
    if not who or not isinstance(who, str):
        return None
    return "/root" if who == "root" else f"/home/{who}"


# --- keys -------------------------------------------------------------------------


def list_keys() -> List[Dict[str, str]]:
    """Private keys in ~/.ssh that have a matching .pub file."""
    d = paths.ssh_dir()
    keys = []
    try:
        entries = sorted(d.iterdir())
    except OSError:
        return []
    for pub in entries:
        if pub.suffix != ".pub":
            continue
        priv = pub.with_suffix("")
        if priv.is_file():
            try:
                comment = pub.read_text(errors="replace").split(None, 2)
                kind = comment[0] if comment else ""
            except OSError:
                kind = ""
            keys.append({"path": _tilde(priv), "type": kind})
    return keys


def _tilde(p: Path) -> str:
    s, home = str(p), str(Path.home())
    return "~" + s[len(home):] if s.startswith(home + os.sep) else s
