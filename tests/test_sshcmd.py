import pytest
from pydantic import ValidationError

from tunnel_manager.models import Forward, Settings, Tunnel
from tunnel_manager.sshcmd import (
    build_argv,
    classify_error,
    display_command,
    parse_forward,
    parse_ssh_command,
)


# --- forward specs ---------------------------------------------------------------

@pytest.mark.parametrize(
    "kind,spec,expected",
    [
        ("L", "8080:localhost:80", dict(listen_port=8080, target_host="localhost", target_port=80)),
        ("L", "127.0.0.1:8080:db:5432", dict(bind="127.0.0.1", listen_port=8080, target_host="db", target_port=5432)),
        ("L", "[::1]:8080:[fe80::1]:80", dict(bind="::1", listen_port=8080, target_host="fe80::1", target_port=80)),
        ("L", "8090:/srv/app/app.sock", dict(listen_port=8090, target_socket="/srv/app/app.sock")),
        ("L", "0.0.0.0:8090:/srv/app.sock", dict(bind="0.0.0.0", listen_port=8090, target_socket="/srv/app.sock")),
        ("R", "3129:127.0.0.1:3128", dict(listen_port=3129, target_host="127.0.0.1", target_port=3128)),
        ("R", "127.0.0.1:9000:localhost:9000", dict(bind="127.0.0.1", listen_port=9000, target_host="localhost", target_port=9000)),
        ("D", "1080", dict(listen_port=1080)),
        ("D", "127.0.0.1:1080", dict(bind="127.0.0.1", listen_port=1080)),
    ],
)
def test_parse_forward(kind, spec, expected):
    f = parse_forward(kind, spec)
    assert f.type == kind
    for k, v in expected.items():
        assert getattr(f, k) == v
    # rendering gives back an equivalent spec
    assert parse_forward(kind, f.spec()) == f


@pytest.mark.parametrize(
    "kind,spec",
    [
        ("L", "/tmp/local.sock:host:80"),  # local unix socket listener
        ("R", "8080:/tmp/x.sock"),         # -R to a local socket
        ("L", "8080"),
        ("L", "abc:host:80"),
        ("D", "1:2:3"),
    ],
)
def test_parse_forward_rejects(kind, spec):
    with pytest.raises(ValueError):
        parse_forward(kind, spec)


# --- whole command lines ----------------------------------------------------------

def test_parse_simple():
    p = parse_ssh_command("ssh -L 8790:localhost:8790 gpu")
    assert p.host == "gpu" and p.user is None
    assert [f.spec() for f in p.forwards] == ["8790:localhost:8790"]


def test_parse_options_after_host_and_remote_command():
    p = parse_ssh_command("ssh -R 3129:127.0.0.1:3128 node1 -t tmux attach")
    assert p.host == "node1"
    assert p.forwards[0].type == "R"
    p = parse_ssh_command("ssh -L 8090:/home/alice/.cache/app.sock -N gpu")
    assert p.host == "gpu" and p.forwards[0].target_socket == "/home/alice/.cache/app.sock"


def test_parse_user_port_jump_identity():
    p = parse_ssh_command("ssh -l bob -p 2222 -J jumpy -i ~/.ssh/k -NfL 9000:localhost:9000 10.0.0.5")
    assert (p.host, p.user, p.port, p.jump, p.identity_file) == ("10.0.0.5", "bob", 2222, "jumpy", "~/.ssh/k")
    p = parse_ssh_command("ssh -L 5901:localhost:5901 bob@10.0.0.5")
    assert (p.host, p.user) == ("10.0.0.5", "bob")
    p = parse_ssh_command("ssh -o ProxyJump=bastion -o User=carol -o Port=2200 -L 80:web:80 host")
    assert (p.user, p.port, p.jump) == ("carol", 2200, "bastion")
    p = parse_ssh_command("ssh ssh://dave@host:2200 -L 1:a:1")
    assert (p.host, p.user, p.port) == ("host", "dave", 2200)


def test_parse_multiple_forwards_and_shell_context():
    p = parse_ssh_command("cd /tmp && ssh -N -L 1:localhost:1 -L 2:localhost:2 -D 1080 box; echo done")
    assert [f.type for f in p.forwards] == ["L", "L", "D"]
    p = parse_ssh_command("FOO=1 nohup ssh -L 1:a:1 box")
    assert p.host == "box"


@pytest.mark.parametrize(
    "cmd",
    [
        "ssh gpu",                              # no forwards
        "echo ssh -L 8080:a:80 h",              # ssh not in command position
        "ssh -Z -L 1:a:1 h",                    # unknown option
        "git push",
        "ssh -L 'unbalanced",
    ],
)
def test_parse_ignores(cmd):
    assert parse_ssh_command(cmd) is None


# --- argv ---------------------------------------------------------------------------

def _tunnel(**kw):
    kw.setdefault("host", "gpu")
    kw.setdefault("forwards", [Forward(type="L", listen_port=8080, target_host="localhost", target_port=80)])
    return Tunnel(**kw)


