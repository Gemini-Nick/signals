# 三层分析体系：指数研判 → 行业选择 → 标的筛选

## Context

用户希望按缠论"自上而下"思维框架重构整个筛选流程：

```
Layer 1  指数研判  →  判断大市方向和结构，明确参与/回避的板块
Layer 2  行业选择  →  在指数方向允许的前提下，找出共振最强的行业
Layer 3  标的筛选  →  在选定行业内，按缠论买卖点评分选标的
```

**已完成的基础**（V1 signals/ 管道）：Layer 3 已全部实现，Layer 2 行业成分股接口已存在但未联动上层。

**本次新增**：Layer 1（指数分析）+ 三层之间的联动逻辑 + **双模式架构**。

---

## 系统双模式架构

整个系统由用户在启动时选择运行模式：

```
python run.py --mode intraday     # 盘中监测模式（默认）
python run.py --mode review --start 2024-09-24   # 盘后复盘模式
```

| 维度 | 盘中监测模式 | 盘后复盘模式 |
|---|---|---|
| **触发** | 交易时段定时循环 | 一次性分析，用户指定起始日期 |
| **目标** | 发现当下存在买卖信号的标的 | 研究指定时间段内的完整历史结构 |
| **Layer 1 指数** | 滚动近 N 日日线（`lookback_days`） | 从指定关键转折点加载完整历史 |
| **Layer 3 个股** | AKShare 分钟线（自然限5日） | 日线 from `start_date`，或分钟历史 |
| **输出** | 实时信号排行榜，按评分排序 | 结构报告 + 历史信号时间线 |

### 时间周期维度设计

#### 优先级体系（缠论三级联动标准）

```
日线（大级别）  →  判断整体趋势方向，是上涨/下跌/震荡
   ↓
30分钟（中级别）→  识别中枢位置，判断趋势段
   ↓
15分钟（小级别）→  精确买卖点，入场/离场时机
```

> **核心原则**：大级别决定方向，小级别找买卖点。没有日线背景的15min信号是危险信号。

#### 各时间维度数据量（AKShare，5个交易日）

| 周期 | K线数/5天 | 估算笔数 | 信噪比 | 适用场景 |
|---|---|---|---|---|
| **1分钟** | ~1200 | 60-100 | ⚠️ 低（噪声多）| 超短线，不推荐本系统 |
| **5分钟** | ~240 | 20-40 | 中等 | 辅助确认，可选 |
| **15分钟** | ~80 | 8-15 | ✅ 高 | **核心（精确买卖点）** |
| **30分钟** | ~40 | 4-8 | ✅ 高 | **核心（中枢识别）** |
| 日线（指数/Layer1）| ~120（近180日）| 15-25 | ✅ 极高 | **核心（趋势方向）** |

#### 加入1min/5min维度对系统的影响

**数据源影响（A股，AKShare `stock_zh_a_minute`）：**

| 维度组合 | API调用/只 | 白名单20只 | 行业50只 | 备注 |
|---|---|---|---|---|
| 15min + 30min（当前） | 2 次 | ~1 min | ~5 min | 基准线 |
| **+ 日线（盘中）** | **3 次** | **~1.5 min** | **~7 min** | **推荐，完整三级缠论** |
| + 5min | 4 次 | ~2 min | ~10 min | 可选，增加超短线信号 |
| + 1min | 5 次 | ~2.5 min | ~12.5 min | **不推荐**，信噪比差 |

> 每增加一个分钟周期 ≈ 多 10s 网络请求（AKShare），并发池不变则耗时线性增加。

**CZSC实例数变化（内存/CPU影响，可忽略）：**

| 维度 | 实例数/只 | 每实例内存估算 | 20只总内存 |
|---|---|---|---|
| 15min+30min | 2 | ~0.5MB | ~20MB |
| +日线 | 3 | ~0.8MB | ~48MB |
| +5min | 4 | ~0.6MB | ~48MB |
| +1min | 5 | ~1.5MB（K线多）| ~150MB |

**信号体系变化（噪声分析）：**

| 维度 | 新增信号类型 | 风险 |
|---|---|---|
| 加日线 | 三级共振加分，过滤逆势 | 低，只增加过滤层 |
| 加5min | 15min买点的入场确认 | 低，辅助确认 |
| 加1min | 大量短暂交叉/背驰 | ⚠️ 高，假信号增多，评分系统需重新标定权重 |

#### 指数分钟线数据源（重要发现）

实测：**A股指数可复用 `stock_zh_a_minute`**，无需单独接口：

```python
# 完全支持指数代码（sh前缀格式）
ak.stock_zh_a_minute(symbol='sh000016', period='15')  # 上证50 15min → 1970根
ak.stock_zh_a_minute(symbol='sh000300', period='30')  # 沪深300 30min → 正常
```

`index_zh_a_hist_min_em`（东方财富）存在但超时，不可靠，**无需使用**。

**恒生科技 15min/30min（需 Futu）**：

`stock_zh_a_minute` 不支持HK代码。需Futu订阅：
- `subscribe('HK.800700', [SubType.K_15M, SubType.K_30M])` → **订阅额度 +2**
- 后续用 `get_cur_kline()` 取数据，不再消耗历史K线额度
- 优于每次用 `request_history_kline` (历史额度)

#### 最终推荐维度方案

**盘中系统（三级联动，完整缠论）**：

| 层级 | 维度 | 数据来源 |
|---|---|---|
| Layer 1 指数 | **日线 + 30min + 15min** | AKShare `stock_zh_a_minute`（A股） / Futu（恒科）|
| Layer 3 个股 | **日线 + 30min + 15min** | AKShare 日线 `lookback_days=90` + 分钟线 5日 |

**5min（可选开关）**：在 config 中提供 `ENABLE_5MIN = False`，需要超短线时打开，不影响主逻辑。

**1min**：不纳入本系统，留给专门的超短线系统。

#### 更新后的 Futu 配额（含指数15/30min）

