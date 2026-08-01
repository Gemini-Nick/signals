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
- Do not infer overnight willingness, new positions, unwinding, sellers versus chasers, or who is taking the other side from price, volume, close location, or order-size flow.
- Do not infer customers, products, orders, or profits from price/volume evidence.
- Do not hardcode a trading day, board, sector, or stock into runtime code or prompts.

Signals is the trading-path lane. It answers how a direction formed, where it lost relative strength, and whether the
close ecosystem preserved or rejected the intraday choice. Use private/high-elasticity, quantitative cross-section,
and retail-attention lenses only as behavior archetypes; they are not account identities. Public-fund or broad
institutional language is limited to observable capacity and cross-sectional confirmation. Omit a national-team
claim unless an official disclosure supplies the subject, instrument, direction, and date; ETF price, turnover,
share change, or an afternoon support pattern cannot fill any missing element.

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

Signals earns its place through the event path: preserve at least two comparable same-day nodes and show which direction lost relative strength, which gained it, and how breadth, turnover, limit-state, or representatives changed. If only the final ranking exists, shorten the replay instead of reconstructing a switch. Treat limit-up/failed-limit data as a close ecosystem unless actual intraday pool transitions are present.

Before prose generation, emit at least the three comparable judgment cards `market/stage`,
`primary_direction/leadership`, and `next_watch/priority`. Lane-specific cards may add event-path or ecology detail,
but they may not replace the shared cards. Keep one to three forward hypotheses for the next session or the next two
known event nodes. Each hypothesis separates its historical reference class, newly arrived information, expected
observable path, and invalidators. A numeric probability is allowed only when a frozen point-in-time reference class,
sample size, and out-of-sample calibration metric are present; otherwise use ordered scenarios without percentages.

For event-driven cadence, maintain a rolling hypothesis ledger rather than a fixed story: recent A-share structure,
historical prior, official event calendar, market expectation, actual new information, and current hypothesis state.
For a “two legs” setup such as North American technology earnings followed by an FOMC decision, the first node tests
the earnings slope and the second tests the discount-rate response. Do not draw a future second bottom as fact; change
the next observation only after the market response updates the hypothesis.

Minute data supports timing only when it is available up to the requested cutoff. Missing optional detail should shorten the report, not create a reader-facing system explanation.

### Sector Transition Events

- Consume `market_replay.sector_transitions.timeline`, `states`, and `next_checks` as Python-produced evidence.
- Consume only: do not recalculate indicators, infer a missing state, or promote/downgrade a board in the skill.
- Use no more than the six timeline rows returned by Signals, and select only changes that alter the daily judgment.
- If the event package is empty, omit the transition sentence instead of rebuilding it from raw minute bars.
- Rephrase structured next checks into trader-readable observations. Do not change notification routing, send decisions, or three-pool membership.

## Reader-Facing Daily Report

Short Markdown:

`/Users/zhangqilong/WorkBuddy/WorkBuddy｜复盘工程/A股盘后复盘/Signals原生/A股盘后简报_YYYY-MM-DD_Signals原生.md`

Long Markdown and Word:

- `/Users/zhangqilong/WorkBuddy/WorkBuddy｜复盘工程/A股盘后复盘/Signals原生/A股盘后复盘报告_YYYY-MM-DD_Signals原生.md`
- `/Users/zhangqilong/WorkBuddy/WorkBuddy｜复盘工程/A股盘后复盘/Signals原生/A股盘后复盘报告_YYYY-MM-DD_Signals原生.docx`
- `/Users/zhangqilong/WorkBuddy/WorkBuddy｜复盘工程/A股盘后复盘/Signals原生/A股盘后可视复盘_YYYY-MM-DD_Signals原生.html`

Both versions answer the same five questions. The short version uses inline bold labels to reduce rendered height:

```markdown
# A股盘后复盘 | YYYY年M月D日（周X）
**今日一句话｜** …
**市场全貌｜** …
**主线与资金｜** …
**代表信号｜** …
**明天看什么｜**
1. …
2. …
3. …
```

### Short Version

- It must fit in the default WorkBuddy reading pane from the title through the third observation without scrolling: at most 800 visible characters and about 11 body lines.
- Do not use level-two or level-three headings; keep the five bold labels inline with their text to avoid Markdown heading margins.
- Keep each of the first four sections to one compact paragraph; allow at most two lines for `主线与资金` and merge all representative signals into one line.
- Do not use a table. Include no more than three indices and one sentence on turnover and breadth.
- Compress `走强` and `转弱` into one paragraph each; use no more than three directions in total.
- Include three or four unique representative stocks, plus no more than one ETF.
- Retain all five labels. Only `明天看什么` is a numbered list, with exactly three one-line items; each states what stronger behavior and still-weak behavior would look like.
- Format every item as `走强：…；仍弱：…`. Do not use confirmation, invalidation, or downgrade process language.
- Stop output immediately after the third numbered item. Never append a parenthetical format check, skill summary, or save-status sentence.
- Return only the report body. Do not append source, replay, save-status, file-path, or “no files changed” notes.

