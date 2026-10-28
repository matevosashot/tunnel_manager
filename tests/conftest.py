import os
import socket
import stat
import sys
import time
from pathlib import Path

import psutil
import pytest

HERE = Path(__file__).parent


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Never touch the real ~/.cache or ~/.ssh from tests."""
    home = tmp_path / "tm-home"
    monkeypatch.setenv("TUNNEL_MANAGER_HOME", str(home))
    ssh_dir = tmp_path / "ssh"
    ssh_dir.mkdir()
    (ssh_dir / "config").write_text("Host gpu\n  HostName 127.0.0.1\n")
    monkeypatch.setenv("TUNNEL_MANAGER_SSH_DIR", str(ssh_dir))
    return home


@pytest.fixture
def fake_ssh(tmp_path, monkeypatch):
    """Point the supervisor at tests/fake_ssh.py; returns a function to set its mode."""
    wrapper = tmp_path / "fake-ssh"
    wrapper.write_text(f"#!/bin/sh\nexec {sys.executable} {HERE / 'fake_ssh.py'} \"$@\"\n")
    wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
    mode_file = tmp_path / "fake_ssh_mode"
    mode_file.write_text("ok")
    monkeypatch.setenv("TUNNEL_MANAGER_SSH", str(wrapper))
    monkeypatch.setenv("FAKE_SSH_MODE_FILE", str(mode_file))
    started = time.time()
    yield mode_file.write_text
    # a failing test must not leave fake tunnels running
    for proc in psutil.process_iter(["cmdline", "create_time"]):
        try:
            if proc.info["create_time"] >= started - 1 and any("fake_ssh.py" in a for a in proc.info["cmdline"] or []):
                proc.kill()
        except psutil.Error:
            pass


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_for(predicate, timeout=8.0, step=0.05, tick=None):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if tick:
            tick()
        if predicate():
            return True
        time.sleep(step)
    return False
