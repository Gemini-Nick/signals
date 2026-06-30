---
name: signals-replay-review
description: Use when generating A-share postmarket replay reviews in the user's screenshot style, especially for Signals/Agent OS reviews that need board-15 sector ranking, intraday turning points, pool consensus, tail-session emotion, and next-day validation points. Also use when packaging this review workflow as a Codex skill or MCP-backed review assistant.
---

# Signals Replay Review

This is an AI-native replay skill. The runtime/MCP layer builds a structured
full-market evidence graph; the AI layer writes the screenshot-style long
review from that graph. Do not hardcode a specific day into Python.

This skill now fuses two contracts:

- Signals replay contract: use local Signals/Agent OS/Mongo evidence to rebuild
  board rotation, high-turnover pressure, pool consensus, tail emotion, and
  next-day validation.
- Structured daily-review contract: use fixed samples, fixed time slices,
  evidence levels, Top7 board views, 20-day trend comparison, acceptance
  analysis, and explicit data-completeness notes.

The output is a replay evidence package first and prose second. Every strong
claim must be traceable to data in `signals_context`, `market_replay`, user
attachments, or `extra_facts[]`.

## Fast Path

From `/Users/zhangqilong/github代码仓库/Signals`, get the evidence graph first:

```bash
printf '%s\n' '{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"get_market_replay_context","arguments":{}}}' \
  | bash scripts/python.sh -m signals.mcp.review_assistant_server
```

Then ask the AI to write from the returned `signals_context`,
`market_replay`, and `analysis_framework`.

For a direct CLI-generated long replay review during the real postmarket
window:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary --window postmarket --max-items 5 --format narrative
```

Use `--ignore-time` only for local inspection or historical dry-runs. A
dry-run must not be used as the WeChat send gate unless the user explicitly
asks for a manual out-of-window send.

For intraday Codex WeChat automations, the preferred path is now AI-native:
collect the structured evidence package, let the automation agent write a short
review from that package, then send the generated body once through WeClaw.
Use `--format wechat` only as the deterministic gate and emergency evidence
preview. It is not an allowed send body for recurring review automations. If
MCP context collection or AI synthesis fails after a `NOTIFY` gate, stop and
report `context_failed_no_send` rather than sending the script body:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary --window <preopen|ten|midday|two|close> --max-items 5 --format wechat
```

For daily postmarket WeChat automation, use narrative output only as the local
gate/evidence preview. A `NOTIFY` gate still requires MCP context collection and
AI synthesis before any WeChat send:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary --window postmarket --max-items 5 --format narrative
```

For screenshot-target training/evaluation, pipe or attach the replay evaluator.
The evaluator target is a training target, not the daily output source:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary \
  --window postmarket --max-items 5 --ignore-time --format narrative \
  --eval-target 2026-06-05-screenshot --min-similarity 0.95 --require-eval-phrases
```

Daily postmarket automation should send the generalized AI-native review when
the evidence package is available and the first line gate permits it. The
2026-06-05 screenshot evaluator is a parallel quality monitor: it can report
`EVAL_BLOCKED` or `EVAL_PASS_NEEDS_MANUAL_REVIEW`, but exact screenshot
similarity must not be required for daily generalized postmarket sending.
Keep `--training-sample` out of recurring daily sends.

For a manual 2026-06-05 training-sample replay, render from structured facts
and require exact equality. This defaults to `DONT_NOTIFY` so the golden sample
cannot be sent by accident:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary \
  --window postmarket --ignore-time --format narrative \
  --training-sample 2026-06-05-screenshot \
  --eval-target 2026-06-05-screenshot --min-similarity 1.0 --require-eval-phrases
```

This path is only for the screenshot training sample. Normal daily replays
leave `--training-sample` empty and must still use live Signals/Mongo evidence.
Only add `--allow-training-sample-send` for an explicit manual send.

For screenshot simulations or outside facts, pass facts explicitly instead of changing code:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary --window postmarket --max-items 5 --ignore-time --format narrative --extra-fact "中际旭创单日成交约583亿，冲高回落无承接"
```

