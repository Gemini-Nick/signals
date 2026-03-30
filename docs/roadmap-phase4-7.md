# Phase 4-7 实施计划：四阶段交易闭环 + 深度社交

## Context

Phase 0-3 已完成（分支稳定化 + 集成验证 + 前端打磨 + 冒烟测试，commit `4a5bf5a`）。
现在按顺序推进 Phase 4 → 5 → 6 → 7，构建四阶段交易闭环 + 深度社交能力。

**核心依赖链**: replay.py(P4) → planner.py(P5) → weekly.py(P5) → trade_log(P7)

---

## Phase 4: 信号回放引擎 (~4-6小时)

> 逐根K线回放信号变化，生成时间线（出现/确认/消失），为复盘增加"过程维度"

### 4.1 新建 `signals/core/replay.py`

```python
@dataclass
class SignalChange:
    dt: datetime
    symbol: str
    freq: str              # "日线" / "30分钟"
    signal_type: str       # "一买" / "二买" / "三买" / "背驰买" 等
    action: str            # "appear" / "disappear"
    price: float
    confidence: float
    bar_index: int         # 第几根K线时出现

class SignalReplayer:
    """逐根K线回放，跟踪信号出现/消失"""

    def __init__(self, symbol: str, freq: Freq):
        self._symbol = symbol
        self._freq = freq
        self._analyzer: Optional[SymbolAnalyzer] = None
        self._prev_signals: Dict[str, SignalEvent] = {}  # key=(signal_type) → SignalEvent
        self._timeline: List[SignalChange] = []
        self._bar_count = 0
        self._warmup = 30  # 前30根用于建立结构，不检测信号

    def feed_bar(self, bar: RawBar):
        """喂入一根K线，检测信号变化"""
        self._bar_count += 1
        if self._analyzer is None:
            self._analyzer = SymbolAnalyzer(self._symbol, self._freq, [bar])
        else:
            self._analyzer.update(bar)

        if self._bar_count < self._warmup:
            return  # 热身期不检测

        # 检测当前状态的所有信号
        current_signals = detect_all_signals(self._analyzer.czsc, self._symbol)
        current_map = {s.signal_type: s for s in current_signals}

        # 对比: 新出现的信号
        for sig_type, sig in current_map.items():
            if sig_type not in self._prev_signals:
                self._timeline.append(SignalChange(
                    dt=bar.dt, symbol=self._symbol, freq=sig.freq,
                    signal_type=sig_type, action="appear",
                    price=bar.close, confidence=sig.confidence,
                    bar_index=self._bar_count,
                ))

        # 对比: 消失的信号
        for sig_type in self._prev_signals:
            if sig_type not in current_map:
                self._timeline.append(SignalChange(
                    dt=bar.dt, symbol=self._symbol, freq=sig.freq if sig_type in current_map else self._prev_signals[sig_type].freq,
                    signal_type=sig_type, action="disappear",
                    price=bar.close, confidence=0.0,
                    bar_index=self._bar_count,
                ))

        self._prev_signals = current_map

    @property
    def timeline(self) -> List[SignalChange]:
        return self._timeline

    @property
    def final_signals(self) -> List[SignalEvent]:
        return list(self._prev_signals.values())
```

**复用已有代码**:
- `signals/core/analyzer.py:SymbolAnalyzer` — 已有 `.update(bar)` 增量喂入
- `signals/core/detectors.py:detect_all_signals()` — 每根K线后重新检测
- `signals/core/detectors.py:SignalEvent` dataclass

**关键设计**:
- warmup=30: 前30根K线仅建立笔段结构，不生成信号变化（避免噪声）
- 信号比较 key 用 `signal_type`（如"一买"），同类型信号只跟踪最新实例
- `feed_bar` 不做评分，只跟踪信号出现/消失

### 4.2 便捷函数: `replay_stock()`

```python
def replay_stock(symbol: str, bars: List[RawBar], freq: Freq = Freq.D) -> List[SignalChange]:
    """一键回放：传入完整K线列表，返回信号时间线"""
    replayer = SignalReplayer(symbol, freq)
    for bar in bars:
        replayer.feed_bar(bar)
    return replayer.timeline
```

### 4.3 集成到 ReviewState

**修改**: `signals/web/services/engine.py`

1. `ReviewState` 新增字段:
```python
replay_timelines: Dict[str, List[object]] = field(default_factory=dict)  # symbol → timeline
```

