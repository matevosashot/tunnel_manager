from tunnel_manager.history import (
    HistoryEntry,
    read_bash_history,
    read_zsh_history,
    tunnels_from_history,
)


def test_read_zsh_extended_multiline_and_meta(tmp_path):
    p = tmp_path / ".zsh_history"
    # "é" is 0xC3 0xA9; zsh metafies 0xA9? no - only 0x83..0x9F and a few others.
    # Use a metafied byte explicitly: Meta(0x83) + (0x83 ^ 32) decodes to 0x83.
    data = (
        b": 1700000000:0;ssh -L 8080:localhost:80 gpu\n"
        b": 1700000100:0;echo one \\\n"
        b"two\n"
        b"plain line without timestamp\n"
        b": 1700000200:0;echo \x83\xa3\n"
    )
    p.write_bytes(data)
    entries = list(read_zsh_history(p))
    assert [e.command for e in entries[:3]] == [
        "ssh -L 8080:localhost:80 gpu",
        "echo one \ntwo",
        "plain line without timestamp",
    ]
    assert entries[0].timestamp == 1700000000
    assert entries[2].timestamp is None
    assert len(entries) == 4


def test_read_bash_history(tmp_path):
    p = tmp_path / ".bash_history"
    p.write_text("#1700000000\nssh -L 1:a:1 h\nls\n")
    entries = list(read_bash_history(p))
    assert entries[0].timestamp == 1700000000 and entries[0].command == "ssh -L 1:a:1 h"
    assert entries[1].timestamp is None


def _e(cmd, ts):
    return HistoryEntry(cmd, ts, "zsh_history")


def test_tunnels_from_history_dedupes_and_counts():
    entries = [
        _e("ssh -L 8790:localhost:8790 gpu", 100),
        _e("ssh -N -L 8790:localhost:8790 gpu", 300),
        _e("ssh -L localhost:8790:localhost:8790 gpu", 200),  # loopback bind == no bind
        _e("ssh -R 3129:127.0.0.1:3128 node1 -t tmux attach", 50),
        _e("ssh -R 3129:127.0.0.1:3128 node1", 60),
        _e("ssh gpu", 400),
        _e("ls -la", 500),
    ]
    ts = tunnels_from_history(entries)
    assert len(ts) == 2
    gpu = ts[0]
    assert gpu.host == "gpu" and gpu.use_count == 3
    assert gpu.last_used_at == 300 and gpu.created_at == 100
    node = ts[1]
    assert node.use_count == 2 and node.forwards[0].type == "R"


def test_tilde_socket_paths_are_made_absolute_and_merged():
    entries = [
        _e("ssh -N -L 8090:~/.cache/app/app.sock gpu", 10),
        _e("ssh -L 8090:/home/alice/.cache/app/app.sock -N gpu", 20),
    ]
    ts = tunnels_from_history(entries, remote_home=lambda host, user: "/home/alice")
    assert len(ts) == 1
    assert ts[0].forwards[0].target_socket == "/home/alice/.cache/app/app.sock"
    assert ts[0].use_count == 2


def test_tilde_kept_when_home_unknown():
    ts = tunnels_from_history([_e("ssh -L 8090:~/x.sock gpu", 1)], remote_home=lambda h, u: None)
    assert ts[0].forwards[0].target_socket == "~/x.sock"