Use these instead of rewriting `/api/pack/dashboard`, `/api/workbench/shell`,
`/api/strategy/snapshot`, or Mongo queries by hand. The first output line
remains the gate for generated review text:

- `NOTIFY`: continue to MCP context collection and AI synthesis before sending.
- `DONT_NOTIFY`: stop and report the reason.
- `[replay-eval] send blocked`: stop and report the similarity/missing phrases; do not send.

## Automation Contract

Use this skill to guide what the automation agent should produce. Delivery is
still mechanical: one generated body, one send, no retries unless explicitly
requested.

For recurring Codex WeChat automations, read
`references/automation-wechat-send.md` first. That file is the canonical compact
contract for gate commands, MCP arguments, synthesis boundaries, and send rules.

- Intraday windows (`preopen`, `ten`, `midday`, `two`, `close`): call
  `get_market_replay_context` or direct local commands to obtain
  `market_replay.structured_daily_review`, `rotation_windows`,
  `rotation_shifts`, `sector_boards`, and pool rows. Write a concise AI-native
  review using only available evidence. If context collection or AI synthesis
  fails, do not send a deterministic script body.
- Postmarket daily review (`postmarket`): use `--format narrative`; first line
  is still the fallback gate, not the final body. Automation output must be
  AI-written from the structured evidence package plus
  `format_market_replay_sections`; after AI synthesis, send exactly once if the
  generated body does not violate the evidence boundaries. If AI synthesis is
  unavailable, stop without sending. Do not add `--ignore-time` in recurring
  postmarket sends.
- Golden screenshot evaluator: report-only unless the user explicitly asks for
  a manual sample send. Never use `--training-sample` in recurring daily sends.
- Final automation status should state only gate result, context/synthesis
  result, send outcome, and `body_source=ai_native`. Do not include
  runtime/Mongo/lane health boilerplate in the user-facing WeChat result.

## AI Synthesis Contract

Every recurring replay send must include a real model synthesis pass after MCP
context collection. The model is not a paraphraser for the script body. It must
use the evidence package to decide what mattered, what did not matter, and what
must be verified next.

Before writing the WeChat body, the automation agent must build these internal
working notes from `signals_context`, `market_replay`,
`market_replay.structured_daily_review`, and `analysis_framework`:

1. `data_boundary`: which required data is `available`, `partial`, `missing`,
   or `unknown`; missing stock daily/minute data must downgrade stock-specific
   claims.
2. `board_heat_order`: the current `板块15` / `sector_boards` order, plus
   `source_driver`, `board_timeline`, `rotation_windows`, and
   `rotation_shifts`. This is the primary sector truth.
3. `direction_state`: for each important board, classify it as confirmed
   main line, hidden/secondary line, temporary carding, false main line,
   weakening line, or evidence-insufficient line. The reason must come from
   board order, intraday rotation, breadth, amount, and representative paths.
4. `representative_selection`: choose same-day representatives from
   `dynamic_market_representatives`, `high_turnover_cores`,
   `representative_paths`, limit/failed-limit pools, and three-pool rows.
   Static industry-chain representatives are background only and cannot replace
   same-day market representatives.
   Do not assign preset higher weights to any specific industry or board; rank
   candidates only by same-day evidence such as amount, change, turnover,
   limit-pool state, board linkage, and acceptance/pressure.
5. `pressure_acceptance`: identify high-turnover pressure, failed boards,
   high-to-close drawdown, close position, pullback amount share, and tail
   emotion when those fields exist.
6. `switch_explanation`: explain whether capital switched, repaired, split, or
   failed to choose a direction. This must compare at least two evidence
   surfaces, not only final涨幅.
7. `next_validation`: convert the judgment into 2-4 validation points with
   negation conditions. Do not output buy/sell/target/stop instructions.

The final WeChat body must be the model's compressed conclusion from these
notes. It should not expose the full internal table unless the user asks for a
manual full report.

## Pre-send Self Check

Before sending, the automation agent must read the draft body once and fix it
until all checks pass:

