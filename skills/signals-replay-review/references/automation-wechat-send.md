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
5. If MCP context or AI synthesis fails, stop with `context_failed_no_send`.
   Do not send the deterministic script body.
6. Send exactly one AI-native WeChat message through `$HOME/.weclaw/bin/weclaw`.

The deterministic gate body is fallback evidence, not the send body. Recurring
reviews must use `body_source=ai_native`; `body_source=fallback_script` is a
no-send failure state, not a delivery path.

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
- Do a real model synthesis pass before writing. The draft must be based on the
  skill sections `AI Synthesis Contract` and `Pre-send Self Check`, not on a
  rewrite of the deterministic gate body.
- Reference the strongest available evidence fields: `structured_daily_review`,
  `rotation_windows`, `rotation_shifts`, `board_timeline`, `sector_boards`,
  `dynamic_market_representatives`, `failed_boards`, `acceptance_pressure`, and
  three-pool rows. In the MCP payload this is nested at
  `market_replay.structured_daily_review`, not a top-level field.
- Treat `sector_boards` / Agent OS `板块15` as the primary sector ordering.
  Use `dynamic_market_representatives` for the day's representative stocks;
  static industry-chain representatives are background only.
- First sentence must name the actual pressure center, confirmed/weakening
  direction, and next validation point. Avoid vague phrases unless the exact
  board/stock and evidence are named in the same sentence.
- Direction-switch claims must cite rotation evidence from
  `rotation_windows`, `rotation_shifts`, `board_timeline`, tail pressure, or
  representative acceptance. Final涨幅 alone is not enough.
- Named stocks must be representative for the current day through dynamic
  representative buckets, high turnover, limit/failed-limit state, 5-minute
  path, or pool membership.
- Missing data stays `unknown` or `待确认`.
- Do not invent minute times, account-level 主力/散户 flow, or catalyst/news.
- Eastmoney/THS 大中小单 is order-size flow only, not participant flow.
- Do not output buy/sell/target/stop commands.
- If the pre-send self-check fails and cannot be fixed with available evidence,
  record `context_failed_no_send` and skip WeChat.
- Final automation reply should only state gate result, context success,
  `body_source=ai_native`, send outcome, and optional evaluator metric. Do not
  include runtime or Mongo health boilerplate.
