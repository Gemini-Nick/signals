---
name: signals-replay-review
description: Use when generating or reviewing Signals/Agent OS A-share postmarket replays, including local Markdown/Word reports and MCP-backed WeChat briefs. The skill keeps Signals evidence independent, reconstructs board rotation and representative paths, and writes concise trader-readable conclusions.
---

# Signals Replay Review

## Goal

Use the Signals full-market context to answer five trader questions:

1. Was the market strong, weak, or split?
2. Where did money leave, and where did it go?
3. Which directions were genuinely strong, and which were only busy?
4. Was the main line extending, rotating, fading, or still unclear?
5. What two or three market behaviors matter next?

The runtime may keep detailed provenance, coverage, and validation state. The reader-facing replay only explains the market.

## Source Boundary

- Use local Signals, Agent OS, Mongo, user attachments, and explicit `extra_facts[]`.
- Do not use WorkBuddy or Tencent-watchlist conclusions to fill Signals gaps.
- Do not infer account identity from Eastmoney/THS order-size flow.
- Do not infer customers, products, orders, or profits from price/volume evidence.
- Do not hardcode a trading day, board, sector, or stock into runtime code or prompts.

See `references/data-requirements.md` for the detailed internal checklist.

## Execution And Send Gate

Keep information generation separate from notification:

- Automated execution: if the first line is `DONT_NOTIFY`, stop immediately. Do not call MCP, render, look up an account, or send.
- Automated `NOTIFY`: continue only through the path defined for that window.
- Missing/timeout/unknown gate result is a failed gate, never `DONT_NOTIFY`; do not send a fallback body.
- Manual execution may inspect a local preview outside the window, but sending stays disabled unless the user explicitly asks to send.

Recurring WeChat automations must read `references/automation-wechat-send.md`.

- Intraday windows use `render_market_replay_wechat_body`; send only the returned `body`, exactly once.
- Postmarket and weekly windows use the script only as the gate, then collect MCP context and perform one AI synthesis pass.
- If context collection, rendering, or synthesis fails after `NOTIFY`, stop without sending.
- Never use `--training-sample` in recurring sends.

## Fast Path

Run from `/Users/zhangqilong/github代码仓库/Signals`.

Collect the structured market context:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_market_replay_context","arguments":{}}}' \
  | bash scripts/python.sh -m signals.mcp.review_assistant_server
```

Postmarket gate/evidence preview:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary \
  --window postmarket --max-items 5 --format narrative
```

Manual or historical Word-style dry-run:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary \
  --window postmarket --trade-date YYYY-MM-DD --max-items 5 --format word \
  --safe-inputs --input-timeout 6 --ignore-time --allow-ignore-time-notify \
  --eval-target YYYY-MM-DD-word
