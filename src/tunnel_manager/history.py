"""Import past tunnels from shell history (zsh and bash)."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

from pydantic import ValidationError

from .models import Tunnel
from .sshcmd import parse_ssh_command

_ZSH_EXTENDED = re.compile(rb"^: (\d+):\d+;")
_META = 0x83


@dataclass
class HistoryEntry:
    command: str
    timestamp: Optional[float]
    source: str


def _unmetafy(data: bytes) -> bytes:
    """zsh stores some bytes as Meta (0x83) followed by byte ^ 32."""
    if _META not in data:
        return data
    out = bytearray()
    it = iter(data)
    for b in it:
        if b == _META:
            nxt = next(it, None)
            if nxt is not None:
                out.append(nxt ^ 32)
        else:
            out.append(b)
    return bytes(out)


def read_zsh_history(path: Path) -> Iterator[HistoryEntry]:
    try:
        raw = path.read_bytes()
    except OSError:
        return
    lines = _unmetafy(raw).split(b"\n")
    i = 0
    while i < len(lines):
        line = lines[i]
        ts: Optional[float] = None
        m = _ZSH_EXTENDED.match(line)
        if m:
            ts = float(m.group(1))
            line = line[m.end():]
        # multi-line commands are written with a trailing backslash
        while line.endswith(b"\\") and i + 1 < len(lines):
            i += 1
            line = line[:-1] + b"\n" + lines[i]
        i += 1
        cmd = line.decode("utf-8", errors="replace").strip()
        if cmd:
            yield HistoryEntry(cmd, ts, "zsh_history")


def read_bash_history(path: Path) -> Iterator[HistoryEntry]:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return
    ts: Optional[float] = None
    for line in text.splitlines():
        if line.startswith("#") and line[1:].isdigit():
            ts = float(line[1:])
            continue
        if line.strip():
            yield HistoryEntry(line.strip(), ts, "bash_history")
        ts = None


def default_history_files() -> List[Tuple[Path, Callable[[Path], Iterator[HistoryEntry]]]]:
    home = Path.home()
    zsh = Path(os.environ.get("HISTFILE", "")) if os.environ.get("HISTFILE", "").endswith("zsh_history") else home / ".zsh_history"
    return [(zsh, read_zsh_history), (home / ".bash_history", read_bash_history)]


def tunnels_from_history(
    entries: Iterable[HistoryEntry],
    remote_home: Optional[Callable[[str, Optional[str]], Optional[str]]] = None,
) -> List[Tunnel]:
    """Turn history entries into unique Tunnel candidates.

    ``remote_home(host, user)`` is used to rewrite ``~/`` socket paths (which
    sshd does not expand) into absolute ones, so the two spellings dedupe.
    """
    found: Dict[tuple, Tunnel] = {}
    home_cache: Dict[tuple, Optional[str]] = {}
    for entry in entries:
        if "ssh" not in entry.command:
            continue
        parsed = parse_ssh_command(entry.command)
        if not parsed:
            continue
        forwards = parsed.forwards
        if remote_home:
            for f in forwards:
                if f.target_socket and f.target_socket.startswith("~/"):
                    k = (parsed.host, parsed.user)
                    if k not in home_cache:
                        home_cache[k] = remote_home(parsed.host, parsed.user)
                    if home_cache[k]:
                        f.target_socket = home_cache[k] + f.target_socket[1:]
        try:
            t = Tunnel(
                host=parsed.host,
                user=parsed.user,
                port=parsed.port,
                identity_file=parsed.identity_file,
                jump=parsed.jump,
                forwards=forwards,
                source=entry.source,
            )
        except (ValidationError, ValueError):
            continue
        ts = entry.timestamp
        key = t.dedupe_key()
        prev = found.get(key)
        if prev is None:
            t.use_count = 1
            if ts:
                t.created_at = t.last_used_at = ts
            found[key] = t
        else:
            prev.use_count += 1
            if ts:
                prev.last_used_at = max(prev.last_used_at, ts)
                prev.created_at = min(prev.created_at, ts)
    return sorted(found.values(), key=lambda t: t.last_used_at, reverse=True)


def scan_default_history(remote_home=None) -> List[Tunnel]:
    entries: List[HistoryEntry] = []
    for path, reader in default_history_files():
        entries.extend(reader(path))
    return tunnels_from_history(entries, remote_home=remote_home)