2. 在 `run_review()` 的 L3 完成后，对达标标的生成回放时间线:
```python
# L3 完成后，对 top-N 标的生成回放时间线
for sc in rv.scored_symbols[:10]:  # 只对 top 10 回放（控制耗时）
    cache_key = f"{sc.symbol.replace('.', '_')}_{today}"
    cached = get_cache().get(cache_key)
    if cached and len(cached) >= 30:
        daily_bars = _records_to_rawbars(cached, sc.symbol)
        timeline = replay_stock(sc.symbol, daily_bars)
        rv.replay_timelines[sc.symbol] = timeline
```

### 4.4 序列化 + API

**修改**: `signals/web/services/serializers.py` 新增:
```python
def serialize_signal_change(sc: SignalChange) -> dict:
    return {
        "dt": _dt_to_unix(sc.dt),
        "signal_type": sc.signal_type,
        "action": sc.action,
        "price": round(sc.price, 2),
        "confidence": round(sc.confidence, 2),
        "bar_index": sc.bar_index,
    }
```

**修改**: `signals/web/api/review.py` — `/api/review/results` 返回值新增:
```python
"replay_timelines": {
    sym: [serialize_signal_change(sc) for sc in timeline]
    for sym, timeline in rv.replay_timelines.items()
}
```

### 4.5 前端: Review 信号时间线

**修改**: `signals/web/static/js/review.js` 新增 `_renderTimeline()`:
- 在信号列表下方增加"信号时间线"折叠区域
- 点击某只股票 → 展开该股票的 appear/disappear 时间轴
- 用简单的时间线列表（不需要图表库）:
```
[03-05 日线] 一买 出现 @ 25.30 (conf=0.85)
[03-07 日线] 二买 出现 @ 26.10 (conf=0.72)
[03-10 日线] 一买 消失 @ 27.50
```

### Phase 4 验证

```bash
# 1. 单元测试
python -c "
from signals.core.replay import replay_stock, SignalReplayer
from czsc import Freq
print('replay import OK')
"

# 2. 集成测试: 启动web, 运行复盘, 检查results中replay_timelines
python run.py --mode web
# POST /api/review/run → 等完成 → GET /api/review/results
# 验证 response 包含 replay_timelines 字段
```

### Phase 4 文件清单

| 文件 | 操作 | 说明 |
|------|------|------|
| `signals/core/replay.py` | **新建** | SignalChange + SignalReplayer + replay_stock() |
| `signals/web/services/engine.py` | 修改 | ReviewState 新增 replay_timelines, run_review() L3后回放 |
| `signals/web/services/serializers.py` | 修改 | 新增 serialize_signal_change() |
| `signals/web/api/review.py` | 修改 | /api/review/results 返回 replay_timelines |
| `signals/web/static/js/review.js` | 修改 | 新增 _renderTimeline() 时间线UI |
| `signals/web/static/css/app.css` | 修改 | 时间线样式 |

---

## Phase 5: 盘前计划 + 周末策略 (~6-8小时)

> 完全分类状态机 + 目标位计算 + 宏观事件剧本

### 5.1 新建 `signals/core/planner.py`

**PlanGenerator 类**:
- 输入: 某指数/个股的 CZSC 分析结果（笔段、中枢）
- 输出: 完全分类的 3 种情景

```python
@dataclass
class Scenario:
    name: str              # "分类A: 上涨延续" / "分类B: 回调确认" / "分类C: 反转"
    probability_hint: str  # "偏高" / "中等" / "偏低"
    trigger: str           # "站稳3200" / "跌破3150"
    action: str            # "加仓至7成" / "减仓至3成"
    target_prices: List[float]
    rationale: str

class PlanGenerator:
    def generate(self, analyzer: SymbolAnalyzer) -> List[Scenario]:
        # 基于中枢ZG/ZD + 笔端点计算目标位
        # 4种方法: 中枢边界 / 嵌套精确 / 新中枢投射 / Fibonacci
```

**复用**: `SymbolAnalyzer.finished_bis`, `czsc.zs_list` 中枢列表

### 5.2 新建 `signals/core/weekly.py`

**WeeklyStrategy 类**:
- 输入: 下周宏观事件日历（AKShare `stock_em_macro_event`）
- 输出: 事件剧本 + 仓位建议

```python
@dataclass
class EventPlaybook:
    event_name: str        # "CPI数据"
    event_date: str
    scenarios: Dict[str, str]  # {"鹰派": "减仓防守", "鸽派": "加仓进攻", "中性": "维持"}
    affected_sectors: List[str]

class WeeklyStrategy:
    def generate(self, index_reports, rotation_stage) -> dict:
        # 宏观日历 + 技术结构 → 周度策略
```

### 5.3 API + CLI + 前端

