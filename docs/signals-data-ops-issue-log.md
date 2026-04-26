# Signals Data Ops Issue Log

Last updated: 2026-04-25

## Debug Frame

- 假设：Mongo 进程存在，但常驻服务口径不符合当前 Longclaw/WeGuard 服务体系；Signals sync 进程仍在跑旧代码或存在 stale-running module，导致部分交易缓存没有持续更新。
- 复现：直接检查 launchd、weguard、Mongo 集合、sync_log meta，不依赖旧日志。
- 最小改动：先修服务常驻和调度状态，再补齐最新交易日关键集合，最后处理 Web1/Web2 融合残留。
- 验证：`launchctl print`、`weguard status`、Mongo count/latest_dt、gateway read-only probe、Electron/Signals UI 冒烟。

## P0

### 1. MongoDB 没有纳入 Longclaw/WeGuard 服务体系

- Evidence: `mongod` 当前通过 `gui/501/homebrew.mxcl.mongodb-community` 运行，PID 526。
- Evidence: `launchctl print system/homebrew.mxcl.mongodb-community` 不存在；`gui/501/com.zhangqilong.ai.mongodb` 不存在。
- Evidence: `weguard status` 只列出 `signals-sync`，没有 MongoDB 服务项。
- Current: 已切换到 `gui/501/com.zhangqilong.ai.mongodb`，`weguard status` 显示 `mongodb running`，旧 `homebrew.mxcl.mongodb-community` label 已卸载。
- Impact: Mongo 虽然在跑，但不符合 WeClaw bridge / Claude / Codex / Signals sync 这套统一 watchdog 口径。用户看到的是“数据库没有系统常驻保障”。
- Target: 新增 Longclaw 管理的 MongoDB service label，并纳入 `~/.weclaw/services.json` 监控；保留 Homebrew service 或迁移到统一 label 二选一，避免双实例抢端口。
- Solution: 已启用 `com.zhangqilong.ai.mongodb` launchd plist、`mongodb-service.sh` wrapper，并已加入 `~/.weclaw/services.json`。
- Verification: `launchctl print gui/$(id -u)/com.zhangqilong.ai.mongodb` running，PID 72794；`~/.longclaw/runtime-v2/bin/weguard status` 显示 `mongodb running`；`127.0.0.1:27017` 由 PID 72794 监听；`db.runCommand({ping:1})` 返回 ok。
- Status: fixed-and-running。
- Activation steps:
  ```bash
  launchctl bootout gui/$(id -u) /Users/zhangqilong/Library/LaunchAgents/homebrew.mxcl.mongodb-community.plist
  launchctl bootstrap gui/$(id -u) /Users/zhangqilong/Library/LaunchAgents/com.zhangqilong.ai.mongodb.plist
  launchctl kickstart -k gui/$(id -u)/com.zhangqilong.ai.mongodb
  ~/.longclaw/runtime-v2/bin/weguard status
  ```

### 2. `stock_daily` 长时间停留在 `running`

- Evidence: `sync_log.stock_daily:_meta.status=running`，`last_run=2026-04-24 16:37:02`，没有 `elapsed_seconds/error_msg`。
- Impact: daemon 旧逻辑会把当天 running 当作“已跑过”，导致日线补数不会自动恢复；白名单 `SH.601958` 没有 bars，L3 复盘无数据。
- Target: stale-running 超过阈值自动可重跑；当前 daemon 需要重启以加载新代码；失败必须写 `error/degraded` 而不是静默 running。
- Solution: sync engine 启动时释放超过 2 小时的 `running` module，标记 `degraded/stale_running_timeout`；`stock_daily` 默认改为活跃池补数，只有显式 `STOCK_DAILY_SCOPE=all` 或 `SIGNALS_SYNC_FULL_STOCK_DAILY=true` 才跑全市场。
- Verification: 前台跑 `market_pools` 时已释放 1 个 stale-running module；`stock_daily` 现在可补活跃池，当前 active pool `bars_covered=50/50`。
- Status: code-patched, verified-partial。

### 3. `signals-sync` 常驻进程仍是旧进程

- Evidence: `signals-sync` launchd PID 547 从旧代码启动，当前 repo 已改 sync engine/storage/modules，但未 kickstart。
- Impact: 新的 `signal_pool/market_pools/quote_snapshots` 和 degraded 判定不会在 daemon 内生效。
- Target: 部署后 kickstart `com.zhangqilong.ai.signals.sync`，再验证新模块进入 `ALL_MODULES` 并写 freshness。
- Solution: 代码侧已让 daemon 启动执行 storage model、stale-running cleanup、bootstrap preheat；部署侧需要 kickstart 现有 launchd label。
- Verification: 已 `kickstart -k`，`signals-sync` PID 从 547 变为 72803；日志显示 `2026-04-25 02:59:46 同步守护进程启动`。
- Status: fixed-and-running。
- Latest: 接入实时 quote provider 后再次 kickstart，`signals-sync` PID 变为 75166，WeGuard 显示 running。

## P1

### 4. 最新交易日口径本身是 2026-04-24，但交易输入不完整

- Evidence: 当前日期 2026-04-25 是周六；`get_last_trading_day()` 返回 `2026-04-24`。
- Evidence: `bars/index_bars/board_ranking/concept_ranking/quote_snapshots/signals` 均有 2026-04-24 记录或 freshness。
- Gap: `bars` 只有少量股票/指数；活跃池 50 个标的里多数没有对应 bars/quote。
- Target: latest_dt 要同时看日期和覆盖率；健康检查必须报告 active_pool coverage。
- Solution: `stock_daily` 默认补活跃池，避免全市场长跑阻塞；后续 health 需要增加 active pool bars/quote coverage 百分比。
- Verification: Mongo 当前 `bars=29558`、`index_bars=3103`、`quote_snapshots=50`、`market_pools.active=50`、`signals=384`；active pool `bars_covered=50/50`、`quote_covered=50/50`。
- Status: patched-and-preheated。

