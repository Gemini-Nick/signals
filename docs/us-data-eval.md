# 美股数据源评估报告

> 2026-03-04 | 隆小侠 LONG CLAW — 美股 dataflow 重新评估

## 1. 背景

当前美股数据流采用 **Futu 优先 + yfinance 兜底** 架构（`signals/data/fetcher.py` → `USDataSource`）。

Futu 的 OpenAPI 美股行情需购买 **Nasdaq Basic 行情卡**，月费 $30+（非专业用户），且仅覆盖 Nasdaq 交易所数据（不含综合行情）。相比之下，Interactive Brokers 的美股 Level 1 行情月费仅 $1.50-10/月，成本差距显著。

### 当前数据需求

| 使用场景 | 品种 | 周期 | 回溯深度 | 调用频率 |
|---------|------|------|---------|---------|
| Layer 1 指数分析 | SPY / QQQ / DIA | 日线 + 30min + 15min | 180天日线, 60天分钟线 | 9次/run（3 ETF × 3 周期） |
| Layer 3 个股筛选 | US.AAPL 等白名单 | 15min + 30min | 60天 | 2次/标的 |
| 盘后复盘 | 同上 | 日线 | 指定起始日 | 按需 |

### 当前痛点

- **Futu 月费高**：Nasdaq Basic 行情卡 $30+/月，仅为获取历史 K 线
- **需要本地网关**：FutuOpenD 需持续运行
- **额度消耗**：每次 `request_history_kline` 消耗额度，高频使用受限
- **仅 Nasdaq 交易所**：OpenAPI 不支持购买综合美股行情

---

## 2. 候选数据源总览

| 维度 | Futu OpenAPI | IB (ib_async) | Alpaca (免费) | yfinance |
|------|-------------|---------------|--------------|----------|
| **月费** | $30+（Nasdaq Basic 行情卡） | $1.50-10（非专业，按交易所） | $0 | $0 |
| **开户要求** | 富途证券账户 | IB 账户（最低 $500 入金） | 注册即可，无需入金 | 无需注册 |
| **本地网关** | FutuOpenD | TWS 或 IB Gateway | 不需要（REST API） | 不需要 |
| **日线深度** | 数年 | 数年（最大 1Y/请求） | 6-7 年 | 1 年 |
| **分钟线深度** | 8 年 | 数月（15min 最大 ~60D） | 6-7 年 | 60 天 |
| **实时性** | 实时（有行情卡） | 实时（有订阅） | 延迟 15 分钟（免费） | 延迟 ~15 分钟 |
| **频率限制** | 额度制（按月交易量分档） | Pacing（60 req/10min 历史数据） | 200 calls/min | 非官方，可能被限流/封禁 |
| **Python 库** | `futu-api` | `ib_async`（原 `ib_insync` 继承者） | `alpaca-py` | `yfinance` |
| **线程安全** | 否（单 QuoteContext） | asyncio 事件循环 | 是（REST） | 是（REST） |
| **数据覆盖** | Nasdaq 交易所（OpenAPI 限制） | NYSE + NASDAQ + ARCA 全覆盖 | IEX（免费）/ SIP（付费） | Yahoo Finance 综合 |

---

## 3. 各数据源详细分析

### 3.1 Futu OpenAPI（当前主力）

**费用明细**：
- Nasdaq Basic 行情卡（OpenAPI 专用）：$30+/月（非专业），专业用户更贵
- 行情卡仅覆盖 Nasdaq、NYSE、NYSE-American 上市的股票/ETF
- OpenAPI **不支持**购买美股综合行情（全 13 个交易所）
- 交易手续费与 APP 端一致，无额外 API 费用

**API 能力**：
- `request_history_kline()`：历史 K 线，最多 8 年分钟线，每次消耗 1 个额度
- `get_cur_kline()`：当前 K 线（需先订阅，不消耗历史额度）
- `subscribe()` + `CurKlineHandlerBase`：实时 K 线推送回调
- 支持 1M/5M/15M/30M/60M/DAY/WEEK

