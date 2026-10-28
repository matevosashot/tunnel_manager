from tunnel_manager import sshconfig


def test_list_hosts_follows_include_and_skips_patterns(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL_MANAGER_SSH_DIR", str(tmp_path))
    (tmp_path / "conf.d").mkdir()
    (tmp_path / "config").write_text(
        "Include conf.d/*.conf\n"
        "Host *\n  ServerAliveInterval 30\n"
        "Host gpu gpu-* !bad\n  HostName gpu.example.org\n"
        "# Host commented\n"
        "Host=bastion\n  User admin\n"
        "Host gpu\n"
    )
    (tmp_path / "conf.d" / "work.conf").write_text("Host db1 db2\n  User me\n")
    assert sshconfig.list_hosts() == ["db1", "db2", "gpu", "bastion"]


def test_include_cycle_is_safe(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL_MANAGER_SSH_DIR", str(tmp_path))
    (tmp_path / "config").write_text("Include config\nHost a\n")
    assert sshconfig.list_hosts() == ["a"]


def test_list_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("TUNNEL_MANAGER_SSH_DIR", str(tmp_path))
    (tmp_path / "id_ed25519").write_text("x")
    (tmp_path / "id_ed25519.pub").write_text("ssh-ed25519 AAAA me@host\n")
    (tmp_path / "orphan.pub").write_text("ssh-rsa AAAA\n")
    (tmp_path / "config").write_text("")
    keys = sshconfig.list_keys()
    assert [k["type"] for k in keys] == ["ssh-ed25519"]
    assert keys[0]["path"].endswith("id_ed25519")
