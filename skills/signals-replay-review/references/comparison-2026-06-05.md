# 2026-06-05 Screenshot Comparison

Use this as the acceptance comparison for the June 5 replay simulation. It is
not runtime code and must not be copied into Python branches.

## Current Code Output vs Screenshot

2026-06-06 verification command:

```bash
bash scripts/python.sh -m signals.notify.trading_workbench_summary \
  --window postmarket --format narrative --ignore-time \
  --eval-target 2026-06-05-screenshot \
  --min-similarity 1.0 --require-eval-phrases
```

Current data-driven result after the sample-style causal-chain pass,
sector-alias opening paragraph, and order-size fund-flow enrichment:

- `char_similarity`: `0.498616`
- `target_chars`: `2347`
- `generated_chars`: `2350`
- `target_paragraphs`: `14`
- `generated_paragraphs`: `14`
- `phrase_coverage`: `14 / 15`
- `send`: blocked; first output line is `DONT_NOTIFY` and the block reason is `[replay-eval] send blocked`

This means the local data graph is useful, but the generated text is not yet
converged to the screenshot target. The current pass improved the narrative by
turning the previous board-rank listing into sample-style causal paragraphs:
opening optical pressure and retail attack, commercial-aerospace hidden-line
handling, 9:33 power negative feedback, 10:30 high-turnover pressure, afternoon
robot spread, 13:30 collapse, tail failed-board emotion, carding structure,
emotion temperature, and time-cycle paragraphs. It also expands candidates from
rotation board constituents, preserves previous close/open/high/low/close in
stock event chains, aggregates multi-pool limit data, and separates confirmed
elastic names from failed-limit emotion names. The latest pass also adds
intraday-delta board candidate expansion so non-front-rank trial directions can
enter the stock-event candidate set, and it shortens the opening market
paragraph so it is less like a workbench board list. The latest pass also
adds optional `external_fund_flows`: Eastmoney `ulist.np/get` page fields can
expose super-large, large, medium, and small order buy/sell/net when reachable,
with quote `stock/get` retained as a fallback; THS `realFunds` can expose
current quote-day 大/中/小单流入流出. In the verified CLI run, Eastmoney was
reachable and supplied the current flow line:
`东财订单资金口径显示，主力全天买入419亿卖出483亿，主力净流出63.81亿；中单/散户代理买入155亿卖出91.6亿，净接63.8亿`.
Sina's free quote page was also probed: the page labels include 主力/散户
placeholders, but the reachable `MoneyFlow.ssi_ssfx_flzjtj` payload covered
only about `423.755` 亿 at `14:57:00`, so it is an incomplete diagnostic source
and not the screenshot's full-day account-flow source.
It also fixes the carding fallback numbering so a missing lithium event no
longer produces “三次卡位” followed by “第四次”; the generated section now writes
“第三次是午后机器人铺开但高位压力仍在” for the current data.

The remaining missing golden phrase is still the screenshot's exact participant
or broker口径 line: `主力全天买入426亿卖出490亿`. Local Mongo has no
participant-flow collection. Eastmoney `ulist.np/get` probing separately showed
a close but non-identical order-size口径 for 中际旭创 on 2026-06-05:
main-order buy/sell/net about `419/483/-64` 亿 and medium+small proxy about
`155/92/+64` 亿. THS realFunds gives a different 大/中/小单 distribution. These
sources support the “高成交核心被大单卖出、中单承接” interpretation, but they do
not justify printing the screenshot's exact `426/490` and `157/93` numbers in
daily output. The exact 2026-06-05 sample can still only pass through the
structured training-sample renderer; that renderer is now `DONT_NOTIFY` by
default and is a training artifact, not the daily postmarket automation source.

Paragraph-level latest scores:

- Opening market structure: `0.719346`.
- Opening fund-flow chain: `0.531008`.
- Commercial aerospace hidden line: `0.293860`.
- 9:33 turn: `0.373159`.
- 10:30 turn: `0.493671`.
- Afternoon robot spread: `0.488889`.
- 13:30 collapse/fund-flow paragraph: `0.734177`.
- Carding structure: `0.760000`.