def test_build_argv_enforces_key_only_and_own_connection():
    argv = build_argv(_tunnel(), Settings())
    joined = " ".join(argv)
    for opt in (
        "BatchMode=yes",
        "PasswordAuthentication=no",
        "KbdInteractiveAuthentication=no",
        "PreferredAuthentications=publickey",
        "ControlMaster=no",
        "ControlPath=none",
        "ExitOnForwardFailure=yes",
    ):
        assert opt in joined
    assert argv[:3] == ["ssh", "-N", "-T"]
    assert argv[-3:] == ["-L", "8080:localhost:80", "gpu"]
    assert "StrictHostKeyChecking" not in joined
    assert "StrictHostKeyChecking=accept-new" in " ".join(build_argv(_tunnel(), Settings(accept_new_host_keys=True)))


def test_build_argv_optional_parts():
    t = _tunnel(user="bob", port=2222, jump="bastion", identity_file="/keys/id",
                forwards=[Forward(type="D", listen_port=1080), Forward(type="R", listen_port=9, target_host="localhost", target_port=9)])
    argv = build_argv(t, Settings())
    i = argv.index("-p")
    assert argv[i + 1] == "2222"
    assert argv[argv.index("-l") + 1] == "bob"
    assert argv[argv.index("-J") + 1] == "bastion"
    assert argv[argv.index("-i") + 1] == "/keys/id"
    assert "-D" in argv and "-R" in argv
    assert display_command(t) == "ssh -N -p 2222 -i /keys/id -J bastion -D 1080 -R 9:localhost:9 bob@gpu"


# --- validation / injection ---------------------------------------------------------

@pytest.mark.parametrize(
    "kw",
    [
        dict(host="-oProxyCommand=evil"),
        dict(host="gpu box"),
        dict(user="-x"),
        dict(user="a b"),
        dict(jump="-oProxyCommand=x"),
        dict(jump="bob@-evil"),
        dict(open_url="javascript:alert(1)"),
        dict(open_url="file:///etc/passwd"),
        dict(identity_file="-oFoo"),
    ],
)
def test_validation_rejects(kw):
    with pytest.raises(ValidationError):
        _tunnel(**kw)


@pytest.mark.parametrize(
    "fw",
    [
        dict(type="L", listen_port=1, target_host="-oX", target_port=1),
        dict(type="L", listen_port=1, target_socket="/tmp/a b.sock"),
        dict(type="L", listen_port=1, target_socket="relative.sock"),
        dict(type="L", listen_port=70000, target_host="a", target_port=1),
        dict(type="R", listen_port=1),
        dict(type="L", listen_port=1),
    ],
)
def test_forward_validation_rejects(fw):
    with pytest.raises(ValidationError):
        Forward(**fw)


def test_jump_formats_accepted():
    assert _tunnel(jump="alice@bastion:2222,hop2").jump == "alice@bastion:2222,hop2"
    assert _tunnel(jump="[2001:db8::1]:22").jump


def test_dedupe_treats_loopback_binds_as_equal():
    a = _tunnel(forwards=[Forward(type="L", bind="localhost", listen_port=1, target_host="localhost", target_port=1)])
    b = _tunnel(forwards=[Forward(type="L", listen_port=1, target_host="localhost", target_port=1)])
    c = _tunnel(forwards=[Forward(type="L", bind="0.0.0.0", listen_port=1, target_host="localhost", target_port=1)])
    assert a.dedupe_key() == b.dedupe_key() != c.dedupe_key()


def test_auto_name_and_open_url():
    t = _tunnel(forwards=[Forward(type="L", listen_port=8090, target_socket="/srv/app.sock")])
    assert t.name == "gpu :8090→app.sock"
    assert t.default_open_url() == "http://localhost:8090"
    vnc = _tunnel(forwards=[Forward(type="L", listen_port=5901, target_host="localhost", target_port=5901)])
    assert vnc.name == "gpu :5901"
    assert vnc.default_open_url() == "vnc://localhost:5901"
    r = _tunnel(forwards=[Forward(type="R", listen_port=3129, target_host="127.0.0.1", target_port=3128)])
    assert r.default_open_url() is None


# --- errors ----------------------------------------------------------------------------

def test_classify_error():
    e = classify_error(["Authenticated to x", "user@host: Permission denied (publickey)."])
    assert e.kind == "auth" and e.permanent
    e = classify_error(["bind [127.0.0.1]:8080: Address already in use", "channel_setup_fwd_listener_tcpip: cannot listen to port: 8080"])
    assert e.kind == "local_port" and not e.permanent
    e = classify_error(["ssh: Could not resolve hostname nope: nodename nor servname provided, or not known"])
    assert e.kind == "dns"
    e = classify_error(["Host key verification failed.", "Connection closed by 1.2.3.4 port 22"])
    assert e.kind == "hostkey"  # permanent beats the later transient line
    e = classify_error(["channel 3: open failed: connect failed: No such file or directory"])
    assert e.kind == "target"
    assert classify_error(["Authenticated to gpu ([10.0.0.1]:22) using \"publickey\"."]) is None