**接入复杂度**：中等
- 需要本地运行 FutuOpenD 网关（127.0.0.1:11111）
- 需要已开通美股行情权限
- Python SDK: `pip install futu-api`

**适配性**：
- 当前代码已完整适配，`US.XXX` 格式为原生 Futu 代码
- `FutuSource` 类已实现全部方法（fetcher.py:218-420）

**优势**：深度历史数据（8 年分钟线）、实时推送能力
**劣势**：月费高、需要网关、额度限制、OpenAPI 行情覆盖面受限

---

### 3.2 Interactive Brokers — ib_async（推荐替代）

**费用明细**：
- 美股 Level 1 行情订阅（非专业用户）：
  - NYSE (Network A/CTA)：~$1.50/月
  - NASDAQ (Network C/UTP)：~$1.50/月
  - NYSE American + BATS + ARCA + IEX (Network B)：~$1.50/月
  - **US Securities Snapshot Bundle**（打包）：$10/月以内
- 最低账户余额：$500（保持活跃，不含订阅费）
- Snapshot 快照：$0.01/请求（每月 $1 免费额度），超额自动升级为订阅
- 无额外 API 使用费

**API 能力**：
- `reqHistoricalData()`：历史 K 线
  - bar size: 1 sec ~ 1 month（支持 1min/5min/15min/30min/1hour/1day）
  - 日线最大回溯 1 年/请求，分钟线 ~60 天/请求
  - 支持 `keepUpToDate=True` 实时更新最新 bar
- `reqMktData()`：实时行情
- `reqRealTimeBars()`：5 秒实时 bar
- 支持 `useRTH=True/False` 切换盘中/盘前盘后数据

**Pacing 限制**：
- 相同合约+周期：15 秒内不能重复请求
- 全局限制：10 分钟内最多 60 个历史数据请求
- 当前需求 9 次/run（3 ETF × 3 周期），远低于限制

**接入复杂度**：中等（与 Futu 类似）
- 需要本地运行 TWS 或 IB Gateway
  - TWS：完整交易终端，端口 7496(live)/7497(paper)
  - IB Gateway：轻量无界面，端口 4001(live)/4002(paper)，推荐
- Python SDK: `pip install ib_async`（原 `ib_insync` 的继任项目，原作者 2024 年离世后社区维护）

**适配性**：
- SPY/QQQ/DIA 在 IB 中为 ETF，使用 `Stock(symbol, 'SMART', 'USD')` 合约
- 代码转换：`US.SPY` → `Stock('SPY', 'SMART', 'USD')`
- 返回 `BarData` 对象，含 `.date/.open/.high/.low/.close/.volume`
- 时区：返回交易所本地时间（US/Eastern），需 `tz_localize(None)` 去时区
- asyncio 架构，需要 `ib.connect()` 或 `ib.connectAsync()`，同步调用可用 `ib_async.util.run()`

**优势**：费用极低（$1.50-10/月）、全交易所覆盖、数据质量高、同时支持交易
**劣势**：需要 IB 账户和本地网关、分钟线回溯不如 Futu（月级 vs 年级）、asyncio 接入稍复杂

---

### 3.3 Alpaca（零成本方案）

**费用明细**：
- Basic plan：完全免费
- Algo Trader Plus：$99/月（全 SIP 实时数据）
- 注册即可获取 API Key，无需入金

**API 能力**：
- `StockHistoricalDataClient.get_stock_bars()`：历史 K 线
  - 支持 1min/5min/15min/30min/1hour/1day
  - 历史深度 6-7 年（包括分钟线）
  - 每请求最多 1000 条，支持分页
- WebSocket 实时推送（免费仅限 IEX 交易所，最多 30 个同时 symbol）

**免费方案限制**：
- **历史数据延迟 15 分钟**：最新可用数据 = 当前时间 - 15 分钟
- 实时行情仅 IEX 交易所（约占美股交易量 2-3%）
- 200 API 调用/分钟（burst: 10 req/sec）
- WebSocket 最多 30 个 symbol

