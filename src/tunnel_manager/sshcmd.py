"""Building ssh command lines, parsing them back, and explaining ssh errors."""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from typing import Iterable, List, Optional, Tuple

from .models import Forward, Settings, Tunnel

# ssh(1) options that take an argument (OpenSSH 9/10).
_OPTS_WITH_ARG = set("BbcDEeFIiJLlmOoPpQRSWw")
_OPTS_NO_ARG = set("46AaCfGgKkMNnqsTtVvXxYy")

# Options that turn every tunnel into an independent, key-only, self-healing
# connection. ControlMaster/ControlPath matter: with "ControlMaster auto" in
# ~/.ssh/config a plain `ssh -L` would ride on an existing master connection
# and could not be stopped or supervised on its own.
def base_options(settings: Settings) -> List[str]:
    opts = {
        "BatchMode": "yes",
        "PasswordAuthentication": "no",
        "KbdInteractiveAuthentication": "no",
        "PreferredAuthentications": "publickey",
        "ControlMaster": "no",
        "ControlPath": "none",
        "ExitOnForwardFailure": "yes",
        "ServerAliveInterval": str(settings.server_alive_interval),
        "ServerAliveCountMax": str(settings.server_alive_count_max),
        "ConnectTimeout": str(settings.connect_timeout),
        "TCPKeepAlive": "yes",
        "ForwardX11": "no",
        "ForwardAgent": "no",
        "RequestTTY": "no",
        "LogLevel": "VERBOSE",
    }
    if settings.accept_new_host_keys:
        opts["StrictHostKeyChecking"] = "accept-new"
    out: List[str] = []
    for k, v in opts.items():
        out += ["-o", f"{k}={v}"]
    return out


def build_argv(tunnel: Tunnel, settings: Settings, ssh_bin: str = "ssh") -> List[str]:
    argv = [ssh_bin, "-N", "-T"] + base_options(settings)
    if tunnel.port:
        argv += ["-p", str(tunnel.port)]
    if tunnel.user:
        argv += ["-l", tunnel.user]
    if tunnel.identity_file:
        argv += ["-i", os.path.expanduser(tunnel.identity_file), "-o", "IdentitiesOnly=yes"]
    if tunnel.jump:
        argv += ["-J", tunnel.jump]
    for f in tunnel.forwards:
        argv += [f"-{f.type}", f.spec()]
    argv.append(tunnel.host)
    return argv


def display_command(tunnel: Tunnel) -> str:
    """A short, copy-pasteable command (without the supervisor-only options)."""
    argv = ["ssh", "-N"]
    if tunnel.port:
        argv += ["-p", str(tunnel.port)]
    if tunnel.identity_file:
        argv += ["-i", tunnel.identity_file]
    if tunnel.jump:
        argv += ["-J", tunnel.jump]
    for f in tunnel.forwards:
        argv += [f"-{f.type}", f.spec()]
    argv.append(tunnel.destination())
    return " ".join(shlex.quote(a) for a in argv)


# --- parsing -------------------------------------------------------------------


def _split_spec(spec: str) -> List[str]:
    """Split a forward spec on ':' while keeping [ipv6] groups intact."""
    parts, buf, i = [], "", 0
    while i < len(spec):
        c = spec[i]
        if c == "[":
            j = spec.find("]", i)
            if j < 0:
                raise ValueError(f"unbalanced '[' in {spec!r}")
            buf += spec[i + 1 : j]
            i = j + 1
            continue
        if c == ":":
            parts.append(buf)
            buf = ""
        else:
            buf += c
        i += 1
    parts.append(buf)
    return parts


def _port(s: str) -> int:
    if not s.isdigit():
        raise ValueError(f"not a port number: {s!r}")
    return int(s)