The low-scoring paragraphs are not style-only failures. They point to data
gaps: concept/news membership for 神剑股份/信维通信/再升科技/航天发展,
full minute paths for more representatives, lithium trial-move evidence, and
the exact account-level fund-flow source.

| Dimension | Screenshot sample | Current code/MCP output | Gap | Generic fix |
| --- | --- | --- | --- | --- |
| Market damage | Starts with tail-session wipeout, 上证 -0.74%, 深成指 -2.21%, 创业板 -3.20%, 科创50 -4.01%, 中际旭创 583 亿 as the real pressure center. | Covers the four index drops and 中际旭创 583.25 亿; also surfaces 京东方A、新易盛、兆易创新、亨通光电 as high-turnover cores. | Covered on facts, but AI must decide which high-turnover cores are negative feedback and which are unrelated liquidity centers. | Use `high_turnover_cores` plus `representative_paths` to classify high-turnover names into pressure center, counter-direction liquidity, and neutral成交. |
| Opening chain | Screenshot writes 光模块 low-open pressure, then consumer attack through 步步高/茂业商业/东百集团. | Code now writes a causal opening chain from `opening_pressure_boards`, high-turnover CPO/通信 events, and retail limit/failed-limit events: 中际旭创/新易盛/天孚通信 plus 步步高/茂业商业/东百集团. | Mostly covered. Wording differs because the generator uses available previous close and live OHLC rather than copied screenshot prose. | Keep using derived event rows; add only externally verified phrasing through `extra_facts[]`. |
| Hidden commercial aerospace line | Screenshot identifies 商业航天 as a hidden line and compares 神剑股份、信维通信、再升科技、航天发展 timing. | Code covers 商业航天/卫星互联网 in `board_timeline` and representative 5-minute paths when those reps are in board-15. | Partial. Timing is available, but “hidden line” is an interpretation layer. | AI should compare board delta, leader continuity, and rep path synchronization before calling it hidden-line strength. |
| 9:33 / 10:30 turns | Screenshot uses precise turns: 9:33 天孚通信 pull + 电力崩；10:30 中际旭创 1301.51 fail + 锂电试盘. | Code writes 9:33 and 10:30 causal paragraphs from stock event chains and wider rotation deltas. It now captures CPO/通信 pressure and electric-power negative feedback, and the 10:30 中际旭创 high-turnover failure. | Partial. 锂电试盘 can still be missed if the lithium rotation does not enter the selected candidate set. | Keep the wider rotation candidate scan and add board-resonance scoring so lithium trial moves surface when it is a meaningful turn. |
| Afternoon robot strength | Screenshot says 机器人被资金平铺买入，但缺少旗帜性涨停焦点. | Code covers 机器人 from early weakness to tail strength and now uses `market_elastic_confirmed` rather than failed-limit names. It selected 绿的谐波 as the high-recognition confirmed focus for the current data. | Structurally covered, but sample wording differs because the local data sees a confirmed high-quality focus. | Score flagship quality by成交额, market, limit speed, board resonance, and 10/20/30cm normalization before saying “有/没有旗帜性焦点”. |
| Core/elastic representatives | Screenshot-style reasoning chooses the day's market-recognized names, not just long-term industry representatives. CPO core is 易中天 as consensus/high-turnover core; elastic names depend on涨停速度,封板质量,涨幅,换手,成交额. | Code now separates `static_representatives` from `dynamic_market_representatives.market_core/market_elastic/market_elastic_confirmed/failed_emotion/pressure_core`, and consumes sampled AkShare/Eastmoney `market_limit_pools` for封板速度/炸板次数/封板资金/连板. | Improved structurally. Still needs board-resonance scoring, market-limit normalization, and concept/news membership for cross-theme names such as 神剑股份. | Use `market_elastic_confirmed` for true弹性确认 and `failed_emotion` for炸板情绪. Treat YAML reps only as candidate seed/industry map. |
| 13:30 collapse | Screenshot says 中际旭创 low-point pull failed, 583 亿 no price acceptance; issue is no price acceptance, not no volume. | Code has 中际旭创 daily OHLC/amount and 5-minute path in `representative_paths`; generated text states high成交 core is承接锚. | Mostly covered. AI must explicitly state “有量但无价格承接”. | Use high-turnover `large_amount_bars`, close vs high/low, and index/board shifts after 13:30. |
| Tail emotion | Screenshot compares 茂业商业/东百集团 failed-board structures and separates 中百集团超跌修复. | Code returns `failed_boards` from OHLC/high-change and uses corrected口径: 涨幅回吐 pct + 价格较高点回落 %. | Partial. It detects failed structures but does not know why 中百集团 differs unless extra context is present. | AI should write detected failed boards and mark missing structural distinction unless local facts or `extra_facts[]` support it. |
| Funds: 主力/散户 | Screenshot gives 主力净流出约64亿、散户净接约64亿, with buy/sell split `426/490` and `157/93`. | Local Mongo currently has no participant flow collection; `external_fund_flows` can expose Eastmoney/THS order-size flow. Verified fallback output used THS 大中小单 flow; Eastmoney `ulist.np/get` probing gave close but not exact `419/483` main-order and `155/92` medium+small proxy. | Net-flow direction is explainable, exact screenshot buy/sell split remains unsupported. | Do not invent exact account-type numbers. Use order-size口径 with source label, add a true L2/participant source, or pass verified sample facts via `extra_facts[]`. |
| News/catalyst | Screenshot style implies event understanding but mostly relies on market behavior. | Code does not depend on news; local available collections are board heat, chain heat, bars, fullmarket snapshots, social heat. | Missing for catalyst attribution. | Use news only as optional evidence; the core replay remains price/amount/board-strength based. |
| Final validation | Screenshot ends with next-day competition between 商业航天、机器人、消费 and high-volume core repair. | Code output already generates validation points around board-15 continuation, high-turnover core digestion, source-strong chain-weak confirmation, and 5m/15m/30m trigger completion. | Covered but more generic than screenshot. | AI should specialize validation points from the strongest board timelines and pressure centers in the event graph. |