- First sentence names the real structure: pressure center, strongest confirmed
  board, failed/dragging board, and next validation point. Vague openings such
  as `指数不差`, `科技高成交`, `先修的票`, or `继续拖的核心` are not acceptable unless
  the sentence immediately names the exact board/stock and evidence.
- Every sector judgment follows `sector_boards` / Agent OS `板块15` unless that
  surface is missing; if missing, say so and downgrade confidence.
- Every named representative stock is justified by same-day evidence:
  dynamic representative bucket, high turnover, limit/failed-limit state,
  representative 5-minute path, or pool membership.
- Every numeric claim is present in the MCP payload, script gate output, user
  attachment, or `extra_facts[]`. If the value is absent, write `待确认/unknown`
  or remove it.
- Direction-switch language must cite rotation evidence:
  `rotation_windows`, `rotation_shifts`, `board_timeline`, tail pressure, or
  representative acceptance. Do not infer switching from final涨幅 alone.
- Eastmoney/THS 大中小单 is order-size flow only. Do not call it account-level
  主力/散户 unless that participant-flow field is explicitly available.
- The body is a trading review, not a system status summary. It must not include
  runtime, Mongo, lane, or API health boilerplate.
- If any check cannot be fixed because evidence is missing or AI synthesis is
  unavailable, stop and record `context_failed_no_send`; do not send a fallback
  script body.

## AI-Native Process

The thinking process is reusable across dates:

0. Check data completeness before judging: indices, market breadth/turnover,
   board daily/minute data, stock daily/minute data, limit/failed-limit pools,
   20-day board history, flow fields, and user holdings/sim records.
1. Confirm real market damage: index drops,成交额承接, breadth/sector spread,
   limit-up/down emotion, failed-board rate, and tail pressure.
2. Build fixed samples before interpretation: full-market成交额 Top50,
   amount-ratio Top20, gainers/losers Top20 after liquidity/ST filters,
   limit-up/limit-down/failed-limit pools, user pools, Top7 turnover boards,
   and liquidity-filtered 20-day trend Top7 boards when available.
3. Rebuild fixed intraday slices: 09:15-09:25, 09:30-10:00, 10:00-10:30,
   10:30-11:00, 11:00-11:30, 13:00-13:30, 13:30-14:00, 14:00-14:30,
   14:30-15:00. If minute data is missing, mark the slice `unknown` instead
   of inventing a time.
4. Rebuild minute-level board rotation: compare `rotation_windows`,
   `rotation_shifts`, and `board_timeline`, not only final涨幅.
5. Map board strength back to `板块15`: split into confirmed chain,
   source-strong chain-weak, chain-internal split, temporary carding, no winner,
   and false main line.
6. Compare today's Top7 turnover boards with the 20-day trend Top7 boards:
   classify as trend continuation, trend climax, trend dulling, high-level
   pullback, daily rotation, or low-liquidity exclusion.
7. Split representatives into two layers: `static_representatives` from the
   industry-chain knowledge base, and `dynamic_market_representatives` from
   that day's market recognition.
8. Pick dynamic core by成交额/换手/市场共识/负反馈 pressure; pick confirmed elastic
   by涨停速度/连板/封板资金/涨幅/量比/换手/板块共振; keep炸板/冲高回落 in
   `failed_emotion`, not confirmed elastic.
9. Verify with representative 5-minute paths: low/high/large-amount bars must
   align with board strengthening. Compute or infer acceptance from close
   position, high-to-close drawdown, and pullback amount share when data exists.
10. Score line-heads (`线头`) from T-3 to T-1 for strong boards and key stocks:
    prior relative strength, prior amount expansion, stabilization, sufficient
    previous drawdown, and today's breadth/limit spread.
11. Judge tail emotion through failed boards, failed limits, high-volume core
    acceptance, and whether strong boards expanded or got drained.
12. Convert the result into next-day validation points, never direct buy/sell
    instructions.

For dynamic representative quality, prefer Mongo `market_limit_pools` because it
stores AkShare/Eastmoney涨停、炸板、跌停、强势池 with `snapshot_minute`.
Daily final-state pools can restore end-of-day quality; only intraday sampling
can restore the timing of recognition and loss of recognition.

