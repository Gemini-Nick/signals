# Automation WeChat Send Contract

This contract applies to automated/recurring Signals trading-review delivery.
Manual review requests follow the separate manual rules below.

All automated Signals trading-review runs start the same way:

1. Run the local `signals.notify.trading_workbench_summary` gate for the target
   window. Recurring automations must not add `--ignore-time`; that flag is
   only for local dry-run inspection.
2. If the first line is `DONT_NOTIFY`, stop the automated path. Do not call MCP, do not send
   WeChat, and do not synthesize a replacement review.
3. If the first line is `NOTIFY`, continue through the delivery mode in the
   table below. The deterministic gate body is fallback evidence only; it is
   never the recurring send body.

There are two delivery modes:

- `renderer`: call
  `signals.mcp.review_assistant_server/render_market_replay_wechat_body` with
  the window arguments. The renderer collects market replay context internally.
  Send only the returned `body` when its returned `status` is `NOTIFY`.
- `synthesis`: call
  `signals.mcp.review_assistant_server/get_market_replay_context`, then write a
  fresh AI-native body from `signals_context`, `market_replay`,
  `market_replay.structured_daily_review`, and `analysis_framework`.

If MCP context collection, rendering, or AI synthesis fails, stop with
`context_failed_no_send`. `body_source=fallback_script` is a no-send failure
state, not a delivery path. Send exactly one WeChat message through
`$HOME/.weclaw/bin/weclaw`.

## Manual Execution

When the user explicitly runs a window or asks for an out-of-window review:

1. Run a local dry-run with `--ignore-time --allow-ignore-time-notify`; add
   `--safe-inputs --input-timeout 6`.
2. Never send from the manual dry-run. External delivery remains disabled even
   if its first line is `NOTIFY`.
3. If the original automatic gate would be `DONT_NOTIFY`, report that reason
   but still return the requested preview or MCP context when data is available.
4. State the underlying trade date/data timestamp so historical data is not
   described as real-time.
5. If data collection fails, return an explicit diagnostic rather than a bare
   `DONT_NOTIFY` or `stopped` status.

## Window Arguments

| Window | Gate command format | Mode | MCP arguments |
| --- | --- | --- | --- |
| `preopen` | `--window preopen --format wechat` | `renderer` | `{"window":"preopen","max_items":5,"include_event_lines":false,"include_external_fund_flows":false}` |
| `ten` | `--window ten --format wechat` | `renderer` | `{"window":"ten","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `midday` | `--window midday --format wechat` | `renderer` | `{"window":"midday","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `two` | `--window two --format wechat` | `renderer` | `{"window":"two","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `close` | `--window close --format wechat` | `renderer` | `{"window":"close","max_items":5,"include_event_lines":true,"include_external_fund_flows":false}` |
| `postmarket` | `--window postmarket --format narrative --safe-inputs --input-timeout 6` | `synthesis` | `{"window":"postmarket","max_items":5,"include_event_lines":false,"include_external_fund_flows":true}` |
| `weekly` | `--window weekly --format wechat` | `synthesis` | `{"window":"weekly","max_items":5,"include_event_lines":false,"include_external_fund_flows":true}` |

## Body Rules

- Use concise Chinese trading-review prose: conclusion first, then the market behavior that supports it.
- Renderer windows must not be rewritten by the automation agent. Send only the
  JSON `body` returned by `render_market_replay_wechat_body`; record
  `audit.internal_gaps` in automation memory only.
- Synthesis windows must do a real model synthesis pass before writing. The
  draft must follow the skill sections `Internal Analysis`, `Reader-Facing Daily Report`,
  and `Pre-Send Check`, not rewrite the deterministic gate body.
- Reference the strongest available evidence fields: `structured_daily_review`,
  `rotation_windows`, `rotation_shifts`, `board_timeline`, `sector_boards`,
  `dynamic_market_representatives`, `failed_boards`, `acceptance_pressure`, and
  three-pool rows. When `sector_transitions` is present, consume its `timeline`
  and `next_checks` directly; the renderer/agent must not recalculate or upgrade
  the Python-produced state. These fields live under `market_replay`;
  `structured_daily_review` is specifically nested at
  `market_replay.structured_daily_review`, not at the response top level.
- For minute rotation, board timeline, dynamic representatives, failed boards,
  high-turnover cores, acceptance pressure, and flow availability, treat
  `market_replay.*` as canonical. Do not mark those fields missing just because
  they are absent from `signals_context`.
- Treat `sector_boards` / Agent OS `板块15` as the primary sector ordering.
  Use `dynamic_market_representatives` for the day's representative stocks;
  static industry-chain representatives are background only.
- First sentence must name the actual pressure center, strongest/weakening
  direction, and the most important next observation. Avoid vague phrases unless
  the exact board or stock is named in the same sentence.
- Direction-switch claims must cite rotation evidence from
  `rotation_windows`, `rotation_shifts`, `board_timeline`, tail pressure, or
  representative acceptance. Final涨幅 alone is not enough.
- Named stocks must be representative for the current day through dynamic
  representative buckets, high turnover, limit/failed-limit state, 5-minute
  path, or pool membership.
- The WeChat body should only include supported facts. Optional missing
  fields go to automation memory via `audit.internal_gaps`, not into the
  user-visible body.
- Do not invent minute times, account-level 主力/散户 flow, or catalyst/news.
- Eastmoney/THS 大中小单 is order-size flow only, not participant flow.
- Do not output buy/sell/target/stop commands.
- If the pre-send self-check fails and cannot be fixed with available evidence,
  record `context_failed_no_send` and skip WeChat.
- Before automated sending, reject any body containing `缺失`, `unknown`, `unavailable`,
  `数据边界`, `字段缺失`, `participant_flow`, `market_replay`, or
  `signals_context`.
- Final automation reply should only state gate result, render/context/synthesis
  result, body source, send outcome, and optional evaluator metric. Use
  `MCP renderer` for renderer windows and `AI 原生` for synthesis windows. Do
  not include runtime or Mongo health boilerplate.

## Send And Memory Rules

- Resolve the recipient from the newest im-bot account file:
  `ls -t $HOME/.weclaw/accounts/*-im-bot.json | head -1`, then read
  `ilink_user_id`.
- Send once with `$HOME/.weclaw/bin/weclaw send --to ... --text ...`.
- Append a short Chinese status entry to the automation's `memory.md` on every
  `NOTIFY`, `DONT_NOTIFY`, context failure, render/synthesis failure, send
  failure, or timeout.
- Memory labels should stay stable: `运行时间`、`门禁结果`、`上下文获取` or
  `渲染结果`、`正文来源`、`微信发送`、`失败原因`、`内部缺口`.
- Keep internal gaps and forbidden-token hits in memory only. Do not leak them
  into the WeChat body.