## What The Code Should Hand To AI

The right handoff is not a finished short report. It is:

- `signals_context`: indices, board-15, pool counts, candidates, risk/watch/focus rows.
- `market_replay.high_turnover_cores`: all-market成交额核心 and OHLC pressure.
- `market_replay.rotation_windows`: minute checkpoint top/weak boards.
- `market_replay.rotation_shifts`: between-checkpoint strengthening/weakening, the proxy for资金迁移.
- `market_replay.board_timeline`: board-15 minute path.
- `market_replay.representative_paths`: 5-minute paths for board reps and high-turnover cores.
- `market_replay.dynamic_market_representatives`: static reps vs market core, confirmed market elastic, failed emotion, pressure core.
- `market_replay.failed_boards`: tail-emotion failed structures.
- `market_replay.external_fund_flows`: optional Eastmoney/THS order-size flow evidence with explicit口径.
- `market_replay.flow_availability`: hard boundary for participant-flow claims.
- `market_replay.analysis_framework`: reusable thinking process and comparison dimensions.

## Generic Replay Reasoning Template

1. Identify the pressure center first, not the strongest board.
2. Divide each time window into strengthening boards, weakening boards, and high-turnover pressure.
3. Connect windows into a fund-flow chain: opening attack, first failed pullback, midmorning trial, afternoon carding, tail-session acceptance or failure.
4. Judge whether each strong board is confirmed chain strength, source-strong chain-weak, chain-internal split, or temporary carding.
5. Use representative paths to verify the board story; if paths disagree, downgrade the board.
6. Use failed-board structures to judge tail emotion and breadth.
7. Write next-day validation as falsifiable checks, not recommendations.

## Ranking Notes From Subagent Review

The durable representative rule is:

- `market_core`: liquidity and consensus first. Score成交额/换手/量比, board resonance, price acceptance or pressure role, and pool state.
- `market_elastic_confirmed`: confirmed limit/near-limit names. Score pool state, first-limit speed, consecutive boards, seal amount, open count,涨幅/high涨幅, turnover/volume ratio, board resonance, and normalize by 10cm/20cm/30cm market limit.
- `failed_emotion`: failed-limit and high-to-close drawdown names. These are emotion evidence, not confirmed弹性.
- `pressure_core`: high-turnover negative feedback and failed recovery names.

Do not use static `core/elastic` as the day's leader list. Static reps are only
industry map seeds.