## Review Shape

Write like the sample screenshot:

1. Start with conclusion first: market state, emotion temperature, real
   pressure center, confirmed/hidden/false main lines, biggest risk, and the
   most important next-day validation.
2. Immediately state data completeness. Use `available`, `missing`, or
   `partial`; explain how missing minute, board history, or flow data weakens
   the conclusion.
3. Name the index damage and high-volume core pressure.
4. Reconstruct the fund-flow chain as long paragraphs for WeChat/narrative
   output, or tables for a full manual report. Use exact times, prices,
   turnover, and net-flow figures only when they are present in the replay
   context, user attachments, or extra facts.
5. Treat `板块15` as the primary sector truth. Do not collapse it into only
   `daily_brief.primary_theme`.
6. Compare sector ranking against `线索池 / 盯盘池 / 买点池 / 风险池`.
7. Include carding structure, emotion temperature, time-cycle position,
   Top7/20-day trend comparison when available, tail acceptance, and next-day
   validation points.
8. Do not write a short system summary when the user asks for screenshot
   restoration; use the sample as style guidance, not as runtime hardcoded
   content.

### Word-Style Daily Note Shape

For manual daily notes and Word-style postmarket reports, restore the user's
report shape instead of compressing into the normal WeChat brief. The 2026-06-29
Word target is the current full-note reference:

- `signals/replay/references/2026-06-29-word.txt`
- `signals/replay/references/2026-06-29-word-phrases.json`

Use that target for structure and tone, not as hardcoded runtime content. A
high-restoration daily note should keep these blocks in order:

1. Title and date: `A股盘后复盘报告 | YYYY年M月D日（周X）`.
2. `昨日观察点校准`: if a previous daily note exists, check each prior
   validation point before today's conclusion. For every item, state the
   observed object, trigger condition, whether it triggered, what actually
   happened, and whether the prior judgment was calibrated or missed. If no
   previous note is available, write `昨日观察点暂无可校准样本`. If the same type of
   judgment misses three consecutive times, explicitly mark it as a recurring
   deviation and name the weak assumption.
3. `核心结论`: one dense conclusion paragraph naming market split, main inflow,
   main outflow, breadth/emotion, and where money was made or lost.
4. `一、市场整体状态`: index table, intraday structure, technical table, and a
   market-state sentence. Do not replace this with a single index summary.
5. `二、市场情绪`: limit-up/limit-down/封板率/连板 table, then microstructure and
   emotion-stage paragraphs.
6. `三、板块深度拆解`: strongest Top10 table, weakest-direction table, then
   separate directional paragraphs for the active main line, candidate main
   line, retreating line, and defensive/补涨 line.
7. `四、个股精选`: capacity/core stocks and unusual observation stocks, each with
   same-day structure and next-day observation.
8. `五、强趋势股启动回溯`: select from the strongest Top10/Top15 boards and
   write 3-5 high-value cases when data permits. Include at least one trend
   continuation winner and at least one same-board failed or weakening sample;
   if no valid failed sample exists, state `本板块无有效失败对比样本`. For each
   winner, write startup recognition, board linkage, a daily replay table
   (`日期 / 涨跌幅 / 成交额 / 换手率 / 盘中低点 / 当日关键事件`),
   retrospective entry-and-drawdown review, style fit, next validation, and a
   success-vs-failure comparison. The comparison must conclude which dimension
   made the failed sample weaker; do not write `各有优劣`.
9. `六、风险提示`: numbered risks tied to observable market structure. Cover at
   least three relevant risks from the menu when evidence exists: 放量滞涨,
   高位股负反馈, 板块冲高回落, 指数与个股背离, 缩量反弹, 情绪高潮后分歧,
   连板断层, 尾盘跳水/抢跑, 科创/北交所超买乖离.
10. `七、明日观察清单`: each item must include observed object, key metric, strong
   condition, and weak/negation condition.

Acceptance for this Word-style path:

