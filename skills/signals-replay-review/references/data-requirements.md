# Data Requirements

## Must Have

- Trade date: `dashboard.daily_brief.as_of`.
- Index damage: `shell.indices[]` for 上证指数, 深证成指, 创业板指, 科创50.
- Sector ranking: `shell.watchlist_groups.sector_boards[]`; preserve order from Agent OS `板块15`.
- Sector evidence per row: `name`, `day_change_pct`, `trader_action`, `source_driver`, `primary_domain`, `candidate_groups`, `representatives`.
- Pool structure: counts and top rows from `focus_stocks`, `watch_stocks`, `risk_stocks`, and `buy_candidates`.
- Candidate reason: `entry_logic_summary`, `trader_action`, `invalidates_when`, `primary_chain`, `queue_lane`.
- Cluster fallback: `dashboard.overview.cluster_summary.industry_top` and `concept_top`.
- Full-market daily result: Mongo `fullmarket_spot_snapshots` for open/high/low/close/change/amount.
- Minute board strength: Mongo `board_heat_ticks` for checkpoint top boards, weak boards, and between-checkpoint deltas.
- Representative paths: Mongo `bars` for 5-minute low/high/large-amount bars.
- Board constituents: Mongo `board_constituents` for converting a hot industry/concept into candidate stocks.

## Structured Daily Review Additions

The fused skill should preserve the screenshot-style narrative while also
checking these structured-review fields. Missing fields stay `missing` or
`unknown`; never fill them from prose.

### Data completeness status

Required status rows:

- Index daily data.
- Index minute data.
- Stock daily data.
- Stock minute data.
- Board daily data.
- Board minute data.
- Board 20-day history.
- Board 20-day amount history.
- Limit-up/limit-down/failed-limit pools.
- Order-size flow.
- Participant/account-level flow.
- News/catalyst facts.
- User holdings/simulation records.

Each status row should carry `status`, `source`, and `impact`.

### Fixed stock samples

- Full-market amount Top50.
- Amount-ratio Top20: today amount divided by past 5-day average, default
  minimum ratio `1.5`.
- Gainers Top20 and losers Top20 after excluding ST/delisting/suspended names
  and names with amount below `2亿`.
- Limit-up, limit-down, failed-limit, and strong-pool names from
  `market_limit_pools` or AkShare/Eastmoney pools.
- User watch/holding/simulation names when supplied.

Recommended stock fields:

- `symbol`, `name`, `board`, `change_pct`, `amount`, `amount_rank`,
  `amount_ratio_5d`, `turnover_pct`, `alpha_vs_board`, `intraday_drawdown`,
  `close_position`, `layer`, `evidence_level`.

### Fixed board samples

- Cleaned Top7 boards by turnover/amount. Exclude region boards, broad indices,
  style indices, tag baskets, financing/SH-HK/SZ-HK/MSCI/Fund-heavy labels,
  ST/delisting/yesterday-limit labels, and administrative-region-only labels.
- Liquidity-filtered 20-day trend Top7 boards. This is not the same as today's
  main line; it is the trend comparison layer.
- High-gain boards excluded by liquidity filters, with the exclusion reason.

Recommended Top7 board fields:

- `name`, `type`, `change_pct`, `amount`, `amount_rank`, `amount_ratio_5d`,
  `breadth_pct`, `limit_up_count`, `intraday_drawdown`, `strongest_slice`,
  `status`, `evidence_level`.

Recommended 20-day trend board fields:

- `name`, `type`, `return_20d`, `avg_amount_20d`, `amount_percentile_20d`,
  `avg_amount_5d`, `today_amount`, `return_5d`, `max_drawdown_20d`,
  `overlaps_today_top7`, `trend_status`, `evidence_level`.

Default liquidity filters:

- `avg_amount_20d >= max(30亿, P40 of cleaned candidate boards)`.
- `avg_amount_5d >= 20亿`.
- `today_amount >= 20亿`.
- `tradable_member_count >= 10`.
- `constituent_amount_coverage >= 60%` when estimating from constituents.