| 操作 | 历史K线额度/run | 订阅额度（持久） |
|---|---|---|
| HK.800700 日线 (request_history_kline) | 1 | 0 |
| HK.800700 周线 (request_history_kline) | 1 | 0 |
| HK.800700 15min (subscribe+get_cur_kline) | 0 | **1** |
| HK.800700 30min (subscribe+get_cur_kline) | 0 | **1** |
| **合计** | **2/run** | **2（全天复用）** |

---

### 数据源验证结论（实测）

**A股指数（7只）**：

| 函数 | 数据源 | 状态 | 数据量 |
|---|---|---|---|
| `ak.stock_zh_index_daily(symbol)` | 腾讯 | ✅ 全部可用 | 5000+ 日线 |
| `ak.index_zh_a_hist()` | 东方财富 | ❌ 超时 | - |

symbol 格式：上海 `sh000016`，深圳 `sz399006`

**恒生科技（Futu OpenD 实测）**：

| 代码 | 名称 | 状态 | 数据量 |
|---|---|---|---|
| `HK.800700` | 恒生科技指数 | ✅ 日线 532根，周线 293根 | 2020-2026 |
| `HK.HSI` / `HK.HSCEI` | 恒生/国企 | ❌ ret=-1 | - |

Futu 返回列：`time_key, open, close, high, low, volume(=0), turnover, change_rate`

---

## Futu API 配额规划

### 配额体系说明

Futu OpenD 有**两个独立配额池**，互不干扰：

| 配额类型 | 触发接口 | 特性 | 当前状态 |
|---|---|---|---|
| **订阅额度**（Subscription） | `subscribe(codes, sub_types)` | 累计活跃订阅数，`unsubscribe` 后释放 | 1000 上限，当前≈0 |
| **历史K线额度**（History KLine） | `request_history_kline()` | 每次调用 -1，**按天重置** | 1000/天，今日已用 **6** |

> `get_cur_kline()` 不直接消耗历史K线额度，但需要**先 subscribe** 才能调用，因此消耗订阅额度。

---

### 各数据需求的配额消耗分析

| 数据需求 | 数据源 | 接口 | 历史K线额度/次 | 订阅额度 |
|---|---|---|---|---|
| **A股7只指数 日线** | AKShare `stock_zh_index_daily` | 免费 | 0 | 0 |
| **A股7只指数 周线** | pandas resample（日线合成） | 无网络 | 0 | 0 |
| **恒生科技 日线** | Futu `request_history_kline` | 历史接口 | **1/run** | 0 |
| **恒生科技 周线** | Futu `request_history_kline` | 历史接口 | **1/run** | 0 |
| **Layer 3 个股分钟线（盘中）** | AKShare `stock_zh_a_minute` | 免费，限5日 | 0 | 0 |
| **Layer 3 个股日线（盘后复盘）** | AKShare `stock_zh_a_hist` | 免费 | 0 | 0 |
| _（未来）个股实时分钟线_ | _Futu subscribe+get_cur_kline_ | _实时推送_ | _0_ | _2/只（15+30min）_ |

**结论**：A股全部走 AKShare（免费无限制）；Futu 仅用于**恒生科技**（AKShare超时，无法替代）。

---

### 每日配额预算

**盘中模式**（每天开盘前初始化一次即可，日线数据不需要盘中重复拉）：

| 操作 | 历史K线额度 | 订阅额度 |
|---|---|---|
| 加载 HK.800700 日线 × 1 | **1** | 0 |
| 加载 HK.800700 周线 × 1 | **1** | 0 |
| 小计 | **2 / 1000** | 0 |

**盘后复盘模式**（每次分析消耗同上，一次 2 calls）：

| 场景 | 历史K线额度 |
|---|---|
| 指数复盘（仅HK.800700） | 2 |
| 如日后添加HK个股复盘（每只） | +1 per stock |

**最坏情况估算**：盘中运行 5 次/天 + 盘后复盘 3 次 = **16 calls/1000**，远低于上限。

---

### 订阅额度扩展规划（未来 Phase 4）

当升级到 Futu 实时流推送时，订阅额度会有明显消耗：

| 场景 | 订阅数 | 占用额度 |
|---|---|---|
| 白名单 20 只 × 2 级别（15+30min） | 40 | 40 |
| 行业批扫 50 只 × 2（临时订阅，扫完释放） | 100 | 100（临时） |
| 指数 8 只 × 日线推送 | 8 | 8 |
| **合计（含临时峰值）** | | **≤148** / 1000 |

> 即使开启实时推送，剩余 850+ 订阅额度，不存在超限风险。

---

**最终数据源分工（含三级时间维度）**：

| 数据 | 维度 | 数据源 | 接口 |
|---|---|---|---|
| A股7只指数 | 日线 | AKShare | `stock_zh_index_daily(symbol)` |
| A股7只指数 | 周线 | pandas resample（日线合成）| 无额外请求 |
| A股7只指数 | 15min / 30min | AKShare | `stock_zh_a_minute(symbol='sh...', period='15/30')` |
| 恒生科技 | 日线 / 周线 | Futu `request_history_kline` | 历史K线额度 2/run |
| 恒生科技 | 15min / 30min | Futu subscribe + `get_cur_kline` | 订阅额度 2（全天复用）|
| A股个股（盘中）| 日线 | AKShare | `stock_zh_a_hist(lookback=90天)` |
| A股个股（盘中）| 15min / 30min | AKShare | `stock_zh_a_minute`（5日）|
| A股个股（盘后）| 日线 from start_date | AKShare | `stock_zh_a_hist` |

---

# ════════════════════════════════════════
# Layer 2：行业强度研判（扩展）
# ════════════════════════════════════════

## 现状与问题

当前 `industry.py` 只有：
- `get_industry_list()` → 获取行业名称列表
- `get_industry_stocks(industry)` → 获取行业成分股

**缺失**：行业本身的强弱研判——没有识别"哪些板块强势"的能力。

## 数据源现状（实测）

