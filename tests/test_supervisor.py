import os
import signal
import socket

import psutil
import pytest

from tunnel_manager.models import Forward, Tunnel
from tunnel_manager.store import Store
from tunnel_manager.supervisor import Supervisor

from .conftest import free_port, wait_for


class FakeNet:
    online = True
    addresses = frozenset({"10.1.2.3"})

    def start(self):
        pass

    def stop(self):
        pass


@pytest.fixture
def env(fake_ssh, isolated_home):
    store = Store()
    sup = Supervisor(store, netwatch=FakeNet())
    yield store, sup, fake_ssh, sup.events
    sup.stop_all(wait=True)


def _add(store, keep_alive=True, port=None):
    port = port or free_port()
    t, _ = store.add(Tunnel(host="gpu", keep_alive=keep_alive,
                            forwards=[Forward(type="L", listen_port=port, target_host="localhost", target_port=80)]))
    return t, port


def _status(sup, tid):
    return sup.snapshot()[tid]["status"]


def test_start_up_and_stop(env):
    store, sup, _, _ = env
    t, port = _add(store)
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)
    rt = sup.snapshot()[t.id]
    assert rt["connect_ms"] is not None and psutil.pid_exists(rt["pid"])
    assert store.get(t.id).desired == "running"
    # the port is really listening
    socket.create_connection(("127.0.0.1", port), timeout=1).close()

    pid = rt["pid"]
    sup.stop(t.id, wait=True)
    assert _status(sup, t.id) == "stopped"
    assert wait_for(lambda: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE)
    assert store.get(t.id).desired == "stopped"
    assert sup.snapshot()[t.id]["last_transfer"]["received"] == 5678


def test_keep_alive_restarts_after_kill(env):
    store, sup, _, _ = env
    t, _ = _add(store)
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)
    first = sup.snapshot()[t.id]["pid"]
    os.kill(first, signal.SIGKILL)
    assert wait_for(lambda: _status(sup, t.id) == "retrying", tick=sup.tick)
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)
    rt = sup.snapshot()[t.id]
    assert rt["pid"] != first and rt["reconnects"] == 1


def test_without_keep_alive_a_drop_is_final(env):
    store, sup, mode, _ = env
    t, _ = _add(store, keep_alive=False)
    mode("drop:0.5")
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "failed", tick=sup.tick)
    rt = sup.snapshot()[t.id]
    assert rt["last_error"]["kind"] == "network"
    assert store.get(t.id).desired == "stopped"


def test_auth_errors_give_up(env):
    store, sup, mode, sent = env
    t, _ = _add(store)
    mode("auth")
    sup.start(t.id)
    for _ in range(3):
        assert wait_for(lambda: _status(sup, t.id) in ("retrying", "failed"), tick=sup.tick)
        if _status(sup, t.id) == "failed":
            break
        sup.rt[t.id].next_retry_at = 0  # skip the backoff wait
        sup.tick()
    assert wait_for(lambda: _status(sup, t.id) == "failed", tick=sup.tick)
    rt = sup.snapshot()[t.id]
    assert rt["last_error"]["kind"] == "auth" and rt["last_error"]["permanent"]
    assert [e["kind"] for e in sent] == ["failed"]


def test_backoff_grows(env):
    store, sup, mode, _ = env
    t, _ = _add(store)
    mode("drop:0")
    sup.start(t.id)
    delays = []
    for _ in range(3):
        assert wait_for(lambda: _status(sup, t.id) == "retrying", tick=sup.tick)
        r = sup.rt[t.id]
        delays.append(round(r.next_retry_at - __import__("time").time()))
        r.next_retry_at = 0
        sup.tick()
    assert delays == [1, 2, 4]


def test_port_conflict_is_reported(env):
    store, sup, _, _ = env
    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    port = blocker.getsockname()[1]
    t, _ = _add(store, keep_alive=False, port=port)
    try:
        sup.start(t.id)
        rt = sup.snapshot()[t.id]
        assert rt["status"] == "failed"
        assert rt["last_error"]["kind"] == "local_port"
        assert str(port) in rt["last_error"]["message"]
    finally:
        blocker.close()


def test_adoption_after_manager_restart(env):
    store, sup, _, _ = env
    t, _ = _add(store)
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)
    pid = sup.snapshot()[t.id]["pid"]
    sup.shutdown()  # tunnels keep running

    sup2 = Supervisor(Store(), netwatch=FakeNet())
    sup2.restore()
    rt = sup2.snapshot()[t.id]
    assert rt["pid"] == pid and rt["adopted"] and rt["status"] == "up"
    # an adopted process is still supervised
    os.kill(pid, signal.SIGKILL)
    assert wait_for(lambda: _status(sup2, t.id) == "retrying", tick=sup2.tick)
    sup2.rt[t.id].next_retry_at = 0
    assert wait_for(lambda: _status(sup2, t.id) == "up", tick=sup2.tick)
    sup2.stop_all(wait=True)


def test_restore_kills_orphans_and_restarts_keep_alive(env):
    store, sup, _, _ = env
    a, _ = _add(store)
    b, _ = _add(store)
    sup.start(a.id)
    sup.start(b.id)
    assert wait_for(lambda: _status(sup, a.id) == "up" and _status(sup, b.id) == "up", tick=sup.tick)
    pid_a = sup.snapshot()[a.id]["pid"]
    pid_b = sup.snapshot()[b.id]["pid"]
    sup.shutdown()
    # while the manager is down: tunnel a is deleted, b's ssh dies
    store.delete(a.id)
    os.kill(pid_b, signal.SIGKILL)
    wait_for(lambda: not psutil.pid_exists(pid_b) or psutil.Process(pid_b).status() == psutil.STATUS_ZOMBIE)

    sup2 = Supervisor(Store(), netwatch=FakeNet())
    sup2.restore()
    assert wait_for(lambda: not psutil.pid_exists(pid_a) or psutil.Process(pid_a).status() == psutil.STATUS_ZOMBIE)
    assert a.id not in sup2.snapshot()
    assert wait_for(lambda: _status(sup2, b.id) == "up", tick=sup2.tick)  # keep-alive restored it
    sup2.stop_all(wait=True)


def test_network_events(env):
    store, sup, mode, _ = env
    t, _ = _add(store)
    mode("drop:0")
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "retrying", tick=sup.tick)
    sup.rt[t.id].next_retry_at = 1e12  # far in the future
    sup.network_event("change", False)
    assert _status(sup, t.id) == "offline"
    mode("ok")
    sup.network_event("change", True)
    assert _status(sup, t.id) == "retrying" and sup.rt[t.id].next_retry_at <= __import__("time").time()
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)


def test_stale_connection_is_restarted_immediately(env, monkeypatch):
    store, sup, _, _ = env
    t, _ = _add(store)
    sup.start(t.id)
    assert wait_for(lambda: _status(sup, t.id) == "up", tick=sup.tick)
    first = sup.snapshot()[t.id]["pid"]
    monkeypatch.setattr(sup, "_stale", lambda r, addrs: True)
    sup.network_event("change", True)
    assert wait_for(lambda: sup.snapshot()[t.id]["pid"] not in (None, first) and _status(sup, t.id) == "up",
                    tick=sup.tick)