- `POST /api/plan/generate` — 盘前计划
- `GET /api/weekly/latest` — 周末策略
- `run.py --mode plan` / `run.py --mode weekly`
- 前端新增 Plan 页面（复用 review 页面模式）

### Phase 5 文件清单

| 文件 | 操作 |
|------|------|
| `signals/core/planner.py` | **新建** |
| `signals/core/weekly.py` | **新建** |
| `signals/web/api/plan.py` | **新建** |
| `signals/web/services/engine.py` | 修改 (新增 run_plan/run_weekly) |
| `signals/web/app.py` | 修改 (注册 plan_router) |
| `signals/web/static/index.html` | 修改 (新增 Plan tab) |
| `signals/web/static/js/plan.js` | **新建** |
| `run.py` | 修改 (新增 --mode plan/weekly) |

---

## Phase 6: 深度社交 + NLP (~6-8小时)

> 小红书/贴吧爬虫 + 情绪关键词NLP + 产业链映射

### 6.1 深度爬虫框架

**新建**: `signals/data/scrapers/` 目录
- `base.py` — BaseScraper 抽象类（rate limit, cache, retry）
- `xiaohongshu.py` — Playwright 爬虫（搜索+提取帖子数/互动量）
- `tieba.py` — requests 爬虫（股吧帖子数/回复量/标题情绪）

设计: 统一 `ScrapedPost` 数据结构，2-4h 缓存 TTL

### 6.2 情绪 NLP

**新建**: `signals/core/sentiment_nlp.py`
- 预置情绪词库 (~200词): 利好/突破/龙头/加仓 vs 套牢/暴雷/割肉/减仓
- `analyze_sentiment(texts: List[str]) -> float` 返回 -100~+100
- 集成到 `social_fetcher.py` 的 `SocialHeatSnapshot`

### 6.3 产业链映射

**新建**: `signals/core/chain_map.py`
- 静态 dict 定义 20 条产业链（算力/昇腾/机器人/储能/光伏...）
- 每条链: 上游/中游/下游 + 代表标的代码
- `get_chain_position(symbol) -> ChainPosition` 标注个股在链中位置
- 与 `theme_discovery.py` 联动: 发现标的自动标注产业链

### Phase 6 文件清单

| 文件 | 操作 |
|------|------|
| `signals/data/scrapers/__init__.py` | **新建** |
| `signals/data/scrapers/base.py` | **新建** |
| `signals/data/scrapers/xiaohongshu.py` | **新建** |
| `signals/data/scrapers/tieba.py` | **新建** |
| `signals/core/sentiment_nlp.py` | **新建** |
| `signals/core/chain_map.py` | **新建** |
| `signals/data/social_fetcher.py` | 修改 (集成深度爬虫+NLP) |
| `signals/core/theme_discovery.py` | 修改 (集成产业链标注) |

---

## Phase 7: 交易日志 + 归因 (~8-10小时)

> 交易记录 + 操作评分 + 遗漏分析 + 月度Dashboard

### 7.1 交易日志

**新建**: `signals/core/trade_log.py`
- SQLite 存储 (`trade_log.db`)
- `TradeRecord` dataclass: symbol, direction, entry_price, exit_price, entry_date, exit_date, signal_type, score
- 手动录入 API / CSV 导入

### 7.2 操作评分

- 执行分 1-5: 入场时机 + 仓位 + 出场时机
- 错误分类: A-type(系统方差) / B-type(执行偏差) / C-type(情绪交易)
- 与回放时间线对照: 信号出现时你做了什么？

### 7.3 遗漏分析

- 系统发出买点信号但未操作的标的
- 事后涨幅追踪（如果买了会赚多少）
- 月度/季度统计

### Phase 7 文件清单

| 文件 | 操作 |
|------|------|
| `signals/core/trade_log.py` | **新建** |
| `signals/web/api/trade.py` | **新建** |
| `signals/web/static/js/trade.js` | **新建** |
| `signals/web/static/index.html` | 修改 (新增 Trade tab) |

---

## 依赖关系 & 工作量

```
Phase 4 (回放引擎, 4-6h) ──→ Phase 5 (计划/策略, 6-8h) ──→ Phase 7 (交易日志, 8-10h)
                              Phase 6 (深度社交, 6-8h) ←── 独立可并行
```

| Phase | 新建文件 | 修改文件 | 工时 |
|-------|---------|---------|------|
| 4 | 1 | 5 | 4-6h |
| 5 | 4 | 4 | 6-8h |
| 6 | 6 | 2 | 6-8h |
| 7 | 3 | 1 | 8-10h |
| **合计** | **14** | **12** | **24-32h** |