| 接口 | 用途 | 状态 |
|---|---|---|
| `stock_board_industry_hist_em(symbol, period='daily')` | 行业板块日线K线（东财）| ⚠️ 东财SSL间歇性超时 |
| `stock_board_industry_fund_flow_rank()` | 行业资金流向排名 | ❌ 当前版本无此函数 |
| `stock_board_industry_name_em()` | 行业列表（东财）| ⚠️ 同上，间歇性超时 |

**结论**：东财行业接口不可作为主力数据源；需要主数据源 + 降级方案。

## 行业配置机制：config 默认 + 命令行覆盖

**原则**：`config.py` 存储常用关注列表（随时可改），命令行参数可临时覆盖不修改文件。

### config.py 默认配置

```python
# config.py
# 默认关注板块：平时重点观察的行业（随时修改这里即可）
WATCH_INDUSTRIES = ["有色金属", "半导体"]
```

### 命令行临时覆盖（不需要改配置文件）

```bash
# 使用 config 默认板块
python run.py --mode intraday

# 临时换板块（今天看这两个，不影响 config.py）
python run.py --mode intraday --industries 新能源车,医药生物

# 全市场自动识别强势板块（较慢，盘后用）
python run.py --mode review --auto-scan-industry

# 不分析行业，只跑指数+白名单
python run.py --mode intraday --industries ""
```

**实现方式**（`run.py` 参数解析）：

```python
parser.add_argument("--industries", default=None,
    help="覆盖config中的WATCH_INDUSTRIES，逗号分隔。传空字符串=跳过Layer2")
# 解析逻辑：
industries = args.industries.split(",") if args.industries is not None \
             else config.WATCH_INDUSTRIES
```

### 自动扫描强势板块（盘后模式可选）

扫描所有行业（~100个），按强度排名，取 Top N。仅在 `--mode review --auto-scan-industry` 时触发，不影响盘中性能。

## 行业强度研判方法（两级降级）

### 方法 A：行业板块 CZSC（最优，东财可用时）

```python
def get_industry_bars(industry: str, lookback_days: int = 180) -> List[RawBar]:
    """从 stock_board_industry_hist_em 获取行业日线 K 线"""
    df = ak.stock_board_industry_hist_em(
        symbol=industry, period='daily',
        start_date=(today - timedelta(days=lookback_days)).strftime('%Y%m%d'),
        end_date=today.strftime('%Y%m%d'),
        adjust='qfq'
    )
    # → 列名映射 → _to_raw_bars → RawBar List
```

- 直接复用 `IndexAnalyzer` 对行业K线做 CZSC 分析
- 输出：行业 `IndexReport`（趋势方向、买卖点、中枢位置）
- **降级**：东财超时 → 自动切换到方法 B

### 方法 B：成分股聚合评分（降级方案，始终可用）

```python
def score_industry_by_members(industry: str, freqs: List[Freq]) -> IndustryScore:
    """
    获取行业成分股 → 对每只股票跑 Layer 3 → 取平均分
    无需行业板块K线，完全依赖已有基础设施
    """
    stocks = get_industry_stocks(industry)   # 已有
    scores = [score_symbol(s, freqs) for s in stocks[:20]]  # 抽样前20只
    return IndustryScore(
        name=industry,
        avg_score=mean([s.total_score for s in scores]),
        buy_ratio=len([s for s in scores if s.total_score > 0]) / len(scores),
        top_stocks=sorted(scores, key=lambda x: -x.total_score)[:5]
    )
```

- 每个行业：20只成分股 × 2-3个级别 × ~10s = **约5分钟/行业**
- 适合用户指定少数行业（2-3个，共10-15分钟）

### 方法 C：Layer 1 指数推断（最快，无需行业数据）

利用 `MarketContext` 的指数强弱自动推断：

```python
def infer_strong_sectors(ctx: MarketContext) -> List[str]:
    """根据指数结构推断强势板块方向"""
    # 创业板/科创50 强 → 科技、新能源、医药生物
    # 上证50/300 强   → 金融、地产、消费
    # 中证500/1000 强 → 中小盘制造、材料
    ...
```

## 新增文件/改动

| 文件 | 操作 | 内容 |
|---|---|---|
| `config.py` | 修改 | 新增 `WATCH_INDUSTRIES = []` |
| `signals/industry.py` | 修改 | 新增 `get_industry_bars()`, `score_industry_by_members()`, `IndustryScore` |
| `signals/market_context.py` | 修改 | 新增 `infer_strong_sectors()` 辅助函数 |

## 推荐工作流（盘中模式 Layer 2）

```
Layer 1 MarketContext
    ↓
gate_industry_scan = True?
    ↓ Yes
1. 若 WATCH_INDUSTRIES 非空 → 分析指定板块
   - 优先方法 A（东财行业K线 → CZSC）
   - 降级方法 B（成分股聚合）
2. 若 WATCH_INDUSTRIES 为空 → 跳过 Layer 2，直接用 Layer 1 推断结果
    ↓
IndustryScore 列表（按平均分排序）→ 选 Top 1-2 行业进入 Layer 3
```

---

# ════════════════════════════════════════
# Layer 1：指数研判（新增）
# ════════════════════════════════════════

## 新增文件结构

```
Signals/
├── run.py                       # 新增：系统入口，--mode intraday|review
├── config.py                    # 修改：新增 INDEX_CODES, INDEX_LOOKBACK_DAYS
├── monitor/
│   └── data_fetcher.py          # 修改：新增 get_index_daily(), get_index_kline()
└── signals/
    ├── index_report.py          # 新增：IndexReport 数据类
    ├── index_analyzer.py        # 新增：IndexAnalyzer（日线+周线 CZSC）
    ├── market_context.py        # 新增：MarketContext（8指数聚合）
    ├── index_screener.py        # 新增：IndexScreener（两模式：盘中/盘后）
    ├── review_screener.py       # 新增：ReviewScreener 盘后复盘入口
    │   ── (以下 Layer 3 已存在) ──
    ├── analyzer.py              # 已有
    ├── detectors.py             # 已有
    ├── scorer.py                # 已有
    ├── screener.py              # 已有（后续与 MarketContext 联动）
    ├── industry.py              # 已有
    └── validate.py              # 已有
```

