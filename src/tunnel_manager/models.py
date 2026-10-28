"""Data model: forwards, tunnels, settings and the persisted state document.

Everything that ends up on an ssh command line is validated here, so that a
value coming from the UI or from shell history can never smuggle in an extra
ssh option (e.g. a host named ``-oProxyCommand=...``).
"""

from __future__ import annotations

import hashlib
import re
import secrets
import time
from typing import List, Literal, Optional, Tuple

from pydantic import BaseModel, Field, field_validator, model_validator

# --- validation patterns -----------------------------------------------------

# Host alias, DNS name or IPv4. Must not start with "-" (option injection).
_HOST_RE = re.compile(r"^(?!-)[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
# Bare or bracketed IPv6 (optionally with a zone id).
_IPV6_RE = re.compile(r"^\[?[0-9A-Fa-f:.]+(%[A-Za-z0-9_.\-]+)?\]?$")
_USER_RE = re.compile(r"^(?!-)[A-Za-z0-9_.][A-Za-z0-9_.\-$]*$")
# Remote unix socket: absolute or ~-relative path, no whitespace or colons.
_SOCKET_RE = re.compile(r"^[~/][^\s:]*$")
_IDENTITY_RE = re.compile(r"^(?!-)[^\n\r\0]+$")

LOOPBACK_BINDS = {"", "localhost", "127.0.0.1"}
VNC_PORTS = range(5900, 6000)


def is_valid_host(value: str) -> bool:
    if not value or len(value) > 255:
        return False
    if _HOST_RE.match(value):
        return True
    return ":" in value and bool(_IPV6_RE.match(value))


def _check_host(value: Optional[str], what: str) -> Optional[str]:
    if value is None:
        return None
    value = value.strip()
    if value and not is_valid_host(value):
        raise ValueError(f"invalid {what}: {value!r}")
    return value or None


def new_id() -> str:
    return "t_" + secrets.token_hex(4)


# --- forwards ------------------------------------------------------------------

ForwardType = Literal["L", "R", "D"]


class Forward(BaseModel):
    """One -L / -R / -D forward.

    * ``L``: local ``[bind:]listen_port`` -> remote ``target_host:target_port``
      or remote unix socket ``target_socket``.
    * ``R``: remote ``[bind:]listen_port`` -> local ``target_host:target_port``.
    * ``D``: local SOCKS proxy on ``[bind:]listen_port``.
    """

    type: ForwardType = "L"
    bind: Optional[str] = None
    listen_port: int = Field(ge=1, le=65535)
    target_host: Optional[str] = None
    target_port: Optional[int] = Field(default=None, ge=1, le=65535)
    target_socket: Optional[str] = None

    @field_validator("bind", "target_host")
    @classmethod
    def _hosts(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if v == "*":
            return v
        return _check_host(v, "address")

    @field_validator("target_socket")
    @classmethod
    def _socket(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not _SOCKET_RE.match(v):
            raise ValueError("socket path must be absolute (start with /) and contain no spaces or ':'")
        return v

    @model_validator(mode="after")
    def _shape(self) -> "Forward":
        if self.type == "D":
            self.target_host = self.target_port = self.target_socket = None
        elif self.type == "R":
            self.target_socket = None
            if not self.target_host or not self.target_port:
                raise ValueError("-R needs a local target host and port")
        else:  # L
            if self.target_socket:
                self.target_host = self.target_port = None
            elif not self.target_host or not self.target_port:
                raise ValueError("-L needs a remote target host:port or a remote socket path")
        return self

    # -- rendering --

    @staticmethod
    def _fmt_host(h: str) -> str:
        return f"[{h.strip('[]')}]" if ":" in h else h

    def spec(self) -> str:
        """The argument for -L/-R/-D, exactly as ssh expects it."""
        parts: List[str] = []
        if self.bind:
            parts.append(self._fmt_host(self.bind))
        parts.append(str(self.listen_port))
        if self.type != "D":
            if self.target_socket:
                parts.append(self.target_socket)
            else:
                parts.append(self._fmt_host(self.target_host or ""))
                parts.append(str(self.target_port))
        return ":".join(parts)

    def target_label(self) -> str:
        if self.type == "D":
            return "SOCKS"
        if self.target_socket:
            return self.target_socket
        return f"{self._fmt_host(self.target_host or '')}:{self.target_port}"

    def key(self) -> Tuple:
        bind = "" if (self.bind or "") in LOOPBACK_BINDS else self.bind
        return (self.type, bind, self.listen_port, self.target_host or "", self.target_port or 0, self.target_socket or "")

    @property
    def socket_has_tilde(self) -> bool:
        return bool(self.target_socket and self.target_socket.startswith("~"))


# --- tunnels -------------------------------------------------------------------

Desired = Literal["running", "stopped"]


class Tunnel(BaseModel):
    id: str = Field(default_factory=new_id)
    name: str = ""
    host: str
    user: Optional[str] = None
    port: Optional[int] = Field(default=None, ge=1, le=65535)
    identity_file: Optional[str] = None
    jump: Optional[str] = None
    forwards: List[Forward] = Field(min_length=1)
    keep_alive: bool = False
    favorite: bool = False
    order: int = 0
    open_url: Optional[str] = None
    desired: Desired = "stopped"
    created_at: float = Field(default_factory=time.time)
    last_used_at: float = Field(default_factory=time.time)
    use_count: int = 0
    source: str = "manual"

    @field_validator("host")
    @classmethod
    def _host(cls, v: str) -> str:
        v = (v or "").strip()
        if not is_valid_host(v):
            raise ValueError(f"invalid host: {v!r}")
        return v

    @field_validator("user")
    @classmethod
    def _user(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _USER_RE.match(v):
            raise ValueError(f"invalid user name: {v!r}")
        return v

    @field_validator("identity_file")
    @classmethod
    def _identity(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not _IDENTITY_RE.match(v):
            raise ValueError("invalid identity file path")
        return v

    @field_validator("jump")
    @classmethod
    def _jump(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        for hop in v.split(","):
            user, _, rest = hop.rpartition("@")
            if user and not _USER_RE.match(user):
                raise ValueError(f"invalid jump host user: {user!r}")
            host, port = rest, None
            if rest.startswith("["):  # [v6]:port
                host, _, tail = rest[1:].partition("]")
                port = tail[1:] if tail.startswith(":") else None
            elif rest.count(":") == 1:
                host, port = rest.split(":")
            if not is_valid_host(host) or (port is not None and not port.isdigit()):
                raise ValueError(f"invalid jump host: {hop!r}")
        return v

    @field_validator("open_url")
    @classmethod
    def _open_url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        v = v.strip()
        if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://\S+$", v):
            raise ValueError("open URL must look like scheme://...")
        if v.split(":", 1)[0].lower() in {"javascript", "data", "file"}:
            raise ValueError("unsupported URL scheme")
        return v

    @model_validator(mode="after")
    def _defaults(self) -> "Tunnel":
        self.name = (self.name or "").strip()[:120] or self.auto_name()
        return self

    # -- derived --

    def dedupe_key(self) -> Tuple:
        return (
            self.host.lower(),
            self.user or "",
            self.port or 0,
            (self.jump or "").lower(),
            tuple(sorted(f.key() for f in self.forwards)),
        )

    def key_hash(self) -> str:
        return hashlib.sha1(repr(self.dedupe_key()).encode()).hexdigest()[:16]

    def auto_name(self) -> str:
        bits = [_short_forward(f) for f in self.forwards[:2]]
        if len(self.forwards) > 2:
            bits.append(f"+{len(self.forwards) - 2}")
        who = f"{self.user}@{self.host}" if self.user else self.host
        return f"{who} {' '.join(bits)}"

    def destination(self) -> str:
        return f"{self.user}@{self.host}" if self.user else self.host

    def default_open_url(self) -> Optional[str]:
        if self.open_url:
            return self.open_url
        for f in self.forwards:
            if f.type == "L":
                scheme = "vnc" if f.listen_port in VNC_PORTS else "http"
                return f"{scheme}://localhost:{f.listen_port}"
        return None

    def local_ports(self) -> List[Tuple[str, int]]:
        """(bind, port) pairs this tunnel listens on locally (L and D)."""
        return [(f.bind or "127.0.0.1", f.listen_port) for f in self.forwards if f.type in ("L", "D")]


def _short_forward(f: Forward) -> str:
    if f.type == "D":
        return f"SOCKS:{f.listen_port}"
    if f.target_socket:
        return f":{f.listen_port}→{f.target_socket.rsplit('/', 1)[-1]}"
    prefix = "R" if f.type == "R" else ""
    if f.target_port == f.listen_port and (f.target_host or "") in ("localhost", "127.0.0.1"):
        return f"{prefix}:{f.listen_port}"
    return f"{prefix}:{f.listen_port}→{f.target_port}"


# --- settings & state ------------------------------------------------------------


class Settings(BaseModel):
    accept_new_host_keys: bool = False
    server_alive_interval: int = Field(default=10, ge=1, le=600)
    server_alive_count_max: int = Field(default=3, ge=1, le=20)
    connect_timeout: int = Field(default=10, ge=1, le=120)
    theme: Literal["auto", "light", "dark"] = "auto"
    history_sort: Literal["recent", "name", "host"] = "recent"


class State(BaseModel):
    version: int = 1
    settings: Settings = Field(default_factory=Settings)
    tunnels: List[Tunnel] = Field(default_factory=list)
    history_imported_at: Optional[float] = None
    # dedupe-key hashes of tunnels the user deleted, so a history re-import
    # does not resurrect them
    ignored_keys: List[str] = Field(default_factory=list)
