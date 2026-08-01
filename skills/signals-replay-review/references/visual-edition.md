# Visual edition and quantitative appendix

Read this only for a requested long quantitative appendix or `visual` output.

Reliable optional groups are: single-day breadth and return quantiles; MA21 breadth; monthly/quarterly/half-year
new highs versus new lows; total turnover and Top 20/50/100 concentration; overnight/intraday/pre-14:30/closing
returns; up to eight broad indices over 1/3/20 days and versus MA21; volatility and drawdown; and up to twenty
capacity stocks in the appendix.

Omit an incomplete group. Never fill missing values with zero. Turnover and concentration are not net inflow.
The appendix explains the five-section conclusion; it cannot replace or reverse it.

Choose the medium by the trading question:

- prose explains the causal judgment, counterevidence, and why the next watch changed;
- a compact table aligns exact values, units, scopes, event states, and scenario conditions;
- a chart shows a time path or cross-sectional relationship that prose cannot make visible;
- a K-line shows price location and completed price-volume structure, never participant identity or intent.

Signals should normally prioritize two chart families: a board relative-strength event graph and, when definitions are
stable, a limit/failed-limit ecology graph. Use at most four comparable board series and annotate only timestamps that
change the daily judgment. Do not turn a final ranking into a reconstructed intraday path. Do not stack pool counts
when pools overlap.

Representative paths require timestamped values and a declared comparison baseline. Do not render an independently
autoscaled, untimed sparkline as a price path. A K-line is optional and requires complete OHLCV, timezone, frequency,
adjustment method, trading-session status, missing-data state, and evidence references. Omit the chart when these
conditions fail.

Event-driven forward views use an event timeline plus a scenario table. The table separates official event time,
market expectation, new information, expected A-share observation, and invalidator. Show numeric probabilities only
with a frozen reference class, sample size, and out-of-sample calibration; otherwise label scenarios as primary,
alternative, or tail. Future candles and unverified participant arrows are prohibited.

Render the same payload with:

```bash
python3 /Users/zhangqilong/.workbuddy/replay-suite/bin/render_replay_visual.py \
  --input /path/to/replay-visual.json \
  --output /path/to/A股盘后可视复盘_YYYY-MM-DD_Signals原生.html
```

Contract:

`/Users/zhangqilong/.workbuddy/replay-suite/contracts/replay-visual-payload.schema.json`

The page is short first screen, long-form chart drill-down, then a collapsed appendix. A missing formal close changes
the page to `A股午后观察`. Missing minute data removes sparklines. Incomparable industry turnover must not control
arrow or Sankey width.

For `schema_version: "2.0"`, render independent `markets[]` panels:

- `A` remains the primary full-market panel and retains the existing quantitative groups.
- `HK` uses HSI, HSCEI, HSTECH, market breadth when the full daily universe is fresh, internet anchors, A+H pairs,
  and optional core-minute session structure. It never uses the A-share limit-pool vocabulary.
- `US` uses the latest completed US session, five core index families, VIX, Mag7, AI-chain representatives, and
  optional first/last-hour structure. Breadth and turnover are labelled as core-universe metrics unless a
  consolidated feed supports a broader claim.
- Display each market's session date, timezone, currency, and coverage scope. Never add CNY, HKD, and USD turnover.
- `cross_market_links[]` may express relative strength, divergence, and representative-pair moves. It may not infer
  customer relationships, account identity, or net fund flow.

Reject known reconstruction traps: conflicting close versus intraday exact returns, swapped Top-table columns,
new-listing returns rendered as zero, truncated "weakest" industry labels, or turnover described as net inflow.