## Step 1: config.py — 新增指数配置

```python
# A股指数（AKShare格式）
INDEX_AK_CODES = {
    "上证50":   "sh000016",
    "沪深300":  "sh000300",
    "创业板指": "sz399006",
    "科创50":   "sh000688",
    "超大盘":   "sh000043",
    "中证500":  "sh000905",
    "中证1000": "sh000852",
}
# HK指数（Futu格式）
INDEX_FUTU_CODES = {
    "恒生科技": "HK.800700",
}
# 合并，供 IndexScreener 使用
INDEX_CODES = {**INDEX_AK_CODES, **{k: v for k, v in INDEX_FUTU_CODES.items()}}
INDEX_FREQS = ["daily", "weekly"]

# 指数日线滚动窗口（盘中模式）：近N自然日
# 180自然日 ≈ 120交易日，足够建立CZSC日线结构
INDEX_LOOKBACK_DAYS = 180
```

## Step 2: data_fetcher.py — 两处新增

**2a. AKShareSource 新增 `get_index_daily()`**

```python
def get_index_daily(self, symbol: str,
                    lookback_days: int = 180,
                    start_date: str = None) -> List[RawBar]:
    """
    A股指数日线（盘中模式：滚动窗口；盘后复盘：传 start_date 固定起点）。
    symbol: AKShare 格式，如 'sh000016'
    lookback_days: 近N自然日（盘中默认180天≈120交易日）
    start_date: 若传入则忽略 lookback_days（供盘后复盘使用）
    """
    import akshare as ak
    from datetime import datetime, timedelta
    df = ak.stock_zh_index_daily(symbol=symbol)
    if df is None or df.empty:
        return []
    df = df.rename(columns={"date": "dt", "volume": "vol"})
    df["amount"] = 0
    df["dt"] = pd.to_datetime(df["dt"])
    cutoff = pd.to_datetime(start_date) if start_date else \
             pd.Timestamp.now() - pd.Timedelta(days=lookback_days)
    df = df[df["dt"] >= cutoff]
    return _to_raw_bars(df, symbol, Freq.D,
                        "dt", "open", "high", "low", "close", "vol", "amount")
```

**2b. FutuSource 新增 `get_index_kline()`**

```python
def get_index_kline(self, futu_code: str, freq: Freq,
                    start: str = None,
                    lookback_days: int = 180) -> List[RawBar]:
    """
    港股/HK指数历史K线（盘中默认近180日）。
    start 若传入则直接使用（盘后复盘）；否则用 today - lookback_days。
    """
    from datetime import datetime, timedelta
    if start is None:
        start = (datetime.now() - timedelta(days=lookback_days)).strftime("%Y-%m-%d")
    """
    港股/HK指数历史K线。
    futu_code: 如 'HK.800700'（恒生科技）
    freq: Freq.D（日线）或 Freq.W（周线）
    start: 开始日期 'YYYY-MM-DD'
    """
    from futu import KLType, AuType, RET_OK
    ktype_map = {Freq.D: KLType.K_DAY, Freq.W: KLType.K_WEEK,
                 Freq.F60: KLType.K_60M}
    ktype = ktype_map.get(freq, KLType.K_DAY)
    ret, df, _ = self._ctx.request_history_kline(
        futu_code, start=start, ktype=ktype,
        autype=AuType.QFQ, max_count=2000
    )
    if ret != RET_OK or df is None or df.empty:
        return []
    df = df.rename(columns={"time_key": "dt", "volume": "vol",
                             "turnover": "amount"})
    return _to_raw_bars(df, futu_code, freq,
                        "dt", "open", "high", "low", "close", "vol", "amount")
```

注意：Futu 连接需要在调用前 `connect()`，调用后 `close()`。`IndexScreener` 负责生命周期管理。

## Step 3: index_report.py — IndexReport 数据类

```python
@dataclass
class ZSLevel:
    """中枢区间"""
    zd: float    # 中枢下沿
    zg: float    # 中枢上沿
    bi_count: int  # 构成笔数

@dataclass
class IndexReport:
    name: str                    # "沪深300"
    symbol: str                  # "sh000300"
    daily_bi_count: int          # 日线笔数
    weekly_bi_count: int         # 周线笔数
    daily_last_direction: str    # "向上" / "向下"
    weekly_last_direction: str
    daily_trend: str             # "上涨趋势" / "下跌趋势" / "中枢震荡"
    weekly_trend: str
    daily_latest_signal: str     # "三买" / "二卖" / "无"
    daily_zs: Optional[ZSLevel]  # 最近一个中枢
    weekly_zs: Optional[ZSLevel]
    latest_price: float
    summary: str                 # 一行文字总结
```

判断逻辑（在 `index_analyzer.py` 中实现）：

- **趋势判断**：看最近 4 笔（2 上 + 2 下）的高低点趋势
  - 高点和低点都在抬升 → 上涨趋势
  - 高点和低点都在下降 → 下跌趋势
  - 其他 → 中枢震荡
- **中枢识别**：取最近 5 笔，b1/b3 重叠区间 `[max(b1.low,b3.low), min(b1.high,b3.high)]`
- **买卖点**：复用已有 `detectors.py` 中的 5 类检测器

## Step 4: index_analyzer.py — IndexAnalyzer

```python
class IndexAnalyzer:
    """
    为单个指数维护两个 CZSC 实例：日线 + 周线。
    A股：周线由日线 resample 合成；HK（恒生科技）：周线由 Futu 直接提供。
    """
    def __init__(self, name: str, symbol: str,
                 daily_bars: List[RawBar],
                 weekly_bars: Optional[List[RawBar]] = None):
        self.name = name
        self.symbol = symbol
        self.daily  = SymbolAnalyzer(symbol, Freq.D, daily_bars,  max_bi_num=100)
        # 若外部传入周线（Futu直拿）则直接用，否则从日线合成
        w_bars = weekly_bars if weekly_bars else _aggregate_to_weekly(daily_bars)
        self.weekly = SymbolAnalyzer(symbol, Freq.W, w_bars, max_bi_num=50)

    def report(self) -> IndexReport: ...
```