- Preserve the long report hierarchy; do not collapse to `明天只盯三点`.
- If prior validation points are available, include the calibration block before
  `核心结论`; do not bury it at the end.
- Keep tables for repeated comparable records: index performance, emotion
  metrics, Top10 strong boards, weak boards, daily trend replay, and comparison
  rows.
- Do not cherry-pick only successful trend stocks. Pair winners with failed or
  weakening same-board samples when the data supports it.
- Style-fit and entry/stop-loss language is retrospective review only. Do not
  output direct buy/sell/target/stop instructions.
- Include exact figures only when they come from local evidence, user
  attachment, or `extra_facts[]`; otherwise write `待确认/unknown` and keep the
  section shape.
- Run the evaluator against `2026-06-29-word` when the task asks for high
  restoration. Phrase coverage must use the sample-specific phrase file, not
  the older 2026-06-05 screenshot phrases.

## Evidence Levels

Every key judgment uses one of these labels:

| Level | Meaning | Use when |
| --- | --- | --- |
| `confirmed` | Direct data supports the statement. | There is explicit price, amount, minute, limit-pool, board, or flow evidence. |
| `inferred` | Multiple data points support the statement, but one direct field is missing. | Board and stock data align, but no exact causality/flow field exists. |
| `unknown` | Data is insufficient. | Minute data, flow, catalyst, or board history is missing. |

Rules:

- Do not use涨幅 to infer fundamentals.
- Do not use long-term fundamentals to explain intraday minute moves.
- If涨停原因/catalyst is not verifiable, write `涨停原因待确认`.
- If participant flow is unavailable, do not write exact account-level主力/散户
  numbers. Order-size flow must be labeled as Eastmoney/THS 大中小单口径.
- All final actions are validation points, not buy/sell/target/stop commands.

## Full Report Template

Use this full structure for manual postmarket reviews, Obsidian-ready reviewed
notes, or deeper non-WeChat output. The daily WeChat narrative can compress the
same logic into paragraphs, but it must preserve the data boundaries.

1. `0. 结论先行`: market state, emotion temperature, main/hidden/false lines,
   biggest risk, most important next-day validation.
2. `1. 数据完整性`: source/status/impact table.
3. `2. 市场状态`: index moves, turnover, breadth, limits/failed limits, tail
   drawdown, evidence level.
4. `3. 固定半小时时间轴`: every fixed slice; mark missing slices `unknown`.
5. `4. 精确关键节点`: only when minute data supports exact timing.
6. `5. Top7 成交额板块`: cleaned board names, amount, amount ratio, breadth,
   limits, drawdown, strongest slice, status.
7. `6. 近20日趋势Top7`: liquidity-filtered trend boards and excluded
   high-gain low-liquidity boards.
8. `7. 资金流动与卡位`: battlefield, winner, loser, time slice, evidence,
   result (`有胜方`, `无胜方`, `板块内分化`, `证据不足`).
9. `8. 重点个股池`: Top50/high amount-ratio/gainers/losers/limit pools/user
   pools, with amount, alpha, drawdown, and layer.
10. `9. 线头回溯`: T-3 to T-1 score for strong boards/key stocks.
11. `10. 题材归因`: catalyst type, evidence, limit reason, sustainability,
    evidence level.
12. `11. 承接与抛压`: high-to-close drawdown, close position, pullback amount
    share, limit state, acceptance level.
13. `12. 尾盘专项`: tail failed limits, repair direction, pressure direction,
    next-day pressure.
14. `13. 持仓/模拟交易复盘`: only if supplied; judge whether the decision matched
    market structure.
15. `14. 明日验证清单`: trigger condition, negation condition, related direction.
16. `15. 200字以内摘要`: no new unsupported claims.

## Data Priority

Primary local sources:

- `/api/workbench/shell`
- `/api/pack/dashboard`
- `/api/strategy/snapshot`
- Mongo `fullmarket_spot_snapshots`
- Mongo `board_heat_ticks`
- Mongo `bars`
- Mongo `market_limit_pools`

Important fields:

- `shell.indices`: index close/change data.
- `shell.watchlist_groups.sector_boards`: Agent OS `板块15`, including chain name, driver change, phase, action, representatives.
- `shell.watchlist_groups.focus_stocks`: buy/opportunity pool.
- `shell.watchlist_groups.watch_stocks`: watch pool.
- `shell.watchlist_groups.risk_stocks`: risk/temporary non-participation pool.
- `dashboard.overview.cluster_summary`: industry/concept top list when `sector_boards` is missing.
- `dashboard.daily_brief`: trade date, primary theme, confidence, changed themes/candidates.
- `market_replay.high_turnover_cores`: all-market成交额核心.
- `market_replay.rotation_windows`: minute checkpoint top/weak board snapshots.
- `market_replay.rotation_shifts`: between-checkpoint strengthening/weakening, used to write资金迁移.
- `market_replay.board_timeline`: board-15 minute path.
- `market_replay.representative_paths`: representative 5-minute price and amount path.
- `market_replay.dynamic_market_representatives`: date-specific market core, confirmed elastic, failed emotion, and pressure representatives.
- `market_replay.failed_boards`: failed-board/high-open-fade emotion evidence.
- `market_replay.external_fund_flows`: optional Eastmoney/THS order-size flow evidence for high-turnover cores.
- `market_replay.flow_availability`: whether exact account-level主力/散户 flow is supported, and whether order-size flow is available.

See `references/data-requirements.md` for the full checklist.

## MCP

The skill can be exposed through the bundled stdio MCP server:

```bash
bash scripts/python.sh -m signals.mcp.review_assistant_server
```

MCP tools:

- `get_signals_replay_context`: fetches local Signals endpoints and returns a structured, date-agnostic data package for the agent to write from.
- `get_market_replay_context`: fetches local endpoints plus Mongo and returns the full-market event graph.
- `get_replay_analysis_framework`: returns the AI-native thinking framework for writing the replay.
- `generate_signals_replay_review`: renders a deterministic evidence preview. Do not use this tool output as the final WeChat body for recurring automations.
- `list_signals_replay_data_requirements`: returns the data checklist this skill expects.

The review/context tools accept `extra_facts[]` for screenshot, news, or exported market facts. This is the supported way to restore a specific sample without hardcoding a date.

## Comparison Discipline

When comparing generated output with a screenshot sample, use these buckets:

- Covered facts: exact index damage, high-turnover cores, board-15 names, representative stock paths.
- Partially covered facts: minute narrative can be inferred from `rotation_shifts`, but the AI must still connect cause and effect.
- Missing facts: exact account-level主力/散户买卖拆分, news/catalyst causes, and any external broker/news wording absent from local data.
- Overreach: any precise participant-flow, account-type flow, or news cause not backed by context must be labeled as missing or passed as `extra_facts[]`. Eastmoney/THS 大中小单 is order-size flow, not participant flow.
- Representative discipline: do not treat YAML `core/elastic` as the day's leader list. Use `dynamic_market_representatives.market_core`, `market_elastic_confirmed`, `failed_emotion`, and `pressure_core` for the actual replay.
- Golden text discipline: keep exact screenshot text in `signals/replay/references/` and use it only through `signals.replay.evaluate`; do not paste it into runtime generators, MCP tools, or automation prompts.

## 2026-06-05 Sample

For the exact 2026-06-05 screenshot target, load:

- `signals/replay/references/2026-06-05-screenshot.txt`
- `signals/replay/references/2026-06-05-screenshot-facts.json`

For the reusable skill guidance, load:

- `skills/signals-replay-review/references/example-2026-06-05.md`
- `skills/signals-replay-review/references/comparison-2026-06-05.md`
- `skills/signals-replay-review/references/data-requirements.md`

Do not hardcode those facts in Python, MCP tools, or daily automations. The key
correction is that board recognition is healthy: the review must explicitly
include `自动化/机器人`, `商业航天/卫星互联网`, `游戏/影视/文旅`,
`创新药/医疗器械`, `食品饮料/零售消费`, `AI应用/智能体`, and
`消费电子/裸眼3D` if they appear in `板块15`.