def parse_forward(kind: str, spec: str) -> Forward:
    """Parse the argument of -L / -R / -D into a Forward.

    Raises ValueError for shapes this app does not handle (e.g. a *local*
    unix socket, or a remote dynamic -R port).
    """
    parts = _split_spec(spec.strip())
    if kind == "D":
        if len(parts) == 1:
            return Forward(type="D", listen_port=_port(parts[0]))
        if len(parts) == 2:
            return Forward(type="D", bind=parts[0] or None, listen_port=_port(parts[1]))
        raise ValueError(f"bad -D spec {spec!r}")

    if kind not in ("L", "R"):
        raise ValueError(f"unknown forward type {kind!r}")
    if parts and "/" in parts[0]:
        raise ValueError("local unix-socket listeners are not supported")

    bind: Optional[str] = None
    if len(parts) == 4:
        bind, parts = parts[0] or None, parts[1:]
    elif len(parts) == 3 and "/" in parts[2]:
        bind, parts = parts[0] or None, parts[1:]

    if len(parts) == 2 and "/" in parts[1]:
        if kind == "R":
            raise ValueError("-R to a local unix socket is not supported")
        return Forward(type="L", bind=bind, listen_port=_port(parts[0]), target_socket=parts[1])
    if len(parts) == 3:
        return Forward(type=kind, bind=bind, listen_port=_port(parts[0]), target_host=parts[1], target_port=_port(parts[2]))
    raise ValueError(f"bad -{kind} spec {spec!r}")


@dataclass
class ParsedCommand:
    host: str
    user: Optional[str] = None
    port: Optional[int] = None
    identity_file: Optional[str] = None
    jump: Optional[str] = None
    forwards: List[Forward] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)


_SHELL_OPS = {";", "&&", "||", "|", "&", "(", ")"}
_SSH_NAMES = {"ssh", "autossh"}


