# Automation WeChat Send Contract

All Signals trading-review automations must follow this order:

1. Run the local `signals.notify.trading_workbench_summary` gate for the target
   window. Recurring automations must not add `--ignore-time`; that flag is
   only for local dry-run inspection.
2. If the first line is `DONT_NOTIFY`, stop. Do not send WeChat and do not
   synthesize a replacement review.
3. If the first line is `NOTIFY`, call
   `signals.mcp.review_assistant_server/get_market_replay_context` with the
   same `window`.
4. Read `skills/signals-replay-review/SKILL.md` and write the WeChat body only
   from `signals_context`, `market_replay`, and
   `market_replay.structured_daily_review`.
5. Send exactly one WeChat message through `$HOME/.weclaw/bin/weclaw`.

The deterministic gate body is fallback evidence, not the preferred AI-native
message. The preferred body is generated from the MCP evidence package.

## Window Arguments

| Window | Gate command format | MCP arguments |
| --- | --- | --- |
| `preopen` | `--window preopen --format wechat` | `{"window":"preopen","max_items":5,"include_event_lines":false,"include_external_fund_flows":false}` |
| `ten` | `--window ten --format wechat` | `{"window":"ten","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `midday` | `--window midday --format wechat` | `{"window":"midday","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `two` | `--window two --format wechat` | `{"window":"two","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `close` | `--window close --format wechat` | `{"window":"close","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `postmarket` | `--window postmarket --format narrative` | `{"window":"postmarket","max_items":5,"include_event_lines":false,"include_external_fund_flows":true}` |
| `weekly` | `--window weekly --format wechat` | `{"window":"weekly","max_items":5,"include_event_lines":false,"include_external_fund_flows":true}` |

## Body Rules

- Use concise Chinese trading-review prose: conclusion first, then evidence.
- Reference the strongest available evidence fields: `structured_daily_review`,
  `rotation_windows`, `rotation_shifts`, `board_timeline`, `sector_boards`,
  `dynamic_market_representatives`, `failed_boards`, `acceptance_pressure`, and
  three-pool rows. In the MCP payload this is nested at
  `market_replay.structured_daily_review`, not a top-level field.
- Missing data stays `unknown` or `待确认`.
- Do not invent minute times, account-level 主力/散户 flow, or catalyst/news.
- Eastmoney/THS 大中小单 is order-size flow only, not participant flow.
- Do not output buy/sell/target/stop commands.
- Final automation reply should only state gate result, context success, send
  outcome, and optional evaluator metric. Do not include runtime or Mongo
  health boilerplate.