**周线合成函数** `_aggregate_to_weekly(daily_bars)`（放在 `index_analyzer.py`）：

```python
def _aggregate_to_weekly(daily_bars: List[RawBar]) -> List[RawBar]:
    """日线 RawBar → 周线 RawBar（按周五收盘聚合）"""
    df = pd.DataFrame([{...}]).set_index("dt")
    weekly = df.resample("W-FRI").agg(
        open=("open", "first"), high=("high", "max"),
        low=("low", "min"),   close=("close", "last"),
        vol=("vol", "sum")
    ).dropna()
    return [RawBar(symbol=..., dt=dt, freq=Freq.W, ...) for dt, row in weekly.iterrows()]
```

## Step 5: market_context.py — MarketContext

```python
@dataclass
class MarketContext:
    reports: List[IndexReport]           # 8 个指数报告
    overall_direction: str               # "偏多" / "偏空" / "分化"
    buy_indices: List[str]               # 出现买信号的指数名称
    sell_indices: List[str]              # 出现卖信号的指数名称
    growth_vs_value: str                 # 成长/价值 相对强弱
    recommended_style: str               # "成长" / "价值" / "均衡"
    gate_industry_scan: bool             # 是否建议进行行业扫描
    summary: str                         # 2-3 行综合判断

def build_market_context(reports: List[IndexReport]) -> MarketContext:
    """
    聚合逻辑：
    - 买信号指数 ≥ 5 个 → 偏多
    - 卖信号指数 ≥ 5 个 → 偏空
    - 否则 → 分化
    - 创业/科创/1000 强 vs 50/300 强 → 成长 vs 价值判断
    - gate_industry_scan: 偏多或中性才进行行业扫描
    """
```

## Step 6: index_screener.py — 指数层入口

```python
class IndexScreener:
    def __init__(self, ak_codes=None, futu_codes=None,
                 futu_host="127.0.0.1", futu_port=11111):
        self.ak_codes   = ak_codes   or INDEX_AK_CODES    # 7只A股
        self.futu_codes = futu_codes or INDEX_FUTU_CODES  # 1只恒科
        self.ak_source  = AKShareSource()
        self.futu_source = FutuSource(futu_host, futu_port)
        self.analyzers: Dict[str, IndexAnalyzer] = {}

    def initialize(self, lookback_days: int = 180):
        """
        盘中模式：滚动拉取近 lookback_days 自然日的K线（默认180天≈120交易日）。
        若需盘后复盘，可在外部调用时传 start_date 给各数据源方法。

        1. AKShare 顺序拉取 7只A股指数日线（~7s，近120根）
        2. Futu 拉取恒生科技日线+周线（~1s，需要 OpenD 运行）
        3. 周线：A股由日线合成（~24根），恒科用Futu直取
        """
        from config import INDEX_LOOKBACK_DAYS
        lb = lookback_days or INDEX_LOOKBACK_DAYS
        # A股（AKShare，滚动窗口）
        for name, ak_sym in self.ak_codes.items():
            daily = self.ak_source.get_index_daily(ak_sym, lookback_days=lb)
            self.analyzers[name] = IndexAnalyzer(name, ak_sym, daily)
        # 恒科（Futu，同等滚动窗口）
        self.futu_source.connect()
        for name, futu_sym in self.futu_codes.items():
            daily  = self.futu_source.get_index_kline(futu_sym, Freq.D, lookback_days=lb)
            weekly = self.futu_source.get_index_kline(futu_sym, Freq.W, lookback_days=lb)
            self.analyzers[name] = IndexAnalyzer(name, futu_sym, daily, weekly)
        self.futu_source.close()

    def analyze(self) -> MarketContext:
        reports = [az.report() for az in self.analyzers.values()]
        return build_market_context(reports)

    def print_report(self, ctx: MarketContext):
        """
        打印格式示例：
        ════════════════════════════════════
          指数研判报告  2026-03-03
        ════════════════════════════════════
        [沪深300 ] 日:上涨 周:震荡 | 日线三买 | ZS[3850~3920]
        [创业板指] 日:下跌 周:下跌 | 无信号
        [恒生科技] 日:震荡 周:下跌 | 日线背驰买
        ...
        综合: 偏多，成长>价值，建议扫描科技/医药行业
        ════════════════════════════════════
        """

    def run(self) -> MarketContext:
        self.initialize()
        ctx = self.analyze()
        self.print_report(ctx)
        return ctx
```

**Futu OpenD 可用性处理**：若连接失败（OpenD 未启动），恒生科技数据降级跳过，其余7只正常分析，`IndexReport` 中标记为 `"数据不可用"`。

## Step 7: review_screener.py — 盘后复盘入口

```python
class ReviewScreener:
    """
    盘后复盘模式：从指定关键时间节点加载历史K线，进行结构分析。
    - 指数：从 start_date 起加载完整日线历史
    - 个股：从 start_date 起加载日线（AKShare stock_zh_a_hist 或 Futu）
    - 输出：各品种历史结构报告 + 历史信号时间线
    """
    def __init__(self, start_date: str, ak_codes=None, futu_codes=None):
        self.start_date = start_date       # 如 "2024-09-24"
        self.ak_codes   = ak_codes   or INDEX_AK_CODES
        self.futu_codes = futu_codes or INDEX_FUTU_CODES

    def run_index_review(self) -> MarketContext:
        """用 start_date 加载指数历史，生成完整结构报告"""
        screener = IndexScreener(self.ak_codes, self.futu_codes)
        screener.initialize_with_start(self.start_date)  # 固定起点
        return screener.analyze()

    def run_stock_review(self, symbols: List[str]) -> List[ScoredSymbol]:
        """对指定标的列表做日线级别缠论分析（盘后）"""
        # 使用日线，而非分钟线
        ...
```