### Long Version

- Keep the same conclusions and three observations as the short version, but select facts for explanation rather than repeating the short body. Use exactly four level-two sections: `核心结论：市场处于什么阶段`, `资金路径与主线角色`, `结构强弱与代表信号`, and `明日观察`.
- Keep it within 4,500 visible characters. Do not add separate `走强方向` and `转弱方向` ranking sections.
- Add only up to five decision-changing intraday turns that actually exist; omit a missing open, morning, afternoon, or close node instead of reconstructing a four-stage path. Keep at most two strong plus two weak cases.
- Use no more than five directions, six representative stocks, two ETFs, and one table of at most six rows.
- Keep the same three next-session observations as the short version.
- Do not restore complete Top lists, data-status sections, methodology sections, or generic risk disclaimers.
- Word uses the same body as long Markdown. Word conversion must not delay the Markdown reports.
- After the four A-share level-two sections, add at most one compact `跨市场联动` paragraph. It may use independently dated
  completed Hong Kong/Korean sessions and the latest completed US session to explain what changed for A-share
  technology risk appetite. Korean context is explicit-request only and cannot upgrade or downgrade the A-share
  state. Do not copy overseas rankings into the long report.
- When reliable breadth, MA21, new-high/new-low, concentration, session-return, or multi-period index data exists,
  long may append a quantitative appendix. Read `references/visual-edition.md`; do not restore repeated Top lists.

### Visual Version

- `visual` is a deterministic HTML rendering of the same short/long facts, not another model summary.
- The first screen carries the short conclusion; charts drill into the long analysis; the quantitative appendix is
  collapsed by default.
- The global page orders six reader chapters: global one-line view, A-share market, Hong Kong market and internet
  leaders, US indices plus Mag7/AI chain, A+H and China-US AI-chain links, and next-session observations.
- Each market keeps its own completed session date, timezone, currency, and coverage scope. Hong Kong and US
  sections must not inherit A-share limit-up/failed-limit language.
- Date, state, direction labels, representatives, key numbers, and the three observations must match short and long.
- Missing formal close changes the whole page to `A股午后观察`; visual failure must not block Markdown.
- The primary Signals chart is a board relative-strength event graph with no more than four comparable series and
  decision-changing timestamps. A second chart may show limit-up, failed-limit, limit-down, or strong-pool ecology
  only when the pool definition is stable across timestamps.
- Do not render untimed standalone representative sparklines. A representative path must share an explicit time axis
  and comparison baseline; otherwise use a compact evidence table. K-lines are conditional evidence only and require
  complete OHLCV, frequency, timezone, adjustment method, session status, and missing-data state.

### Language

Write market behavior directly:

- “资金从电新和资源撤出，转向设备与封测。”
- “指数被权重托住，但多数个股仍弱。”
- “只有少数高成交核心活跃，板块没有扩散。”

Do not expose production language such as gate, evidence level, data completeness, `partial`, `unknown`, downgrade, pending confirmation, confirmation condition, invalidation condition, risk exposure, interface, field, runtime state, source list, data note, or disclaimer.

Do not provide direct trading instructions, target prices, stop-loss levels, automated orders, exact position sizes, or account-identity claims.

If the available Signals context does not cover the formal close, title the chat output `A股午后观察`; do not save it or present it as a formal postmarket replay.
Even then, the first visible line must be the title. Never prepend or append `formal_ready`, close-source, internal-state,
replay-mode, or “no file written” explanations.

When the user asks only to generate, replay, preview, or compare and does not explicitly ask to save, return the short report in chat only. Do not create or modify a report, memory, log, or archive. Write files only for an explicitly requested save or a formal automated run.

## Pre-Send Check

Before a recurring body is sent:

- The opening sentence must name the real market structure, not a vague “market divergence”.
- Every named direction and stock must have same-day support in the MCP payload, user attachment, or `extra_facts[]`.
- A money-switch claim must describe both the withdrawing side and the receiving side.
- Strong/weak direction language must match board breadth and representative paths.
- Optional missing details must be omitted from the body.
- Any unsupported numeric, news-cause, participant-flow, or exact-timing claim must be removed.
- Rewrite intent claims as observable behavior: use “the index closed near the session low” instead of “money refused to stay overnight”, and “high-turnover hardware leaders faded from the high” instead of “unwinding money escaped”.
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
- `get_market_replay_context` accepts optional `markets=["A","HK","US","KR"]`; omitting it preserves the A-share-only
  response. KR requires an explicit request plus `SECTOR_TRANSITION_KR_CONTEXT_ENABLED=true`; otherwise return it as
  disabled/unavailable. It remains external context only. At A-share postmarket, US means the latest completed US
  session, not the Beijing calendar date.
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