### 5. `quote_snapshots` 只是 bars_latest stale 快照，不是真实盘中实时输入

- Evidence: `quote_snapshots` count=11，gateway 返回 `freshness=stale`，`source=bars_latest`。
- Evidence: 直连实测 `push2delay.eastmoney.com/api/qt/stock/get` 可返回个股实时 quote；`push2his.eastmoney.com/api/qt/stock/kline/get` 仍出现 SSL record layer failure / bad decrypt。
- Impact: 盘中预警/cluster 可以不阻塞，但不能认为已有实时行情链路。
- Target: sync/backfill 中接入实时 quote provider，只覆盖 `market_pools`，runtime 继续只读 snapshot。
- Solution: `quote_snapshots` sync 已接入东财 `push2delay.stock.get` 直连实时接口；每个 active-pool 标的优先写 `eastmoney_push2delay/fresh`，单标失败才回落 `bars_latest/stale`。Runtime 仍只读 Mongo。
- Verification: 前台 sync 50 个 active-pool 标的全部 live，`quote_snapshots=50`，source 全部为 `eastmoney_push2delay`；`data_freshness.quote=fresh`，`live_count=50/stale_count=0`；gateway `get_quote_snapshot(realtime)` 返回 fresh。
- Status: fixed-and-preheated。

### 6. `signal_pool` 目前只是 SQLite 迁移，不是新的候选/预警生成器

- Evidence: Mongo `signals=371` 来自 `.data/backtest.db.signal_records`；`signal_pool` sync 只做 upsert migration。
- Impact: 候选/预警有历史池，但没有基于 `bars/index_bars/market_pools` 的每日生成逻辑。
- Target: 增加基于最新 bars 和 active pool 的 candidate/warning 生成 job。
- Solution: `signal_pool` sync 已增加本地生成逻辑：只读 `market_pools + bars`，按趋势增强/跌幅扩大/跌破 MA20 生成 `candidate/warning`，用 `dedupe_key` 幂等写 `signals`。
- Verification: 前台 sync 生成 13 条本地候选/预警，Mongo `signals=384`，latest `2026-04-24`。
- Status: patched-and-preheated。

### 7. `index_daily` A 股已写入，US 指数写入失败

- Evidence: `index_bars=1600`，A 股 8 个指数写入；`US.SPY/QQQ/DIA` 报 `float() argument must be a string or a real number, not 'Series'`。
- Impact: L1 美股指数缓存不完整。
- Target: 修 yfinance MultiIndex/Series 兼容，US 指数写 `index_bars`。
- Solution: `_sync_us_index()` 已压平 yfinance MultiIndex，并对 row 值做 scalar 提取。
- Verification: 前台 `index_daily` 写入 A 股 8 个指数 + `US.SPY/US.QQQ/US.DIA`，`index_bars=3103`，latest `2026-04-24`。
- Status: patched-and-preheated。

### 8. Web1 runtime 曾自动启动旧分析引擎并直连 provider

- Evidence: Electron 冒烟时 Web1 startup 调用 `engine.run_all_async()`，触发 Futu、IB、yfinance、概念成分股外部请求。
- Current change: 已改为 `SIGNALS_WEB_AUTOSTART_ENGINE=true` 才启动。
- Target: 重新跑 Web1/Electron 验证默认启动不再拉 provider。
- Solution: 保持默认不启动旧 engine；Web1 API 已改走 `signals.services.*`。
- Status: code-patched, needs-regression。

## P2

### 9. `data_freshness` domain 命名存在重复和旧状态

- Evidence: 同一 collection 有 `quote` 与 `quote_snapshots`、`market_pool` 与 `market_pools` 两套 freshness 文档；旧 `quote` 文档曾显示 `empty`。
- Impact: 健康面板容易误报。
- Target: 统一 domain 命名并清理旧 freshness 文档。
- Solution: sync engine freshness 按业务 domain 写入：`quote/market_pool/signal/index/kline/board/concept`；storage startup 清理旧 module-name duplicate 文档。
- Verification: 当前关键 freshness 行为 `kline/index/market_pool/quote/signal`，旧 `quote_snapshots/market_pools/signal_pool` duplicate 已由 storage cleanup 移除。
- Status: patched-and-preheated。

### 10. `/api/health/cache` 对 `signals` 的 latest_dt 取值不完整

- Evidence: `signals` 没有 `dt`，只有 `signal_date/updated_at`，health 里 latest_dt 为空。
- Impact: 面板显示“没有最新日期”，但实际 signals 到 2026-04-24。
- Target: health cache 对不同集合使用 `dt/signal_date/latest_dt/updated_at` 的统一解析。
- Solution: `/api/health/cache` 已统一解析 `dt/latest_dt/signal_date/snapshot_at/updated_at`，并带出 `freshness/stale_reason`。
- Status: code-patched。

### 11. Web2 兼容层仍存在，但 Web1 直接 import 已清理

- Evidence: `signals.web2` 仍作为兼容 router 存在；Web1 API 已改调 `signals.services.*`。
- Impact: 短期可接受；后续需要把 web2 implementation 继续下沉到 service，最后仅留 thin router。
- Target: 下一轮 Web1/Web2 融合继续拆内部实现。