If fewer than 7 boards pass, lower the percentile threshold from P40 to P30.
If still fewer than 7 pass, output only the passing boards.

### Fixed intraday slices

The full postmarket report should cover these slices:

- `09:15-09:25` auction, when available.
- `09:30-10:00`.
- `10:00-10:30`.
- `10:30-11:00`.
- `11:00-11:30`.
- `13:00-13:30`.
- `13:30-14:00`.
- `14:00-14:30`.
- `14:30-15:00`.

Recommended slice fields:

- `slice`, `market_behavior`, `strongest_board`, `weakest_board`,
  `active_fund_direction`, `drained_direction`, `slice_change_pct`,
  `slice_amount`, `slice_amount_share`, `high_to_end_drawdown`,
  `low_to_end_rebound`, `rank_change`, `evidence_level`.

Only output exact minute nodes when minute data supports them.

### Classification fields

Board status:

- `主线`, `受伤主线`, `暗线`, `轮动`, `分歧`, `出货`, `伪主线`, `unknown`.

Trend status:

- `强趋势延续`, `趋势高潮`, `趋势钝化`, `高位回撤`, `与当日主线共振`,
  `低流动性剔除`, `unknown`.

Carding result:

- `有胜方`, `无胜方`, `板块内分化`, `证据不足`.

Acceptance level:

- `强承接`, `一般承接`, `弱承接`, `无承接`, `天量无承接`, `unknown`.

Evidence level:

- `confirmed`: directly supported by price/amount/minute/limit/board/flow data.
- `inferred`: supported by multiple data points but missing one direct field.
- `unknown`: key data is missing.

## Nice To Have

- Intraday event lines from `fetch_market_event_lines()` for 9:45, 11:15, 13:45, and close windows.
- AkShare/Eastmoney limit pools: `stock_zt_pool_em`, `stock_zt_pool_zbgc_em`, `stock_zt_pool_dtgc_em`, `stock_zt_pool_strong_em`.
- Mongo `market_limit_pools`: minute-sampled normalized output from `signals.sync.modules.market_limit_pools.sync_market_limit_pools`; preferred source for limit-up speed/quality fields. Keep `snapshot_at` and `snapshot_minute` so intraday封板/炸板 changes can be replayed.
- Order-size flow: optional Eastmoney/THS 大单/中单/小单 buy-sell/net fields for high-turnover cores. This can support资金承接 analysis, but it is not account-level主力/散户.
- Participant flow: exact主力/散户/机构 buy-sell/net from a fund-flow or L2 source. Without this, do not output precise account-type flow figures.
- THS fund flow via AkShare: `stock_fund_flow_industry(symbol="即时")`, `stock_fund_flow_concept(symbol="即时")`.
- Eastmoney individual fund flow via AkShare: `stock_individual_fund_flow(stock="300308", market="sz")`; historical endpoint exposes net fields only.
- Eastmoney order-size flow via `signals.replay.fund_flow_sources.fetch_stock_fund_flow_evidence()`: prefer `push2delay.eastmoney.com/api/qt/ulist.np/get` because it exposes super-large, large, medium, and small order buy/sell/net fields; fall back to quote `stock/get` when the page endpoint is unreachable.
- THS realFunds via `signals.replay.fund_flow_sources.fetch_ths_real_funds()`: parses current quote-day 大/中/小单流入流出 from `/spService/{code}/Funds/realFunds`.
- News/catalyst causes: trusted market news, user screenshots, or exported market data when local Signals does not store them.
- Leader names from sector rows: `data_truth.primary_domain.leader_name`, `source_driver.name`, `cluster_summary.*.leader`.
- Market position line: `shell.market.position_suggestion`, `recommended_style`, `overall_direction`.
- MCP context package from `get_market_replay_context`, which should be treated as the reusable all-market data API for the skill.
- MCP framework package from `get_replay_analysis_framework`, which should be treated as the reusable AI reasoning contract.
- `extra_facts[]` in MCP or `--extra-fact` in CLI for externally verified screenshot/news facts that are not present in local Signals endpoints.

## Global Visual Additions

These groups extend the visual edition without enlarging the A-share short report.

