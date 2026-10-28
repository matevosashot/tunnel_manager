"""Start at login: a launchd LaunchAgent (macOS) or a systemd --user unit (Linux)."""

from __future__ import annotations

import os
import plistlib
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List

from . import paths

LABEL = "io.github.tunnel-manager"
SYSTEMD_UNIT = "tunnel-manager.service"
ENV_FLAG = "TUNNEL_MANAGER_SERVICE"


def running_as_service() -> bool:
    return os.environ.get(ENV_FLAG) == "1"


def _program(port: int) -> List[str]:
    # sys.executable is the interpreter of the environment tunnel-manager is
    # installed in (pipx/uv/venv), so the service keeps working after upgrades
    return [sys.executable, "-m", "tunnel_manager", "serve", "--no-browser", "--wait-for-lock", "--port", str(port)]


def _env() -> dict:
    env = {
        ENV_FLAG: "1",
        "PATH": os.pathsep.join(
            dict.fromkeys(["/usr/bin", "/bin", "/usr/sbin", "/sbin", "/opt/homebrew/bin", "/usr/local/bin"]
                          + os.environ.get("PATH", "").split(os.pathsep))
        ),
    }
    for var in ("TUNNEL_MANAGER_HOME", "XDG_CACHE_HOME"):
        if os.environ.get(var):
            env[var] = os.environ[var]
    return env


# ---------------------------------------------------------------- macOS


def _plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _launchctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["launchctl", *args], capture_output=True, text=True)


def _install_launchd(port: int) -> dict:
    p = _plist_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    log = str(paths.service_log())
    plist = {
        "Label": LABEL,
        "ProgramArguments": _program(port),
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ProcessType": "Interactive",
        "StandardOutPath": log,
        "StandardErrorPath": log,
        "EnvironmentVariables": _env(),
    }
    with open(p, "wb") as f:
        plistlib.dump(plist, f)
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    r = _launchctl("bootstrap", _domain(), str(p))
    if r.returncode != 0:
        raise RuntimeError(f"launchctl bootstrap failed: {r.stderr.strip() or r.stdout.strip()}")
    return {"installed": True, "path": str(p)}


def _uninstall_launchd() -> dict:
    p = _plist_path()
    _launchctl("bootout", f"{_domain()}/{LABEL}")
    existed = p.exists()
    p.unlink(missing_ok=True)
    return {"installed": False, "removed": existed}


def _status_launchd() -> dict:
    p = _plist_path()
    r = _launchctl("print", f"{_domain()}/{LABEL}")
    pid = None
    if r.returncode == 0:
        m = re.search(r"\bpid = (\d+)", r.stdout)
        pid = int(m.group(1)) if m else None
    return {"supported": True, "kind": "launchd", "installed": p.exists(), "loaded": r.returncode == 0,
            "pid": pid, "path": str(p)}


# ---------------------------------------------------------------- Linux


def _unit_path() -> Path:
    base = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
    return base / "systemd" / "user" / SYSTEMD_UNIT


def _systemctl(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True)


def _install_systemd(port: int) -> dict:
    p = _unit_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    env_lines = "\n".join(f"Environment={k}={v}" for k, v in _env().items())
    cmd = " ".join(_program(port))
    p.write_text(
        "[Unit]\n"
        "Description=Tunnel Manager (SSH tunnels web UI)\n"
        "After=network-online.target\n\n"
        "[Service]\n"
        f"ExecStart={cmd}\n"
        f"{env_lines}\n"
        "Restart=on-failure\n"
        "# keep ssh tunnels alive across manager restarts; they are re-adopted\n"
        "KillMode=process\n\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    _systemctl("daemon-reload")
    r = _systemctl("enable", "--now", SYSTEMD_UNIT)
    if r.returncode != 0:
        raise RuntimeError(f"systemctl enable failed: {r.stderr.strip()}")
    return {"installed": True, "path": str(p)}


def _uninstall_systemd() -> dict:
    _systemctl("disable", "--now", SYSTEMD_UNIT)
    p = _unit_path()
    existed = p.exists()
    p.unlink(missing_ok=True)
    _systemctl("daemon-reload")
    return {"installed": False, "removed": existed}


def _status_systemd() -> dict:
    p = _unit_path()
    r = _systemctl("show", SYSTEMD_UNIT, "--property=ActiveState,MainPID")
    props = dict(line.split("=", 1) for line in r.stdout.splitlines() if "=" in line)
    pid = int(props.get("MainPID", "0") or 0) or None
    return {"supported": True, "kind": "systemd", "installed": p.exists(),
            "loaded": props.get("ActiveState") == "active", "pid": pid, "path": str(p)}


# ---------------------------------------------------------------- dispatch


def install(port: int = 5766) -> dict:
    if sys.platform == "darwin":
        return _install_launchd(port)
    if shutil.which("systemctl"):
        return _install_systemd(port)
    raise RuntimeError("start-at-login is supported on macOS (launchd) and Linux (systemd) only")


def uninstall() -> dict:
    if sys.platform == "darwin":
        return _uninstall_launchd()
    if shutil.which("systemctl"):
        return _uninstall_systemd()
    raise RuntimeError("start-at-login is supported on macOS (launchd) and Linux (systemd) only")


def status() -> dict:
    if sys.platform == "darwin":
        st = _status_launchd()
    elif shutil.which("systemctl"):
        st = _status_systemd()
    else:
        st = {"supported": False, "installed": False, "loaded": False}
    st["this_process_is_service"] = running_as_service()
    return st
