import pytest
from fastapi.testclient import TestClient

from tunnel_manager.server import create_app
from tunnel_manager.store import Store
from tunnel_manager.supervisor import Supervisor

from .test_supervisor import FakeNet

H = {"X-Tunnel-Manager": "1"}


@pytest.fixture
def client(isolated_home):
    store = Store()
    sup = Supervisor(store, netwatch=FakeNet())
    app = create_app(store, sup, port=5766, extra_hosts=["testserver"])
    with TestClient(app) as c:
        yield c


def _new(client, **kw):
    body = {"host": "gpu", "forwards": [{"type": "L", "listen_port": 18080, "target_host": "localhost", "target_port": 80}]}
    body.update(kw)
    return client.post("/api/tunnels", json=body, headers=H)


def test_guard_requires_header_and_host(client):
    assert client.post("/api/tunnels", json={}).status_code == 403
    assert client.get("/api/state", headers={"Host": "evil.example:5766"}).status_code == 403
    assert client.post("/api/stop-all", headers=dict(H, Origin="http://evil.example")).status_code == 403
    assert client.get("/api/ping").json()["app"] == "tunnel-manager"


def test_create_list_update_delete(client):
    r = _new(client)
    assert r.status_code == 200 and r.json()["created"]
    tid = r.json()["id"]
    again = _new(client)
    assert again.json() == {"id": tid, "created": False, "name": "gpu :18080→80"}

    state = client.get("/api/state").json()
    t = state["tunnels"][0]
    assert t["keep_alive"] is True  # new tunnels default to keep-alive
    assert t["runtime"]["status"] == "stopped"
    assert t["command"] == "ssh -N -L 18080:localhost:80 gpu"
    assert t["open"] == "http://localhost:18080"

    r = client.put(f"/api/tunnels/{tid}", json={"name": "web", "favorite": True}, headers=H)
    assert r.status_code == 200
    t = client.get("/api/state").json()["tunnels"][0]
    assert t["name"] == "web" and t["favorite"]

    assert client.put(f"/api/tunnels/{tid}", json={"host": "-oProxyCommand=x"}, headers=H).status_code == 422
    assert client.delete(f"/api/tunnels/{tid}", headers=H).json() == {"ok": True}
    assert client.get("/api/state").json()["tunnels"] == []


def test_validation_errors_are_readable(client):
    r = _new(client, forwards=[{"type": "R", "listen_port": 1}])
    assert r.status_code == 422
    assert "-R needs a local target" in r.json()["detail"]


def test_preview(client):
    body = {"host": "gpu", "forwards": [{"type": "L", "listen_port": 8090, "target_socket": "~/app.sock"}]}
    r = client.post("/api/preview", json=body, headers=H).json()
    assert r["ok"] and r["command"] == "ssh -N -L '8090:~/app.sock' gpu"
    assert any("expand" in w for w in r["warnings"])
    bad = client.post("/api/preview", json={"host": "gpu", "forwards": []}, headers=H).json()
    assert not bad["ok"]


def test_favorites_order(client):
    ids = [_new(client, favorite=True, forwards=[{"type": "D", "listen_port": p}]).json()["id"] for p in (1081, 1082, 1083)]
    client.put("/api/favorites/order", json={"ids": ids[::-1]}, headers=H)
    tunnels = client.get("/api/state").json()["tunnels"]
    order = {t["id"]: t["order"] for t in tunnels}
    assert [order[i] for i in ids] == [2, 1, 0]


def test_logs_endpoint(client, isolated_home):
    tid = _new(client).json()["id"]
    from tunnel_manager import paths

    paths.tunnel_log(tid).write_text("line one\nline two\n")
    r = client.get(f"/api/tunnels/{tid}/logs").json()
    assert r["text"].endswith("line two\n") and r["offset"] == 18
    assert client.get(f"/api/tunnels/{tid}/logs?offset=18").json()["text"] == ""