```

Use `--ignore-time` only for local inspection or historical restoration. It must not bypass a recurring send gate.

## Internal Analysis

Build the analysis from `signals_context`, `market_replay`, and `analysis_framework`.

1. Compare index performance with market breadth and turnover. A stable index with weak breadth is not a healthy market.
2. Reconstruct rotation from `rotation_windows`, `rotation_shifts`, `board_timeline`, and representative paths. Final returns alone do not prove a switch.
3. Identify both sides of the money path: the direction losing support and the direction gaining participation.
4. Rank directions using breadth, amount, persistence, limit/failed-limit state, and representative-stock acceptance.
5. Separate a broad main line from a narrow cluster of high-turnover stocks.
6. Use same-day dynamic representatives for capacity, strength, acceptance, and weakness. Static industry representatives are background only.
7. Turn the result into two or three next-session observations, not buy/sell/target/stop instructions.

Minute data supports timing only when it is available up to the requested cutoff. Missing optional detail should shorten the report, not create a reader-facing system explanation.

## Reader-Facing Daily Report

Short Markdown:

`/Users/zhangqilong/WorkBuddy/复盘报告/A股盘后简报_YYYY-MM-DD_Signals原生.md`

Long Markdown and Word:

- `/Users/zhangqilong/WorkBuddy/复盘报告/A股盘后复盘报告_YYYY-MM-DD_Signals原生.md`
- `/Users/zhangqilong/WorkBuddy/复盘报告/A股盘后复盘报告_YYYY-MM-DD_Signals原生.docx`

Both versions use the same five-section hierarchy. The short version does not add level-three headings:

```markdown
# A股盘后复盘 | YYYY年M月D日（周X）
## 今日一句话
## 市场全貌
## 主线与资金
## 代表信号
## 明天看什么
```

### Short Version

- It must fit in the WorkBuddy reading pane without scrolling: at most 1,000 visible characters and about 16 body lines.
- Do not use a table. Include no more than three indices and one sentence on turnover and breadth.
- Compress `走强` and `转弱` into one paragraph each; use no more than three directions in total.
- Include three or four unique representative stocks, plus no more than one ETF.
- End with exactly three one-line observations. Each states what stronger behavior and still-weak behavior would look like.
- Return only the report body. Do not append source, replay, save-status, file-path, or “no files changed” notes.

### Long Version

- Use the same five sections and the same facts as the short version.
- Keep it within 3,800 visible characters. Level-three `走强方向` and `转弱方向` headings are allowed here.
- Add only up to five decision-changing intraday turns, stronger/weaker comparisons, and at most two strong plus two weak cases.
- Use no more than five directions, six representative stocks, two ETFs, and one table of at most six rows.
- Keep the same three next-session observations as the short version.
- Do not restore complete Top lists, data-status sections, methodology sections, or generic risk disclaimers.
- Word uses the same body as long Markdown. Word conversion must not delay the Markdown reports.

### Language

Write market behavior directly:

- “资金从电新和资源撤出，转向设备与封测。”
- “指数被权重托住，但多数个股仍弱。”
- “只有少数高成交核心活跃，板块没有扩散。”

Do not expose production language such as gate, evidence level, data completeness, `partial`, `unknown`, downgrade, pending confirmation, confirmation condition, invalidation condition, risk exposure, interface, field, runtime state, source list, data note, or disclaimer.

Do not provide direct trading instructions, target prices, stop-loss levels, automated orders, exact position sizes, or account-identity claims.

If the available Signals context does not cover the formal close, title the chat output `A股午后观察`; do not save it or present it as a formal postmarket replay.

When the user asks only to generate, replay, preview, or compare and does not explicitly ask to save, return the short report in chat only. Do not create or modify a report, memory, log, or archive. Write files only for an explicitly requested save or a formal automated run.

## Pre-Send Check

Before a recurring body is sent:

- The opening sentence must name the real market structure, not a vague “market divergence”.
- Every named direction and stock must have same-day support in the MCP payload, user attachment, or `extra_facts[]`.
- A money-switch claim must describe both the withdrawing side and the receiving side.
- Strong/weak direction language must match board breadth and representative paths.
- Optional missing details must be omitted from the body.
- Any unsupported numeric, news-cause, participant-flow, or exact-timing claim must be removed.
- The short body must satisfy the actual one-screen structure above; do not expose character or number counting in the response.

If the draft cannot be corrected without adding unsupported facts, stop and do not send.

## MCP Surface

Start the stdio server:

```bash
bash scripts/python.sh -m signals.mcp.review_assistant_server
```

Tools:

- `get_signals_replay_context`: compact local Signals context.
- `get_market_replay_context`: full-market event graph from local endpoints and Mongo.
- `get_replay_analysis_framework`: reusable internal reasoning framework.
- `render_market_replay_wechat_body`: deterministic intraday WeChat body.
- `generate_signals_replay_review`: deterministic evidence preview, not the final recurring WeChat body.
- `list_signals_replay_data_requirements`: detailed input checklist.

## Historical Evaluation

The screenshot and Word samples are evaluator targets only:

- `signals/replay/references/2026-06-05-screenshot.txt`
- `signals/replay/references/2026-06-29-word.txt`
- `skills/signals-replay-review/references/example-2026-06-05.md`
- `skills/signals-replay-review/references/comparison-2026-06-05.md`

Do not paste sample-specific facts into runtime generators, MCP tools, or daily automation prompts.
