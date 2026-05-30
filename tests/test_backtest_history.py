import pytest

from signals.core.backtest_history import BacktestHistoryStore, SCHEMA_VERSION


def test_backtest_history_store_create_list_get_delete(tmp_path):
    store = BacktestHistoryStore(tmp_path)
    payload = {
        "id": "multi:daily:002409,300394:2026-05-30T07:44:00.000Z",
        "mode": "multi",
        "title": "多标的回测 · 2只",
        "meta": "2标的 · 8信号 · 3成交",
        "createdAt": "2026-05-30T07:44:00.000Z",
        "codes": ["002409", "300394"],
        "freq": "daily",
        "signalType": "all",
        "batchResult": {"terminal": {"version": "backtest-terminal.v1", "mode": "multi"}},
    }

    saved = store.save(payload)

    assert saved["schema_version"] == SCHEMA_VERSION
    assert saved["deletedAt"] is None
    assert store.list(limit=50)[0]["id"] == payload["id"]
    assert store.get(payload["id"])["batchResult"]["terminal"]["mode"] == "multi"

    deleted = store.delete(payload["id"])

    assert deleted["deletedAt"]
    assert store.list(limit=50) == []
    assert store.get(payload["id"]) is None
    assert store.get(payload["id"], include_deleted=True)["id"] == payload["id"]


def test_backtest_history_store_writes_tombstone_for_missing_delete(tmp_path):
    store = BacktestHistoryStore(tmp_path)

    deleted = store.delete("single:daily:300394:2026-05-30T07:44:00.000Z")

    assert deleted["deletedAt"]
    assert store.list(include_deleted=True)[0]["mode"] == "deleted"
    assert store.list() == []


@pytest.mark.parametrize("bad_id", ["../bad", "bad/path", "."])
def test_backtest_history_store_rejects_unsafe_ids(tmp_path, bad_id):
    store = BacktestHistoryStore(tmp_path)

    with pytest.raises(ValueError):
        store.save({"id": bad_id, "mode": "single"})

    with pytest.raises(ValueError):
        store.get(bad_id)
