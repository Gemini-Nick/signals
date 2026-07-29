from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SKILL = ROOT / "skills" / "signals-replay-review" / "SKILL.md"


def test_replay_skill_uses_five_trader_questions_and_shared_reader_structure():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "Was the market strong, weak, or split?",
        "Where did money leave, and where did it go?",
        "genuinely strong",
        "main line extending, rotating, fading, or still unclear",
        "What two or three market behaviors matter next?",
        "**今日一句话｜**",
        "**市场全貌｜**",
        "**主线与资金｜**",
        "**代表信号｜**",
        "**明天看什么｜**",
    ):
        assert phrase in text


def test_replay_skill_has_distinct_short_and_long_reading_contracts():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
            "at most 800 visible characters",
        "about 11 body lines",
        "Do not use a table",
        "Retain all five labels",
        "Stop output immediately after the third numbered item",
        "within 3,800 visible characters",
        "up to five decision-changing intraday turns",
        "at most two strong plus two weak cases",
        "Keep the same three next-session observations",
    ):
        assert phrase in text


def test_manual_preview_is_read_only_and_stale_close_is_not_formal():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "title the chat output `A股午后观察`",
        "do not save it or present it as a formal postmarket replay",
        "the first visible line must be the title",
        "does not explicitly ask to save",
        "Do not create or modify a report, memory, log, or archive",
        "Return only the report body",
    ):
        assert phrase in text


def test_replay_skill_keeps_send_gate_and_source_boundary():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "if the first line is `DONT_NOTIFY`, stop immediately",
        "Do not call MCP, render, look up an account, or send",
        "send only the returned `body`, exactly once",
        "Do not use WorkBuddy or Tencent-watchlist conclusions",
    ):
        assert phrase in text


def test_replay_skill_rejects_engineering_language_in_reader_body():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "gate, evidence level, data completeness",
        "`partial`, `unknown`",
        "pending confirmation",
        "risk exposure",
        "runtime state",
        "source list",
        "disclaimer",
    ):
        assert phrase in text


def test_visual_edition_is_deterministic_and_optional():
    text = SKILL.read_text(encoding="utf-8")
    assert "Visual Version" in text
    assert "deterministic HTML rendering" in text
    assert "A股盘后可视复盘_YYYY-MM-DD_Signals原生.html" in text
    assert "visual failure must not block Markdown" in text


def test_global_visual_keeps_market_sessions_and_scopes_distinct():
    text = SKILL.read_text(encoding="utf-8")
    assert "add at most one compact `跨市场联动` paragraph" in text
    assert "global one-line view" in text
    assert "latest completed US session" in text
    assert 'markets=["A","HK","US","KR"]' in text
    assert "KR requires an explicit request plus `SECTOR_TRANSITION_KR_CONTEXT_ENABLED=true`" in text


def test_sector_transition_events_are_consume_only_and_do_not_change_routing():
    text = SKILL.read_text(encoding="utf-8")
    for phrase in (
        "`market_replay.sector_transitions.timeline`",
        "Consume only: do not recalculate indicators",
        "Use no more than the six timeline rows returned by Signals",
        "Do not change notification routing, send decisions, or three-pool membership",
    ):
        assert phrase in text
