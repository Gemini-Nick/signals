import importlib.util
from pathlib import Path


MODULE = Path(__file__).parents[1] / "scripts" / "versioned_index_constituents.py"
SPEC = importlib.util.spec_from_file_location("versioned_index_constituents", MODULE)
MOD = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
SPEC.loader.exec_module(MOD)
append_index_snapshot = MOD.append_index_snapshot


class FakeCollection:
    def __init__(self):
        self.rows = []
        self.indexes = []

    def create_index(self, *args, **kwargs):
        self.indexes.append((args, kwargs))

    def find_one(self, query):
        return next((row for row in self.rows if all(row.get(k) == v for k, v in query.items())), None)

    def insert_one(self, document):
        self.rows.append(dict(document))


def test_index_snapshots_are_append_only_and_hash_addressed():
    collection = FakeCollection()
    first = append_index_snapshot(
        collection,
        index_name="沪深300",
        effective_date="2026-07-31",
        stocks=[{"code": "600000", "code_name": "浦发银行"}],
        source="baostock",
    )
    repeated = append_index_snapshot(
        collection,
        index_name="沪深300",
        effective_date="2026-07-31",
        stocks=[{"code": "600000", "code_name": "浦发银行"}],
        source="baostock",
    )
    changed = append_index_snapshot(
        collection,
        index_name="沪深300",
        effective_date="2026-07-31",
        stocks=[{"code": "600001", "code_name": "邯郸钢铁"}],
        source="baostock",
    )
    assert first["inserted"] is True
    assert repeated["inserted"] is False
    assert changed["inserted"] is True
    assert len(collection.rows) == 2
    assert all(row["immutable_snapshot"] is True for row in collection.rows)
    assert all(row["payload_hash"] for row in collection.rows)
