# -*- coding: utf-8 -*-
from __future__ import annotations

from signals.sync.modules.knowledge_market_views import sync_knowledge_market_views


class _Collection:
    def __init__(self):
        self.docs = {}

    def update_one(self, query, update, upsert=False):
        key = query.get("view_id") or tuple(sorted(query.items()))
        self.docs[key] = dict(update.get("$set") or {})


class _Db(dict):
    def __getitem__(self, key):
        if key not in self:
            self[key] = _Collection()
        return super().__getitem__(key)


def test_knowledge_market_views_reads_desktop_vault_strategy_rules(tmp_path, monkeypatch):
    vault = tmp_path / "知识库"
    (vault / "10 Knowledge").mkdir(parents=True)
    (vault / "10 Inbox" / "WeChat" / "2026-04").mkdir(parents=True)
    (vault / "20 Sources").mkdir(parents=True)
    (vault / "30 Assets" / "Originals").mkdir(parents=True)
    (vault / "10 Knowledge" / "道长-市场框架.md").write_text(
        """---
title: "道长-市场框架"
author_focus: "daozhang"
---
# 道长-市场框架
- 先看市场阶段，再看板块。
- 不能只看沪指，要同时看国证2000、创业板、科创50、超大盘。
- 高位强势兑现，低位确定性承接。
""",
        encoding="utf-8",
    )
    (vault / "10 Inbox" / "WeChat" / "2026-04" / "2026-04-28胖哥观点.md").write_text(
        """---
title: "胖哥观点"
author_focus: "pangge"
asset_path: "asset_paths/pangge.png"
---
# 胖哥观点
- 盘中策略是等条件，不是猜方向。
- 趋势弱时不做左侧，要等止跌、放量或右侧确认。
- 公开利好不等于可交易，要看赔率、拥挤度、参与性。
""",
        encoding="utf-8",
    )
    monkeypatch.setenv("SIGNALS_KNOWLEDGE_VAULT_DIR", str(vault))

    import signals.research as research

    monkeypatch.setattr(research, "load_all_notes", lambda notes_dir: [])
    db = _Db()

    result = sync_knowledge_market_views(db)

    assert result["vault_docs"] == 2
    assert result["strategy_rule_views"] >= 3
    combined = db["knowledge_market_views"].docs["strategy:combined:market_rules"]
    assert combined["target_type"] == "strategy_rule"
    assert combined["knowledge_effect"] == "context_only"
    assert "右侧确认" in combined["right_side_requirement"]
    assert combined["sources"]
    assert any("pangge.png" in path for path in combined["asset_paths"])