- Keep a versioned fixed-plus-dynamic universe. Fixed anchors include HSI/HSCEI/HSTECH, major Hong Kong internet
  names, A+H technology pairs, US broad/technology/small-cap index anchors, VIX, Mag7, and representatives from the
  existing cross-market AI hardware chain.
- Hong Kong may use validated full-market daily bars for breadth; its core indices and anchors use minute bars only
  when a reliable formal source is available.
- US breadth, turnover, MA21, highs/lows, volatility, and drawdown are explicitly `core_universe` unless a
  consolidated full-market feed is present. IEX coverage must not be described as the US whole market.
- Every market snapshot carries `session_date`, `as_of`, `timezone`, `currency`, `session_state`,
  `coverage_scope`, and `universe_id`.
- Reject non-finite prices, invalid OHLC relationships, negative volume/amount, duplicate bars, and future session
  dates before a snapshot can be formal.
- HKD, USD, and CNY turnover remain separate. Price and turnover participation do not prove net inflow.
- Do not create Hong Kong or US limit-up, failed-limit, limit-down, or consecutive-limit metrics.

## Local Coverage Notes

For 2026-06-05, local sources were enough for:

- Index moves from `/api/workbench/shell`: 上证 `-0.7403%`, 深成指 `-2.2148%`, 创业板 `-3.2023%`, 科创50 `-4.0119%`.
- Full-market daily rows from `fullmarket_spot_snapshots`: about 5.8k names with OHLC/change/amount/turnover.
- Board/minute rotation from `board_heat_ticks`: checkpoint and between-checkpoint强弱切换.
- Chain heat from `chain_heat_snapshots`.
- Limit/failed-limit/strong-pool quality from `market_limit_pools`.
- Index 5-minute paths from `index_bars`.

Local `bars` is partial for individual stocks. If a stock lacks local 5-minute
bars, the review may use daily OHLC only, or online backfill when explicitly
enabled and source-tagged.

No local Mongo collection currently contains `flow` or `fund`; participant
flow must therefore return unavailable unless an external verified L2/account
source or `extra_facts[]` supplies it. Public Eastmoney/THS order-size flow can
be exposed under `external_fund_flows`, but it must not flip
`participant_flow_available` to true.

## AkShare / External Coverage

Usable for replay enrichment:

- `stock_zt_pool_em(date)`: limit-up pool with first/last封板,炸板次数,封板资金,连板.
- `stock_zt_pool_zbgc_em(date)`: failed-limit pool.
- `stock_zt_pool_dtgc_em(date)`: limit-down pool.
- `stock_zt_pool_strong_em(date)`: strong pool with量比 and reason.
- `stock_fund_flow_industry("即时")`: industry total inflow/outflow/net.
- `stock_fund_flow_concept("即时")`: concept total inflow/outflow/net.
- `stock_fund_flow_individual("即时")`: stock total inflow/outflow/net/amount.
- Eastmoney `/api/qt/ulist.np/get` page numeric fields:
  - `f6`: amount.
  - `f62`: main-order net.
  - `f64/f65/f66`: super-large-order buy/sell/net.
  - `f70/f71/f72`: large-order buy/sell/net.
  - `f76/f77/f78`: medium-order buy/sell/net.
  - `f82/f83/f84`: small-order buy/sell/net.
  - `f124`: quote update timestamp.
- Eastmoney `/api/qt/stock/get` quote fallback fields:
  - `f135/f136/f137`: main-order buy/sell/net.
  - `f138/f139/f140`: super-large-order buy/sell/net.
  - `f141/f142/f143`: large-order buy/sell/net.
  - `f144/f145/f146`: medium-order buy/sell/net.
  - `f149`: small-order net only in the observed payload.
- THS `/spService/{code}/Funds/realFunds`: current quote-day 大/中/小单流入流出 and total流入/流出/净额.
- Project direct Sina/Tencent minute fetchers when AkShare's Sina/Eastmoney minute routes fail.

Unstable on this machine in the current run:

- Eastmoney historical individual fund flow and rank APIs backed by `push2his`.
- Eastmoney realtime `push2` quote/page endpoints can disconnect after repeated requests; `push2delay` was reachable for `ulist.np/get` in the current run. Keep THS fallback and source-tag the result.
- AkShare Eastmoney minute history routes.
- AkShare Sina minute route due SSL EOF.

Even when industry/concept/stock total fund flow or 大中小单 order-size flow is
available, it is not a verified main/retail participant split.

## AI Context Shape

`get_market_replay_context` returns two layers:

- `signals_context`: API-derived market, board-15, and pool context.
- `market_replay`: Mongo-derived full-market event graph.
- `market_replay.structured_daily_review`: structured v2.1 evidence package
  for data completeness, fixed half-hour slices, fixed stock pools, Top7 board
  proxy, 20-day trend availability, and acceptance/pressure tables.

Minimum data contract for future MCP/API evolution:

- `indices`: index changes, OHLC, 5-minute path, and source.
- `market_turnover`: full-market amount aggregation and high-turnover cores.
- `market_breadth`: up/down/flat counts, limit-up/down counts, failed-limit
  counts, tail failed-limit counts, and emotion temperature inputs.
- `sector_rotation`: checkpoint top/weak boards and rotation shifts.
- `sector_boards`: Agent OS `板块15` order and source-driver metadata.
- `sector_top7_by_amount`: cleaned Top7 amount boards with status fields.
- `sector_trend_top7_20d`: liquidity-filtered 20-day trend Top7.
- `intraday_slices`: fixed half-hour timeline with evidence levels.
- `stock_paths`: daily OHLC/amount plus local or online 5-minute path with `coverage_status`.
- `stock_sample_pool`: Top50 amount, amount-ratio Top20, gainers/losers,
  limit/failed-limit pools, and user pools.
- `stock_alpha_layers`: leader/strong/synchronous/weak/laggard/risk-release
  classification versus board change.
- `structured_daily_review.fixed_time_slices`: always returns the fixed
  09:15-15:00 slice grid; missing slice data is `unknown`.
- `structured_daily_review.key_stock_pool`: Top50 amount, amount-ratio proxy,
  gainers/losers, and limit-pool sample.
- `structured_daily_review.top_turnover_boards`: cleaned Top7 board proxy; if
  board amount is unavailable, label it as partial/inferred instead of strict
  成交额Top7.
- `structured_daily_review.trend_20d_boards`: 20-day trend Top7 only when
  board daily history and liquidity filters are available; otherwise output
  missing/unknown.
- `structured_daily_review.acceptance_pressure`: close-position and
  high-to-close drawdown based承接/抛压 tables.
- `limit_events`: limit-up, failed-limit, limit-down, and strong-pool evidence with `snapshot_minute`.
- `flow`: split into `board_total_flow`, `stock_total_flow`, `order_size_flow`, and `participant_flow`; the last one must be `unavailable` when unsupported.
- `evidence`: every field should carry source/status; missing fields stay missing instead of falling back to sample prose.

The AI should write from these fields:

- `market_replay.analysis_framework.thinking_process`
- `market_replay.high_turnover_cores`
- `market_replay.rotation_windows`
- `market_replay.rotation_shifts`
- `market_replay.board_timeline`
- `market_replay.representative_paths`
- `market_replay.dynamic_market_representatives`
- `market_replay.failed_boards`
- `market_replay.external_fund_flows`
- `market_replay.flow_availability`
- `market_replay.structured_daily_review`

Codex automations must not write market analysis from raw API snippets alone.
They must first run the local window gate command, then call
`signals.mcp.review_assistant_server` for `get_market_replay_context`, then
synthesize from this skill and the returned evidence package. The deterministic
`--format wechat` output is a gate/fallback, not the preferred AI-native
analysis body.

The limit-pool sync is deliberately a sampled evidence source. A postmarket
single pull can restore the final涨停/炸板 pool, but cannot reconstruct which
names entered or lost recognition at 9:33, 10:30, or 13:30. For screenshot-style
复盘, run it through the realtime `workbench_lane` during the session.

## Interpretation Boundaries