def _segments(cmdline: str) -> Iterable[List[str]]:
    lex = shlex.shlex(cmdline, posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    seg: List[str] = []
    for tok in lex:
        if tok in _SHELL_OPS or set(tok) <= set(";&|()"):
            if seg:
                yield seg
            seg = []
        else:
            seg.append(tok)
    if seg:
        yield seg


def parse_ssh_command(cmdline: str) -> Optional[ParsedCommand]:
    """Extract the first ssh invocation with forwards from a shell command line."""
    try:
        segments = list(_segments(cmdline))
    except ValueError:  # unbalanced quotes etc.
        return None
    for seg in segments:
        for i, tok in enumerate(seg):
            name = os.path.basename(tok)
            if name in _SSH_NAMES:
                parsed = _parse_ssh_args(seg[i + 1 :], autossh=name == "autossh")
                if parsed and parsed.forwards:
                    return parsed
                break
            # only look at the command position: skip `FOO=bar`, `sudo`, `nohup` ...
            if not (_ENV_ASSIGN.match(tok) or name in _CMD_PREFIXES):
                break
    return None


_ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_CMD_PREFIXES = {"sudo", "command", "exec", "nohup", "time", "caffeinate", "env", "noglob"}


def _parse_ssh_args(args: List[str], autossh: bool = False) -> Optional[ParsedCommand]:
    user = port = identity = jump = host = None
    forwards: List[Forward] = []
    skipped: List[str] = []
    i = 0

    def take_option(letter: str, value: str) -> None:
        nonlocal user, port, identity, jump
        if letter in "LRD":
            try:
                forwards.append(parse_forward(letter, value))
            except ValueError as e:
                skipped.append(f"-{letter} {value}: {e}")
        elif letter == "l":
            user = value
        elif letter == "p":
            port = int(value) if value.isdigit() else None
        elif letter == "i":
            identity = value
        elif letter == "J":
            jump = value
        elif letter == "o":
            k, _, v = value.replace("=", " ", 1).partition(" ")
            k, v = k.strip().lower(), v.strip()
            if k == "user":
                user = v
            elif k == "port" and v.isdigit():
                port = int(v)
            elif k == "proxyjump":
                jump = v
            elif k == "identityfile":
                identity = v

    while i < len(args):
        a = args[i]
        if autossh and a == "-M":  # autossh monitor port
            i += 2
            continue
        if a == "--":
            if host is None and i + 1 < len(args):
                host = args[i + 1]
            break
        if a.startswith("-") and len(a) > 1:
            j = 1
            while j < len(a):
                c = a[j]
                if c in _OPTS_WITH_ARG:
                    value = a[j + 1 :]
                    if not value:
                        i += 1
                        if i >= len(args):
                            return None
                        value = args[i]
                    take_option(c, value)
                    break
                if c not in _OPTS_NO_ARG:
                    return None  # unknown option: not a command we understand
                j += 1
            i += 1
            continue
        if host is None:
            host = a
            i += 1
            continue
        break  # first non-option after the host is the remote command

    if not host:
        return None
    if host.startswith("ssh://"):
        rest = host[6:]
        if "@" in rest:
            user, rest = rest.rsplit("@", 1)
        if rest.count(":") == 1:
            rest, p = rest.split(":")
            port = int(p) if p.isdigit() else port
        host = rest
    elif "@" in host:
        user, host = host.rsplit("@", 1)
    return ParsedCommand(host=host, user=user, port=port, identity_file=identity, jump=jump, forwards=forwards, skipped=skipped)


# --- error classification --------------------------------------------------------


@dataclass
class SshError:
    kind: str  # auth | hostkey | config | local_port | remote_port | dns | network | target
    message: str
    hint: str
    permanent: bool


_ERROR_RULES: List[Tuple[str, str, str, bool]] = [
    # (regex, kind, hint, permanent)
    (r"Permission denied \(", "auth",
     "Key-based login was refused. Make sure the right key is loaded in your agent "
     "(e.g. `ssh-add --apple-use-keychain ~/.ssh/<key>`) or set an identity file.", True),
    (r"Too many authentication failures", "auth",
     "The agent offered too many keys. Pick an explicit identity file for this tunnel.", True),
    (r"REMOTE HOST IDENTIFICATION HAS CHANGED", "hostkey",
     "The server's host key changed. Verify it, then fix ~/.ssh/known_hosts (ssh-keygen -R <host>).", True),
    (r"Host key verification failed|No .* host key is known", "hostkey",
     "The host key is unknown. Run `ssh <host>` once in a terminal to accept it, "
     "or enable 'accept new host keys' in Settings.", True),
    (r"Bad (local|remote|dynamic) forwarding specification|Bad configuration option|"
     r"command-line line \d+:|Bad port", "config",
     "ssh rejected the command line or your ssh config. Check the tunnel definition.", True),
    (r"[Ii]dentity file .* not accessible", "config",
     "The identity file does not exist or is unreadable.", True),
    (r"bind \[?[^\]]*\]?:? ?(port )?\d*:? ?Address already in use|cannot listen to port|"
     r"Could not request local forwarding", "local_port",
     "A local port is already in use by another process.", False),
    (r"remote port forwarding failed|Error: remote port forwarding failed", "remote_port",
     "The remote port is busy (maybe a stale session still holds it).", False),
    (r"Could not resolve hostname|nodename nor servname|Name or service not known|"
     r"Temporary failure in name resolution", "dns",
     "Hostname could not be resolved (DNS / VPN down?).", False),
    (r"Connection timed out|Operation timed out|No route to host|Network is unreachable|"
     r"Connection refused|Connection reset|Connection closed by|Broken pipe|"
     r"Timeout, server .* not responding|kex_exchange_identification|Connection to .* closed",
     "network", "Network problem talking to the server.", False),
    (r"open failed: (connect failed|administratively prohibited)", "target",
     "The tunnel is up, but the remote target refused a connection "
     "(service not running or socket path wrong?).", False),
]


def classify_error(lines: Iterable[str]) -> Optional[SshError]:
    """Return the most relevant error found in ssh's output (last match wins,
    except that permanent errors beat transient ones)."""
    found: Optional[SshError] = None
    for line in lines:
        for pattern, kind, hint, permanent in _ERROR_RULES:
            if re.search(pattern, line):
                err = SshError(kind, line.strip(), hint, permanent)
                if found is None or not found.permanent or permanent:
                    found = err
                break
    return found


_TILDE_HINT = "sshd does not expand '~' in socket paths - use an absolute path like /home/<user>/..."


def tilde_warning(tunnel: Tunnel) -> Optional[str]:
    return _TILDE_HINT if any(f.socket_has_tilde for f in tunnel.forwards) else None
