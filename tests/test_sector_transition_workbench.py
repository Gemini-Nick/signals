from datetime import date, datetime


class _Cursor:
    def __init__(self, rows):
        self.rows = list(rows)

    def sort(self, spec):
        for key, direction in reversed(spec):
            self.rows.sort(key=lambda row: row.get(key) or datetime.min, reverse=direction < 0)
        return self

    def limit(self, value):
        self.rows = self.rows[:value]
        return self

    def __iter__(self):
        return iter(self.rows)


class _Collection:
    def __init__(self, rows):
        self.rows = rows

    def find(self, query, projection):
        market = query.get("market")
        rows = [row for row in self.rows if not market or row.get("market") == market]
        return _Cursor(rows)

    def find_one(self, query, projection, sort=None):
        market = query.get("market")
        return next(
            (row for row in self.rows if not market or row.get("market") == market),
            None,
        )


class _Db(dict):
    def __getitem__(self, key):
        if key not in self:
            raise KeyError(key)
        return super().__getitem__(key)


def test_sector_transition_radar_is_optional(monkeypatch):
    from signals.web.api import workbench

    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "false")
    monkeypatch.setattr(workbench, "_mongo_db", lambda: _Db())

    radar = workbench._sector_transition_radar()

    assert radar["events"] == []
    assert radar["states"] == []
    assert radar["freshness"]["status"] == "disabled"
    assert radar["counts"]["stable_turn"] == 0


def test_sector_transition_radar_serializes_events_counts_and_blockers(monkeypatch):
    from signals.web.api import workbench

    changed_at = datetime(2026, 7, 29, 13, 10)
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    monkeypatch.setattr(workbench, "_market_today", lambda market="A": date(2026, 7, 29))
    monkeypatch.setattr(workbench, "_sync_now", lambda: datetime(2026, 7, 29, 13, 11))
    db = _Db(
        {
            "data_freshness": _Collection(
                [
                    {
                        "market": "A",
                        "freshness": "fresh",
                        "latest_dt": changed_at,
                        "updated_at": changed_at,
                        "stale_reason": "",
                    }
                ]
            ),
            "sector_transition_states": _Collection(
                [
                    {
                        "_id": "ignored",
                        "market": "A",
                        "sector_id": "bk_chip_design",
                        "turn_state": "repairing",
                        "last_changed_at": changed_at,
                        "blockers": ["etf_not_confirmed"],
                    },
                    {
                        "market": "A",
                        "sector_id": "bk_insurance",
                        "turn_state": "stable_turn",
                        "last_changed_at": datetime(2026, 7, 29, 12, 55),
                        "blockers": [],
                    },
                ]
            ),
            "sector_transition_events": _Collection(
                [
                    {
                        "event_id": "evt-1",
                        "episode_id": "episode-1",
                        "market": "A",
                        "sector_name": "数字芯片设计",
                        "from_state": "panic_release",
                        "to_state": "repairing",
                        "observed_at": changed_at,
                        "sentinels": {"capacity": ["SH.603986"]},
                    }
                ]
            ),
        }
    )
    monkeypatch.setattr(workbench, "_mongo_db", lambda: db)

    radar = workbench._sector_transition_radar()

    assert radar["counts"]["repairing"] == 1
    assert radar["counts"]["stable_turn"] == 1
    assert radar["unread_event_ids"] == ["evt-1"]
    assert radar["events"][0]["observed_at"] == "2026-07-29T13:10:00"
    assert radar["freshness"]["status"] == "fresh"
    assert radar["freshness"]["blockers"] == []


def test_sector_transition_enabled_caps_shell_cache_to_one_minute(monkeypatch):
    from signals.web.api import workbench

    payload = {"session": {"ready": True}}
    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "true")
    assert workbench._shell_cache_ttl_seconds(payload) == 60.0

    monkeypatch.setenv("SECTOR_TRANSITION_ENABLED", "false")
    assert workbench._shell_cache_ttl_seconds(payload) == workbench._SHELL_CACHE_TTL_SECONDS


def test_shell_stock_row_preserves_transition_pool_annotations():
    from signals.web.api import workbench

    slim = workbench._slim_shell_stock_row(
        {
            "symbol": "SH.603986",
            "name": "兆易创新",
            "source": "sector_transition",
            "sector_event_id": "evt-chip-turn",
            "episode_id": "ep-chip-turn",
            "first_seen_at": datetime(2026, 7, 29, 10, 5),
            "turn_state": "stable_turn",
            "sector_transition_pool": "watch",
            "sector_transition_eligibility": "buy_review_pending_individual_gates",
            "sector_transition_annotation": "板块稳定转折；个股仍未达到买点池。",
            "sector_transition_next_gate": "等待个股执行周期买点确认。",
            "sector_transition_promoted": False,
        }
    )

    assert slim["sector_event_id"] == "evt-chip-turn"
    assert slim["turn_state"] == "stable_turn"
    assert slim["sector_transition_pool"] == "watch"
    assert slim["sector_transition_eligibility"] == "buy_review_pending_individual_gates"
    assert slim["sector_transition_promoted"] is False
    assert "仍未达到买点池" in slim["sector_transition_annotation"]