- `rotation_windows` tells who is strong/weak at each minute checkpoint.
- `rotation_shifts` tells which boards gained or lost strength between checkpoints; this is the closest local proxy for资金迁移.
- `high_turnover_cores` tells where the largest成交额承接 pressure sits.
- `representative_paths` verifies whether a board's leader/core names moved with the board.
- `dynamic_market_representatives` separates knowledge-base representatives from the day's market-recognized representatives.
- `market_limit_pools.snapshot_minute` tells when the limit-pool evidence was observed; use the latest snapshot for end-of-day representative scoring and compare earlier snapshots for event timing.
- `market_elastic_confirmed` is for confirmed弹性; `failed_emotion` is for炸板/冲高回落情绪. Do not mix those two lists.
- `failed_boards` is tail-emotion evidence, not direct proof of account-level资金流.
- If `flow_availability.participant_flow_available` is false, precise account-level主力/散户买卖拆分 must be omitted or supplied as `extra_facts[]`.
- If only `flow_availability.order_size_flow_available` is true, write it as Eastmoney/THS order-size flow and keep the口径 note visible.

## Dynamic Representative Rules

Static representatives from `industry_chains.yaml` are only the industry map:

- `core`: long-term chain owner or common institutional representative.
- `elastic`: long-term chain beta name from the knowledge base.

They are not necessarily the day's leader or elastic trade.

Dynamic representatives should be selected from board constituents plus static reps:

- `market_core`: high成交额, high换手, market consensus, or high-volume pressure center.
- `market_elastic`: early/fast涨停, higher连板数, higher封板资金, fewer炸板次数, strong涨幅, high换手/量比.
- `market_elastic_confirmed`: the subset of `market_elastic` with confirmed or inferred封板 quality; use this when writing leader/focus language.
- `failed_emotion`: failed-limit or high-to-close drawdown names; use this for尾盘情绪, not for主线确认.
- `pressure_core`: high成交额 negative feedback, high-to-close drawdown, or failed recovery.

Recommended generic scores:

- `board_score = 25*close_strength + 20*intraday_delta + 15*rank_persistence + 15*breadth + 15*limit_pool_quality_density + 10*representative_path_resonance - negative_feedback_penalty`
- `core_score = 35*liquidity + 15*turnover_volume_ratio + 15*board_resonance + 15*acceptance_or_pressure_role + 10*change_role + 10*pool_state`
- `elastic_score = 20*pool_state + 15*first_limit_speed + 10*consecutive_or_seal_quality + 15*change_or_high_change + 10*turnover_volume_ratio + 10*amount_floor + 15*board_resonance + 5*minute_path_confirmation - failed_or_drawdown_penalty`

Normalize涨幅 role by limit band: 10cm, 20cm, and 30cm names cannot be ranked
with raw涨幅 alone.

AkShare field coverage:

- `stock_zt_pool_em(date)`: `首次封板时间`, `最后封板时间`, `炸板次数`, `封板资金`, `涨停统计`, `连板数`, `所属行业`.
- `stock_zt_pool_zbgc_em(date)`: `首次封板时间`, `炸板次数`, `涨停统计`, `振幅`, `所属行业`.
- `stock_zt_pool_dtgc_em(date)`: `封单资金`, `最后封板时间`, `板上成交额`, `连续跌停`, `开板次数`, `所属行业`.
- `stock_zt_pool_strong_em(date)`: `量比`, `入选理由`, `涨停统计`, `所属行业`.

## Automated Output Checks

- Automated gate output must start with `NOTIFY` or `DONT_NOTIFY`. A manual
  operational response may report the original gate reason and data date outside
  the replay body; the saved replay itself always uses the same reader-facing
  structure as automation.
- Do not output runtime, Mongo, cache, or lane-health boilerplate.
- Do not turn validation points into direct buy/sell instructions.
- Do not let `daily_brief.primary_theme` hide a broader `板块15` structure.
- If data is simulated from a screenshot, say it is a simulation/replay and keep the live-source facts separate.
- Do not hardcode date-specific screenshot facts in runtime code; keep them in references or user-provided extra facts.
- Do not let AI turn board ranking into a simple涨幅榜; require a minute-level event chain.