`IndexScreener` 扩展 `initialize_with_start(start_date)` 方法：

```python
def initialize_with_start(self, start_date: str):
    """盘后模式：从固定起点加载历史K线"""
    for name, ak_sym in self.ak_codes.items():
        daily = self.ak_source.get_index_daily(ak_sym, start_date=start_date)
        self.analyzers[name] = IndexAnalyzer(name, ak_sym, daily)
    self.futu_source.connect()
    for name, futu_sym in self.futu_codes.items():
        daily  = self.futu_source.get_index_kline(futu_sym, Freq.D, start=start_date)
        weekly = self.futu_source.get_index_kline(futu_sym, Freq.W, start=start_date)
        self.analyzers[name] = IndexAnalyzer(name, futu_sym, daily, weekly)
    self.futu_source.close()
```

## Step 8: run.py — 系统总入口（双模式选择）

```python
"""
用法：
  python run.py                                  # 盘中监测（默认）
  python run.py --mode intraday                  # 盘中监测
  python run.py --mode review --start 2024-09-24 # 盘后复盘（从924起）
  python run.py --mode review --start 2025-01-06 # 盘后复盘（从DeepSeek行情起）
"""
import argparse
from signals.index_screener  import IndexScreener
from signals.review_screener import ReviewScreener
from signals.screener        import IntraDayScreener

def run_intraday(args):
    """盘中模式：三层联动实时扫描"""
    # Layer 1
    ctx = IndexScreener().run()               # 滚动近180日日线
    if not ctx.gate_industry_scan:
        print("⚠️  市场偏空，建议观望")
        return
    # Layer 2 + 3
    screener = IntraDayScreener.from_context(ctx)
    screener.run_full(industry=args.industry)

def run_review(args):
    """盘后复盘模式：从关键时间节点加载完整历史，生成结构报告"""
    reviewer = ReviewScreener(start_date=args.start)
    ctx = reviewer.run_index_review()
    ctx.print_full_report()

if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode",       default="intraday", choices=["intraday", "review"])
    parser.add_argument("--start",      default="2024-09-24",
                        help="盘后复盘起始日期，如 2024-09-24")
    parser.add_argument("--industries", default=None,
                        help="覆盖 config.WATCH_INDUSTRIES，逗号分隔。传空字符串跳过行业分析")
    parser.add_argument("--auto-scan-industry", action="store_true",
                        help="盘后模式：自动扫描全市场行业强度（慢）")
    args = parser.parse_args()
    # 行业列表解析：命令行优先，其次 config 默认
    if args.industries is not None:
        args.industry_list = [x.strip() for x in args.industries.split(",") if x.strip()]
    else:
        args.industry_list = config.WATCH_INDUSTRIES
    {"intraday": run_intraday, "review": run_review}[args.mode](args)
```

## 三层联动逻辑（盘中模式）

`MarketContext` 作为入参传入 `IntraDayScreener`：

```python
# IntraDayScreener 新增 from_context 工厂方法
@classmethod
def from_context(cls, ctx: MarketContext) -> "IntraDayScreener":
    """根据指数研判结果，自动确定要扫描的行业"""
    industry = ctx.recommended_industries[0] if ctx.recommended_industries else None
    return cls(symbols=config.WHITELIST, industry=industry)
```

## 数据流总览（完整三层）

```
Layer 1 — 指数研判（盘中模式：滚动近180自然日≈120交易日）
  ┌── A股7只 ─────────────────────────────────────────────────┐
  │  INDEX_AK_CODES → AKShareSource.get_index_daily(          │
  │      symbol, lookback_days=INDEX_LOOKBACK_DAYS)           │
  │  → 日线 ~120根  →  pandas resample("W-FRI") → 周线 ~24根  │
  └──────────────────────────────────────────────────────────── ┘
  ┌── 恒生科技 ────────────────────────────────────────────── ┐
  │  INDEX_FUTU_CODES(HK.800700) → FutuSource                  │
  │  → get_index_kline(lookback_days=INDEX_LOOKBACK_DAYS)      │
  │  → 日线 ~120根(K_DAY) + 周线 ~24根(K_WEEK)                │
  └──────────────────────────────────────────────────────────── ┘
         ↓ IndexAnalyzer(name, symbol, daily, weekly?)
         ↓ IndexReport × 8  →  MarketContext
              (综合方向 / 成长vs价值 / gate_industry_scan)
                       │
                       │ gate = True?
                       ▼
Layer 2 — 行业选择
  industry.py → get_industry_stocks(行业名)  [AKShare]
  → 行业成分股 (50只)  →  IntraDayScreener.run_industry()
  → 行业信号排行榜
                       │
                       ▼
Layer 3 — 标的筛选
  config.WHITELIST + 行业精选
  → IntraDayScreener.run_whitelist()  [AKShare 分钟线]
  → 最终标的评分榜单（缠论买卖点 + 多级别共振）
```

**改动文件汇总**：

| 文件 | 操作 | 内容 |
|---|---|---|
| `run.py` | **新建** | 系统入口，`--mode intraday\|review --start YYYY-MM-DD` |
| `config.py` | 修改 | 新增 `INDEX_AK_CODES`, `INDEX_FUTU_CODES`, `INDEX_LOOKBACK_DAYS` |
| `monitor/data_fetcher.py` | 修改 | `AKShareSource.get_index_daily(lookback_days, start_date)` + `FutuSource.get_index_kline()` |
| `signals/index_report.py` | **新建** | `ZSLevel`, `IndexReport` 数据类 |
| `signals/index_analyzer.py` | **新建** | `IndexAnalyzer`, `_aggregate_to_weekly()` |
| `signals/market_context.py` | **新建** | `MarketContext`, `build_market_context()` |
| `signals/index_screener.py` | **新建** | `IndexScreener`（`initialize()` 盘中 + `initialize_with_start()` 盘后） |
| `signals/review_screener.py` | **新建** | `ReviewScreener`（盘后复盘入口） |

