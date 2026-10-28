import json

import pytest

from tunnel_manager.models import Forward, Tunnel
from tunnel_manager.store import Store


def _t(port=8080, **kw):
    kw.setdefault("host", "gpu")
    return Tunnel(forwards=[Forward(type="L", listen_port=port, target_host="localhost", target_port=port)], **kw)


@pytest.fixture
def store(tmp_path):
    return Store(tmp_path / "state.json")


def test_add_dedupes(store):
    a, created = store.add(_t())
    assert created
    b, created = store.add(_t(name="other name"))
    assert not created and b.id == a.id and b.use_count == 1
    assert len(store.tunnels()) == 1


def test_persisted_and_reloaded(store, tmp_path):
    store.add(_t(favorite=True))
    again = Store(tmp_path / "state.json")
    assert again.tunnels()[0].favorite


def test_update_validates_and_rejects_duplicates(store):
    a, _ = store.add(_t(8080))
    b, _ = store.add(_t(9090))
    with pytest.raises(ValueError):
        store.update(b.id, {"forwards": [f.model_dump() for f in a.forwards]})
    with pytest.raises(Exception):
        store.update(b.id, {"host": "-bad"})
    updated = store.update(b.id, {"name": "renamed"})
    assert updated.name == "renamed"
    assert store.update(b.id, {"name": ""}).name == "gpu :9090"


def test_delete_is_remembered_by_history_import(store):
    a, _ = store.add(_t(8080))
    store.delete(a.id)
    result = store.merge_imported([_t(8080), _t(9090)])
    assert result == {"added": 1, "updated": 0, "ignored": 1}
    # creating it manually again un-ignores it
    _, created = store.add(_t(8080))
    assert created
    assert store.merge_imported([_t(8080)])["ignored"] == 0


def test_merge_refreshes_usage(store):
    a, _ = store.add(_t(8080))
    imported = _t(8080, use_count=7)
    imported.last_used_at = a.last_used_at + 100
    assert store.merge_imported([imported])["updated"] == 1
    assert store.get(a.id).use_count == 7


def test_favorites_order(store):
    ids = [store.add(_t(p, favorite=True))[0].id for p in (1, 2, 3)]
    assert [store.get(i).order for i in ids] == [0, 1, 2]
    store.reorder_favorites(list(reversed(ids)))
    assert [store.get(i).order for i in ids] == [2, 1, 0]
    x, _ = store.add(_t(4))
    assert store.update(x.id, {"favorite": True}).order == 3


def test_corrupt_state_is_backed_up(tmp_path):
    p = tmp_path / "state.json"
    p.write_text("{not json")
    s = Store(p)
    assert s.tunnels() == []
    assert list(tmp_path.glob("state.corrupt-*.json"))


def test_find(store):
    a, _ = store.add(_t(8080, name="web app"))
    store.add(_t(9090, name="webhook"))
    assert store.find(a.id).id == a.id
    assert store.find("web app").id == a.id
    assert store.find("web") is None  # ambiguous prefix
    assert store.find("webh").name == "webhook"