**接入复杂度**：低
- 纯 REST API，无需本地网关
- Python SDK: `pip install alpaca-py`
- 仅需 API Key + Secret Key

**适配性**：
- 代码转换：`US.SPY` → `"SPY"`
- 返回 pandas DataFrame，含 open/high/low/close/volume/trade_count/vwap
- 时区：UTC 时间戳，需转换
- REST 调用，线程安全，可并发请求

**优势**：零成本、无需网关、分钟线深度 6-7 年（优于 yfinance 和 IB）、稳定官方 API
**劣势**：免费版 15 分钟延迟（盘中监测受影响）、IEX 实时数据覆盖面窄

---

### 3.4 yfinance（当前兜底）

**费用明细**：完全免费，无需注册

**API 能力**：
- `Ticker.history(period, interval)`：历史 K 线
  - 日线：最多 1 年（`period="1y"`）
  - 分钟线：1min 7 天，5min/15min/30min/60min 60 天
- 无实时推送能力

**接入复杂度**：最低
- `pip install yfinance`，无需 API Key 或网关

**适配性**：
- 已在 `YFinanceSource` 类中完整实现（fetcher.py:425-481）
- `US.AAPL` → `yf.Ticker("AAPL")`

**优势**：零成本、零配置、代码已就绪
**劣势**：分钟线仅 60 天、日线仅 1 年、非官方 API 可能被限流/封禁、无实时数据

---

## 4. 场景匹配分析

### 4.1 盘中监测（`--mode intraday`）

需要**近实时**数据（分钟级延迟可接受，15 分钟延迟不可接受）。

| 数据源 | 适用性 | 说明 |
|--------|--------|------|
| Futu | ✅ 完全适用 | 实时推送，但月费高 |
| IB | ✅ 完全适用 | 实时数据，月费低 |
| Alpaca (免费) | ⚠️ 受限 | 15 分钟延迟，盘中信号滞后 |
| yfinance | ⚠️ 受限 | 延迟约 15 分钟，可能被限流 |

**结论**：盘中场景需要 IB 或 Futu，Alpaca 免费版不够。

### 4.2 盘后复盘（`--mode review`）

延迟可接受，关注历史深度和可靠性。

| 数据源 | 适用性 | 说明 |
|--------|--------|------|
| Futu | ✅ | 8 年分钟线历史 |
| IB | ✅ | 足够的历史深度 |
| Alpaca (免费) | ✅ | 6-7 年历史，延迟不影响 |
| yfinance | ⚠️ | 分钟线仅 60 天，日线仅 1 年 |

**结论**：除 yfinance 外都够用；Alpaca 在此场景性价比最高。

### 4.3 指数分析（Layer 1）

3 只 ETF × 3 周期 = **9 次历史 K 线请求/run**，频率极低。

| 数据源 | 适用性 | 说明 |
|--------|--------|------|
| 全部 | ✅ | 9 次请求远低于所有数据源的限制 |

**结论**：任何数据源都能满足 Layer 1。

### 4.4 个股筛选（Layer 3）

批量拉取白名单 + 行业股票的 15min/30min bars。当前美股标的较少（白名单为主），但可能扩展。

| 数据源 | 适用性 | 说明 |
|--------|--------|------|
| Futu | ✅ | 但额度消耗快 |
| IB | ✅ | 60 req/10min 限制，需注意 pacing |
| Alpaca | ✅ | 200 calls/min，可批量请求多 symbol |
| yfinance | ⚠️ | 非官方，大批量易被限流 |

**结论**：Alpaca 批量能力最强，IB 需注意 pacing 但 10+ 只标的完全没问题。

---

## 5. 推荐方案

### 方案 A：IB 优先 + yfinance 兜底（推荐 ⭐）

```
USDataSource(primary=IBSource, fallback=YFinanceSource)
```