---

# ════════════════════════════════════════
# Layer 3：标的筛选（已有，保留）
# ════════════════════════════════════════

## 已验证的关键 API

| 项目 | 实际行为 |
|---|---|
| `Direction.Up / .Down` | Rust enum，`==` 比较有效 |
| `fx.mark` | `"底分型"` / `"顶分型"`，字符串 `==` 有效 |
| `bi.high / .low / .sdt / .edt` | 直接属性，float/datetime |
| `bi.power_price / .power_volume` | 可用，用于背驰检测 |
| `CZSC.update(bar: RawBar)` | 增量更新，签名确认 |
| `CZSC.finished_bis` | 已完成笔列表 |
| `cxt_first_buy_V221126(c, di=1)` | 可用，返回 OrderedDict |
| `AKShareSource.get_a_minute('SH.601958', Freq.F15)` | 返回 1970 根 K 线，49 笔 |

## 性能实测数据

| 环节 | 耗时/只/级别 | 占比 |
|---|---|---|
| AKShare 拉取 | ~10s | 91% |
| CZSC 初始化 | 0.012s | <1% |
| 信号检测 | ~1s | 9% |

**并发 5 线程加速 ~3.4x**。瓶颈完全在网络 I/O。

| 筛选范围 | 标的数 | 并发耗时 |
|---|---|---|
| 白名单 | 10~20 只 | ~1 分钟 |
| 单行业 | 50 只 | ~5 分钟 |
| 大行业 | 100 只 | ~10 分钟 |

## 两轮筛选模式

用户选择：**白名单快扫 + 行业批扫**，分两轮执行。

- **第 1 轮**（白名单）：config.WHITELIST 中的标的，~1 分钟出结果
- **第 2 轮**（行业扫描）：指定行业代码，通过 AKShare 获取行业成分股，~5 分钟出结果
- 两轮结果合并排序，输出最终榜单

## 新增文件（7 个）

所有文件位于 `/Users/zhangqilong/Desktop/Signals/signals/`：

```
signals/
├── __init__.py       # 包导出
├── freq_utils.py     # 频率映射：config 字符串 ↔ czsc.Freq
├── analyzer.py       # SymbolAnalyzer：每个(标的,级别)一个 CZSC 实例
├── detectors.py      # 信号检测：一买/二买/三买/背驰/趋势
├── scorer.py         # 评分排序：多信号加权 + 多级别共振加分
├── screener.py       # 主筛选器：两轮模式 + 并发拉取 + 控制台输出
├── industry.py       # 行业工具：获取行业列表 + 成分股
└── validate.py       # 验证脚本：端到端测试
```

## 实现步骤

### Step 1: `freq_utils.py` — 频率映射

`config.MONITOR_FREQS = ["15min", "30min"]` → `czsc.Freq.F15 / F30`

```python
FREQ_MAP = {"1min": Freq.F1, "5min": Freq.F5, "15min": Freq.F15,
            "30min": Freq.F30, "60min": Freq.F60, "daily": Freq.D}

def config_freq_to_czsc(freq_str: str) -> Freq
```

### Step 2: `analyzer.py` — CZSC 包装器

每个 (symbol, freq) 对维护一个 CZSC 实例：

- `__init__(symbol, freq, bars)` — 用历史 K 线初始化 CZSC
- `update(bar)` — 增量喂入新 K 线（`_last_dt` 防重复）
- 代理属性：`bi_list`, `finished_bis`, `fx_list`

### Step 3: `detectors.py` — 信号检测（核心）

定义 `SignalEvent` 数据类和 5 个检测器：

| 检测器 | 逻辑 | 信心度 |
|---|---|---|
| **一买** | 调用 `cxt_first_buy_V221126(c, di=1)`，解析返回值中的 "一买" 关键词 | 0.7 |
| **二买** | 最近5笔：b1↓ b3↓ 且 `b3.low > b1.low`（回调不破前低） | 0.8 |
| **三买** | 最近5笔构成中枢 `[zs_zd, zs_zg]`，回调低点 > 中枢上沿 | 0.85 |
| **背驰** | 同方向相邻两笔：创新高/低但 `power_price` 减弱 | 0.6~0.75 |
| **趋势** | 调用 `cxt_bs_V240526(c)`，解析 "买点"/"卖点" | 0.7 |

返回 `List[SignalEvent]`，每个事件含 symbol, freq, dt, signal_type, confidence, price, details。

### Step 4: `scorer.py` — 评分引擎

信号权重表：

| 信号 | 分值 | 原因 |
|---|---|---|
| 二买 | 55 | 缠论认为风险收益比最佳 |
| 三买 | 50 | 最安全的参与点 |
| 一买 | 40 | 高收益但高风险 |
| 背驰买 | 35 | 需要确认 |
| 趋势买 | 30 | 辅助信号 |
| 卖点 | 对应负值 | |

- 每个信号：`base_score × confidence`
- 多级别共振加分：同方向买信号出现在 15min + 30min → +15 分
- 最终 `ScoredSymbol(symbol, total_score, signals, details)`

### Step 5: `industry.py` — 行业成分股获取

通过 AKShare 获取行业列表和成分股，转换为 Futu 格式代码：

```python
def get_industry_list() -> pd.DataFrame           # 返回所有行业名称
def get_industry_stocks(industry: str) -> List[str]  # 行业名 → ["SH.600xxx", ...]
```

使用 `akshare.stock_board_industry_name_em()` 获取行业列表，
`akshare.stock_board_industry_cons_em(symbol=行业名)` 获取成分股。

### Step 6: `screener.py` — 主筛选器（两轮模式）

`IntraDayScreener` 类，支持并发拉取 + 两轮筛选：