| 项目 | 说明 |
|------|------|
| 月费 | $1.50-10（US 行情订阅） |
| 适用场景 | 盘中 + 盘后 + 指数 + 个股，全覆盖 |
| 前置条件 | IB 账户 + IB Gateway 本地运行 |
| 接入难度 | 中（与 Futu 类似的网关模式） |
| 数据质量 | 高（官方交易所数据，全覆盖） |

**适合**：已有或计划开设 IB 账户的用户。成本最低的全功能方案。

### 方案 B：Alpaca 优先 + yfinance 兜底（零成本）

```
USDataSource(primary=AlpacaSource, fallback=YFinanceSource)
```

| 项目 | 说明 |
|------|------|
| 月费 | $0 |
| 适用场景 | 盘后复盘 + 指数分析 ✅ / 盘中监测 ⚠️（15 分钟延迟） |
| 前置条件 | 注册 Alpaca 账户（免费，无需入金） |
| 接入难度 | 低（纯 REST API，无需网关） |
| 数据质量 | 中等（IEX 实时，历史数据完整） |

**适合**：以盘后分析为主，不常用盘中监测的用户。分钟线历史深度 6-7 年反而优于 IB。

### 方案 C：纯 yfinance（最简方案）

```
USDataSource(primary=None, fallback=YFinanceSource)
```

| 项目 | 说明 |
|------|------|
| 月费 | $0 |
| 适用场景 | 指数日线分析 ✅ / 分钟线分析 ⚠️（60 天限制） |
| 前置条件 | 无 |
| 接入难度 | 零（已实现） |
| 数据质量 | 低（非官方 API，可能不稳定） |

**适合**：仅做初步判断，不需要深度分钟线历史。当前 Futu 不可用时的实际运行状态。

### 方案 D：IB 盘中 + Alpaca 盘后（混合最优）

```
盘中模式: USDataSource(primary=IBSource, fallback=YFinanceSource)
盘后模式: USDataSource(primary=AlpacaSource, fallback=YFinanceSource)
```

| 项目 | 说明 |
|------|------|
| 月费 | $1.50-10 |
| 适用场景 | 全覆盖，且 Alpaca 6-7 年分钟线弥补 IB 深度不足 |
| 接入难度 | 较高（两套数据源配置） |

**适合**：追求最优数据覆盖的场景，但增加了复杂度。

---

## 6. 实现影响评估

无论选择哪个方案，代码改动范围一致：

| 文件 | 改动 |
|------|------|
| `signals/data/fetcher.py` | 新增 `IBSource` 和/或 `AlpacaSource` 类 + 重构 `USDataSource` 构造函数 + 新增 `create_us_source()` 工厂函数 |
| `config.py` | 新增 `US_DATA_SOURCE` / `IB_HOST` / `IB_PORT` 等配置项 |
| `.env.example` | 新增对应环境变量模板 |
| `signals/layers/index_screener.py` | `_load_us_indices()` 改用 `create_us_source()` |
| `signals/layers/screener.py` | `_fetch_minute_bars()` 改用 `create_us_source()` |
| `signals/layers/review_screener.py` | 改用 `create_us_source()` |
| `requirements.txt` | 按需添加 `ib_async` / `alpaca-py` |

现有 `FutuSource` 和 `YFinanceSource` **无需修改**，完全向后兼容。

---

## 7. 参考链接

- [IB 行情订阅定价](https://www.interactivebrokers.com/en/pricing/market-data-pricing.php)
- [IB API 历史数据文档](https://interactivebrokers.github.io/tws-api/historical_bars.html)
- [ib_async GitHub（ib_insync 继任）](https://github.com/ib-api-reloaded/ib_async)
- [Futu OpenAPI 费用说明](https://openapi.futunn.com/futu-api-doc/en/intro/fee.html)
- [Futu OpenAPI 权限与限制](https://openapi.futunn.com/futu-api-doc/en/intro/authority.html)
- [Alpaca 市场数据 API](https://docs.alpaca.markets/docs/about-market-data-api)
- [Alpaca 历史 Bars API](https://docs.alpaca.markets/reference/stockbars)
- [yfinance GitHub](https://github.com/ranaroussi/yfinance)