```
__init__(symbols, freqs, max_workers=5)
initialize()       # 并发拉取分钟线 → 创建 SymbolAnalyzer（ThreadPoolExecutor）
scan_once()        # detect_all_signals → score → 排序
print_results()    # 控制台格式化输出

run_whitelist()    # 第1轮：白名单快扫（~1分钟）
run_industry(name) # 第2轮：行业批扫（~5分钟）
run_full(industry) # 两轮合并：白名单 + 行业，去重后合并排序
```

### Step 7: `validate.py` — 端到端验证

用 3 只股票（SH.601958 金钼股份、SH.600519 贵州茅台、SZ.000001 平安银行）× 2 个级别（15min、30min）验证：

1. 数据加载 → 打印 K 线数量（并发拉取）
2. CZSC 分析 → 打印笔数/分型数/最后一笔方向
3. 信号检测 → 打印所有检测到的信号
4. 评分排序 → 打印最终得分
5. 行业接口测试 → 获取一个行业的成分股列表

运行方式：`python -m signals.validate`（从 Signals/ 目录）

## 数据流总览

```
┌─ 第1轮：白名单快扫 (~1min) ─────────────────────┐
│  config.WHITELIST → ["SH.601958", ...]           │
└──────────────────────┬───────────────────────────┘
                       │
┌─ 第2轮：行业批扫 (~5min) ─────────────────────── ┐
│  industry.py → get_industry_stocks("有色金属")    │
│  → ["SH.600489", "SZ.002460", ...] (50只)        │
└──────────────────────┬───────────────────────────┘
                       │  合并去重
                       ▼
  ThreadPoolExecutor(5) 并发拉取
  AKShareSource.get_a_minute(symbol, Freq.F15/F30)
       │
       ▼ List[RawBar] (~2000根/级别)
       │
  SymbolAnalyzer(symbol, freq, bars)
       │
       ▼ CZSC 对象 (bi_list, fx_list, finished_bis)
       │
  detect_all_signals(czsc, symbol)
       │
       ▼ List[SignalEvent]
       │
  score_signals(symbol, signals)
       │
       ▼ ScoredSymbol(total_score, details)
       │
  screener.print_results()  ──→  控制台输出（按评分排序）
```

## V1 信号体系

当前 5 类纯缠论结构信号：

| 信号 | 检测方法 | 信心度 | 适用场景 |
|---|---|---|---|
| 一买 | `cxt_first_buy_V221126` 内置 | 0.7 | 下跌末端底背驰反转 |
| 二买 | 笔结构：回调不破前低 | 0.8 | 一买确认后首次回调 |
| 三买 | 中枢分析：回调不破上沿 | 0.85 | 趋势确立后的上车点 |
| 背驰 | `power_price` 同向笔力度对比 | 0.6~0.75 | 上涨/下跌衰竭预警 |
| 趋势 | `cxt_bs_V240526` 内置 | 0.7 | 趋势跟踪确认 |

## 后续优化路线图

### Phase 1: 信号丰富度（加维度，改 `detectors.py`）

| 维度 | 可加信号 | czsc 库函数 | 改动 |
|---|---|---|---|
| MACD 辅助 | 金叉/死叉配合笔端背驰 | `tas_macd_base_V221028` 等 20+ | detectors.py 加函数 |
| 均线系统 | MA 多头排列、突破/回踩 | `tas_ma_base_V221101` 等 | detectors.py 加函数 |
| 成交量确认 | 放量突破、缩量回调 | `bi.power_volume` 已有 | 现有检测器加判断 |
| 中枢震荡 | 中枢内高抛低吸 | 需自定义 | detectors.py 加函数 |
| 多级别共振 | 日线+30分+15分同向 | 加载日线 CZSC | screener.py 加日线 |

### Phase 2: 信号质量（减假信号，改 `detectors.py`）

| 优化因素 | 说明 | 数据来源 |
|---|---|---|
| 笔力度过滤 | 太弱的笔产生的信号可信度低 | `bi.power_price` 设最小阈值 |
| 中枢级别确认 | 真正三买需至少 3 笔重叠中枢 | 检查 5 笔以上中枢 |
| 分型强度 | 弱分型产生的买卖点不可靠 | `fx.power_str`, `fx.power_volume` |
| 回撤幅度阈值 | 二买回调超 61.8% 则可信度降低 | 计算回撤比例 |
| 时间衰减 | 信号距今越久分值越低 | 指数衰减函数 |

### Phase 3: 评分模型优化（改 `scorer.py`）

| 方向 | 做法 |
|---|---|
| 历史回测校准 | 用 czsc `DummyBacktest` 回测各信号胜率，据此调整权重 |
| 动态权重 | 牛市提高三买权重，熊市提高一买权重 |
| 信号衰减 | 生成越久分值越低（时间衰减函数） |
| 行业贝塔 | 热门行业加分，冷门行业减分 |

### Phase 4: 数据源升级（改 `screener.py`）

| 升级 | 效果 | 改动 |
|---|---|---|
| Futu 实时推送 | 延迟从分钟级降到秒级 | screener.py 加 FutuSource 模式 |
| 日线叠加 | 多级别完整分析 | analyzer 加日线 CZSC 实例 |
| 资金流数据 | 额外过滤维度 | industry.py 加资金流接口 |

**架构设计支持逐步叠加**：加信号改 `detectors.py`，调权重改 `scorer.py`，不需要改管道架构。

## 复用的现有文件

- `config.py` — WHITELIST, MONITOR_FREQS, SCORE_THRESHOLD, MAX_POOL_SIZE
- `monitor/data_fetcher.py` — `AKShareSource.get_a_minute()` 作为主数据源

## 验证方式

```bash
cd /Users/zhangqilong/Desktop/Signals
python -m signals.validate
```

预期输出：
1. 6 个 analyzer 初始化成功（3 标的 × 2 级别）
2. 每个打印 K 线数量 > 1000、笔数量 > 10
3. 检测到的信号列表（可能为空，取决于当下市场结构）
4. 评分排序结果
5. 即使无信号，笔/分型数据能证明管道正常工作
