# 多维信号融合优化方案

## Context

当前 Signals 系统本质是**单维度**分析——缠论技术面结构检测 → 评分 → 筛选。一位资深交易员指出：缠论的笔段只是描述工具（类似物理学里的力箭头），AI 的真正优势在于**多维度变量的信息聚合**。当 ≥2 个独立维度同时触发异常时，置信度才真正高。

本方案在不动缠论骨架的前提下，新增统计异常检测层 + 信号融合框架，将系统从"单维度评分"升级为"多维度融合评分"。

---

## 总体架构

```
现有流程:
  detect_all_signals() → score_signals() → ScoredSymbol

升级后:
  detect_all_signals() → score_signals() → ScoredSymbol (缠论维度)
                                              ↓
  compute_anomaly_profile(bars) ─────→ AnomalyProfile (统计维度)
                                              ↓
  fuse_scores(scored, anomaly) ─────→ FusedScore (多维融合)
                                              ↓
                                     ScoredSymbol + anomaly + fused
```

---

## Phase 1: 统计异常检测引擎

### 新建文件: `signals/core/anomaly.py`

核心思路：对已有 OHLCV 数据计算滚动统计量，输出 z-score，标记异常。

```python
@dataclass
class AnomalyItem:
    """单个异常维度"""
    name: str           # "volume" / "amplitude" / "gap" / "range"
    z_score: float      # 标准差倍数
    raw_value: float    # 原始值
    rolling_mean: float # 滚动均值
    rolling_std: float  # 滚动标准差
    is_anomaly: bool    # 是否超过阈值
    label: str          # "异常放量" / "异常缩量" / "正常"

@dataclass
class AnomalyProfile:
    """一只标的的异常画像"""
    symbol: str
    items: Dict[str, AnomalyItem]   # key = anomaly name
    anomaly_count: int              # 触发异常的维度数
    convergence: bool               # ≥2 维度同时异常
    capitulation_score: float       # 割肉指标 (0-100)
    summary: str                    # 人类可读一行摘要
```

### 5 个异常维度算法

所有维度使用统一框架：`z_score = (today_value - rolling_mean) / rolling_std`

| # | 维度 | 计算公式 | 异常阈值 | 数据来源 |
|---|------|---------|---------|---------|
| 1 | **量能异常** `volume` | today_vol vs 20日均量 | >2σ 异常放量, <-1.5σ 异常缩量 | `bar.vol` |
| 2 | **振幅异常** `range` | (high-low)/close vs 20日均振幅 | >2.5σ 异常波动 | `bar.high/low/close` |
| 3 | **跳空异常** `gap` | \|open - prev_close\| / prev_close vs 20日均跳空 | >2σ 异常跳空 | `bar.open, prev.close` |
| 4 | **实体异常** `body` | \|close-open\| / open vs 20日均实体 | >2σ 异常大阳/大阴 | `bar.close/open` |
| 5 | **量价背离** `vol_price_div` | 价格新高但量能 z<-1, 或价格新低但量能 z>1.5 | 组合判断 | 综合 |

### 割肉指标 `capitulation_score` (0-100)

多因子打分，检测散户集中止损：

```
因子1 (30分): 量能异常放量 (vol_z > 2) → 恐慌抛售
因子2 (25分): 长下影线 (下影 > 2× 实体) → 抛后有承接
因子3 (25分): 连续缩量阴跌后突然放量 (前5日缩量 + 今日vol_z > 1.5)
因子4 (20分): 收盘靠近日内最低 + 持续下跌环境 (close < MA20)
```

`capitulation_score >= 60` 表示高概率割肉出清，是逆向买入机会。

### 核心函数

```python
def compute_anomaly_profile(
    symbol: str,
    bars: List[RawBar],         # 日线 bars（需 >= 30 根）
    window: int = 20,           # 滚动窗口
    thresholds: dict = None,    # 可覆盖阈值
) -> Optional[AnomalyProfile]:
    """从日线 bars 计算异常画像"""
```

算法步骤:
1. 提取 closes, volumes, highs, lows, opens 序列
2. 对每个维度计算 rolling_mean 和 rolling_std（窗口=20）
3. 计算最新一根 bar 的 z_score
4. 判断异常 + 生成 label
5. 统计 anomaly_count，判断 convergence
6. 计算 capitulation_score
7. 生成 summary 文本

---

## Phase 2: 信号融合框架

### 新建文件: `signals/core/fusion.py`

```python
@dataclass
class FusedScore:
    """多维融合评分"""
    raw_czsc_score: float        # 原始缠论分数
    anomaly_boost: float         # 异常维度加减分
    convergence_bonus: float     # 多维收敛加分
    capitulation_bonus: float    # 割肉指标加分
    fused_total: float           # 融合后总分
    dimension_count: int         # 触发维度数
    confidence_level: str        # "高"(≥3维) / "中"(2维) / "低"(1维) / "无"(0维)
    detail: str                  # 明细
```

### 融合算法

```python
def fuse_scores(
    scored: ScoredSymbol,
    anomaly: Optional[AnomalyProfile],
    weights: dict = None,
) -> FusedScore:
```

融合逻辑:
```
1. base = scored.total_score (缠论原始分)

2. 异常加减分 (anomaly_boost):
   - 买信号 + 异常放量(vol_z > 2): +15
   - 买信号 + 异常缩量(vol_z < -1.5): -10
   - 卖信号 + 异常放量: -10 (恐慌加剧)
   - 异常跳空(gap_z > 2) + 买信号方向一致: +10
   - 异常波动(range_z > 2.5): ±5 (需看方向)

3. 多维收敛加分 (convergence_bonus):
   - ≥3 维度同时异常: +20
   - 2 维度同时异常: +12
   - 1 维度异常: +5
   - 0 维度: 0
   （仅在异常方向与缠论信号方向一致时给加分）

4. 割肉指标加分 (capitulation_bonus):
   - capitulation_score >= 80: +25 (极度恐慌出清)
   - 60-80: +15 (恐慌出清)
   - 40-60: +8 (偏弱)
   - <40: 0

5. fused_total = base + anomaly_boost + convergence_bonus + capitulation_bonus
```

---

## Phase 3: 集成到现有系统

### 修改文件 1: `signals/core/scorer.py`

在 `ScoredSymbol` dataclass 中新增 3 个可选字段:
```python
@dataclass
class ScoredSymbol:
    # ... 现有字段不变 ...
    anomaly_profile: Optional["AnomalyProfile"] = None   # 异常画像
    fused_score: Optional["FusedScore"] = None            # 融合评分
    fused_total: float = 0.0                              # 融合后总分 (方便排序)
```

### 修改文件 2: `signals/layers/screener.py`

在 `scan_once()` 方法中，`score_signals()` 之后添加异常计算和融合:

```python
# 在 line 338 results.append(score_signals(...)) 之后:
# 异常检测 + 融合
for scored in results:
    daily_bars = self._get_daily_bars(scored.symbol)  # 新增方法
    if daily_bars and len(daily_bars) >= 30:
        anomaly = compute_anomaly_profile(scored.symbol, daily_bars)
        fused = fuse_scores(scored, anomaly)
        scored.anomaly_profile = anomaly
        scored.fused_score = fused
        scored.fused_total = fused.fused_total
```

新增 `_get_daily_bars()` 方法：从 AKShare 获取日线（带缓存），用于计算异常。

排序策略改为: `results.sort(key=lambda x: x.fused_total or x.total_score, reverse=True)`

### 修改文件 3: `signals/layers/stock_deep_dive.py`

在 `_run_analysis()` 方法 line 300（score_signals 之后）添加:

```python
# 异常检测 + 融合
from signals.core.anomaly import compute_anomaly_profile
from signals.core.fusion import fuse_scores

self.anomaly = compute_anomaly_profile(self.symbol, self.daily_bars)
if self.scored and self.anomaly:
    self.fused = fuse_scores(self.scored, self.anomaly)
    self.scored.anomaly_profile = self.anomaly
    self.scored.fused_score = self.fused
    self.scored.fused_total = self.fused.fused_total
```

### 修改文件 4: `config.py`

新增配置常量:
```python
# ── 异常检测配置 ──
ANOMALY_ROLLING_WINDOW = 20              # 滚动统计窗口
ANOMALY_THRESHOLDS = {
    "volume":         {"high": 2.0, "low": -1.5},
    "range":          {"high": 2.5},
    "gap":            {"high": 2.0},
    "body":           {"high": 2.0},
}

# ── 割肉指标权重 ──
CAPITULATION_WEIGHTS = {
    "volume_spike": 30,    # 异常放量
    "lower_shadow": 25,    # 长下影线
    "vol_breakout": 25,    # 缩量后放量
    "close_at_low": 20,    # 收盘靠近最低
}

# ── 信号融合权重 ──
FUSION_WEIGHTS = {
    "anomaly_volume_boost": 15,    # 异常放量加分
    "anomaly_volume_penalty": -10, # 异常缩量减分
    "convergence_3dim": 20,        # 3维收敛加分
    "convergence_2dim": 12,        # 2维收敛加分
    "convergence_1dim": 5,         # 1维异常加分
    "capitulation_extreme": 25,    # 极度割肉加分
    "capitulation_high": 15,       # 恐慌割肉加分
    "capitulation_medium": 8,      # 偏弱割肉加分
}
```

### 修改文件 5: `signals/web/services/serializers.py`

新增序列化函数，让前端能展示异常数据:
```python
def serialize_anomaly_profile(anomaly) -> dict:
    """AnomalyProfile → JSON"""

def serialize_fused_score(fused) -> dict:
    """FusedScore → JSON"""
```

更新 `serialize_scored_symbol()` 添加 anomaly 和 fused 字段。

---

## 实施顺序

```
Step 1: 创建 signals/core/anomaly.py                    [后端核心]
        - AnomalyItem, AnomalyProfile 数据结构
        - compute_anomaly_profile() 核心算法
        - _calc_z_score() 工具函数
        - _calc_capitulation_score() 割肉指标
        → 可独立测试：传入 bars 列表直接输出

Step 2: 创建 signals/core/fusion.py                     [后端核心]
        - FusedScore 数据结构
        - fuse_scores() 融合算法
        → 可独立测试：传入 ScoredSymbol + AnomalyProfile

Step 3: 修改 config.py                                  [配置]
        - 添加 ANOMALY_* 和 FUSION_* 常量

Step 4: 修改 scorer.py                                  [数据结构]
        - ScoredSymbol 添加 3 个可选字段

Step 5: 修改 stock_deep_dive.py                         [集成]
        - 在 _run_analysis() 中接入 anomaly + fusion
        → 验证: 跑 StockDeepDive("SZ.300750") 看异常数据输出

Step 6: 修改 screener.py                                [集成]
        - 在 scan_once() 中接入 anomaly + fusion
        - 新增 _get_daily_bars() 日线获取方法
        → 验证: python run.py --mode review 看融合排序

Step 7: 修改 stock.py (API)                             [API]
        - analyze_stock() 序列化 anomaly + fused 字段
        → 验证: curl /api/stock/analyze/SZ.300750 看 JSON

Step 8: 修改 stock.js + app.css                         [前端-个股页]
        - 评分卡片升级: 双分数(缠论分+融合分) + 置信度标签
        - 新增 renderAnomalyRadar(): z-score 进度条 + 收敛提示 + 割肉仪表
        - 新增 .anomaly-* 系列 CSS 样式
        → 验证: Web 打开个股详情，看异常雷达卡片

Step 9: 修改 dashboard.js                               [前端-总览页]
        - 信号列表条目追加异常指示器小标签
        → 验证: Web 打开 dashboard，看信号列表异常标记
```

---

## Phase 4: 前端展示设计

当前个股分析页 (`stock.js`) 的布局顺序是:
1. 评分卡片 → 2. 均线趋势 → 3. 多级别缠论结构 → 4. 量价分析 → 5. 完全分类 → 6. 支撑阻力 → 7. 风控仓位

### 4.1 评分卡片升级（stock.js 第 65-79 行）

**现状**: 只显示 `total_score` + 方向 + 信号数 + MA确认

**升级为双分数展示**: 左侧缠论分 + 右侧融合分 + 置信度标签

```
┌─────────────────────────────────────────────────────┐
│  [68.5]        [86.2]         方向: 偏多            │
│  缠论评分      融合评分        信号数: 5             │
│                               MA确认: 回踩MA20确认   │
│               ┌──────┐                              │
│               │ 高置信 │  ← 3维异常收敛              │
│               └──────┘                              │
└─────────────────────────────────────────────────────┘
```

- `fused_total` 作为主分数展示（字号更大）
- `total_score` 作为缠论子分数（字号略小，灰色标注"缠论"）
- 置信度标签用色标: 高=绿, 中=橙, 低=灰

### 4.2 新增"异常雷达"卡片（插在评分卡片之后，均线之前）

```
┌─────────────────────────────────────────────────────┐
│ 异常检测                                             │
│                                                      │
│ 量能   ████████████░░░░░░  z=2.3  异常放量 🔴        │
│ 振幅   ██████░░░░░░░░░░░░  z=1.1  正常    ⚪        │
│ 跳空   ████████████████░░  z=2.8  异常跳空 🔴        │
│ 实体   █████████░░░░░░░░░  z=1.5  正常    ⚪        │
│ 背离   ░░░░░░░░░░░░░░░░░░  —      无      ⚪        │
│                                                      │
│ ⚡ 2维收敛 — 量能+跳空同时异常，信号可信度提升         │
│                                                      │
│ ┌──────────────────────────────────────────────┐     │
│ │ 🩸 割肉指标: 72/100  恐慌出清                 │     │
│ │ ████████████████████████████░░░░░░░░░░       │     │
│ │ 放量(z=2.3) + 长下影线 + 缩量后突然放量       │     │
│ └──────────────────────────────────────────────┘     │
└─────────────────────────────────────────────────────┘
```

**组件设计**:

每个异常维度渲染为一行:
- 名称标签（量能/振幅/跳空/实体/背离）
- z-score 进度条（0 到 3σ 映射为进度条宽度）
- z-score 数值
- 异常标签 + 色点（🔴异常 / ⚪正常）

底部特殊区域:
- **收敛提示**: 当 ≥2 维度同时触发，用 ⚡ 高亮提示
- **割肉指标**: 独立进度条 + 分值 + 因子明细

### 4.3 Dashboard 信号列表增强（dashboard.js 第 667-728 行）

Dashboard 的 "Part 3: L3 个股信号" 区域，每只达标股票的 `.signal-row` 下方新增一行异常摘要:

```
现有:
┌────────────────────────────────────────────────────────────────┐
│ #1  宁德时代                          68.5    恐慌×1.25   偏多  │
│     SZ.300750 | 5个信号 回踩MA20确认                           │
└────────────────────────────────────────────────────────────────┘

升级后:
┌────────────────────────────────────────────────────────────────┐
│ #1  宁德时代                          86.2    恐慌×1.25   偏多  │
│     SZ.300750 | 5个信号 回踩MA20确认  (缠论68.5)               │
│     ┌─────────────────────────────────────────────────┐        │
│     │ 🔴量能z2.3  🔴跳空z2.8  ⚪振幅  ⚪实体  ⚪背离  │        │
│     │ ⚡2维收敛  🩸割肉72分                            │        │
│     └─────────────────────────────────────────────────┘        │
└────────────────────────────────────────────────────────────────┘
```

核心改动（dashboard.js `renderSignalList` 第 682-705 行）:
- 主分数从 `total_score` 改为 `fused_total`（有融合分时用融合分，无时降级为缠论分）
- 信号代码行追加 `(缠论XX.X)` 子分标注
- 新增 `.signal-anomaly-strip` 展示区:
  - 5 个维度各一个色点+名称+z值（异常=红, 正常=灰, 紧凑一行）
  - 收敛标签 + 割肉分值（仅 ≥40 时显示）

### 4.4 修改文件清单

| 文件 | 改动 |
|------|------|
| `signals/web/api/stock.py` | `analyze_stock()` 序列化新增 anomaly + fused 字段 |
| `signals/web/static/js/stock.js` | 升级评分卡片 + 新增 `renderAnomalyRadar()` 函数 |
| `signals/web/static/js/dashboard.js` | 信号列表条目追加异常指示器 |
| `signals/web/static/css/app.css` | 新增 `.anomaly-*` 系列样式 |

### 4.5 CSS 新增样式

```css
/* 异常雷达卡片 */
.anomaly-radar { /* stock-section 风格 */ }
.anomaly-row { display: flex; align-items: center; gap: 8px; height: 28px; }
.anomaly-label { width: 40px; font-size: 12px; color: var(--text-secondary); }
.anomaly-bar { flex: 1; height: 8px; background: var(--bg-tertiary); border-radius: 4px; }
.anomaly-bar-fill { height: 100%; border-radius: 4px; transition: width 0.3s; }
.anomaly-bar-fill.normal { background: var(--text-muted); }
.anomaly-bar-fill.warning { background: #f59e0b; }
.anomaly-bar-fill.anomaly { background: #ef4444; }
.anomaly-zscore { width: 50px; font-size: 11px; font-family: var(--font-mono); }
.anomaly-tag { font-size: 11px; }
.anomaly-tag.fired { color: #ef4444; }
.anomaly-tag.normal { color: var(--text-muted); }

/* 收敛提示 */
.anomaly-convergence {
  margin-top: 8px; padding: 6px 10px; border-radius: 6px;
  background: rgba(245, 158, 11, 0.1); color: #f59e0b; font-size: 12px;
}

/* 割肉指标 */
.capitulation-box {
  margin-top: 10px; padding: 10px; border-radius: 6px;
  background: var(--bg-tertiary); border: 1px solid var(--border);
}
.capitulation-score { font-size: 18px; font-weight: 700; font-family: var(--font-mono); }
.capitulation-bar { height: 6px; background: var(--bg-primary); border-radius: 3px; }
.capitulation-bar-fill { height: 100%; border-radius: 3px; background: #ef4444; }
.capitulation-detail { font-size: 11px; color: var(--text-muted); margin-top: 4px; }

/* Dashboard 异常指示器 */
.signal-anomaly-tags { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 2px; }
.signal-anomaly-tag { font-size: 10px; padding: 1px 5px; border-radius: 3px; }
.signal-anomaly-tag.fired { background: rgba(239,68,68,0.15); color: #ef4444; }
.signal-anomaly-tag.cap { background: rgba(239,68,68,0.1); color: #ef4444; }
.signal-anomaly-tag.conv { background: rgba(245,158,11,0.15); color: #f59e0b; }
```

### 4.6 API 序列化新增字段

`stock.py analyze_stock()` 返回的 JSON 新增:

```json
{
  "scored": {
    "total_score": 68.5,
    "fused_total": 86.2,
    "direction": "偏多",
    "signal_count": 5,
    "ma_confirmation": "回踩MA20确认",
    "confidence_level": "高"
  },
  "anomaly": {
    "items": [
      {"name": "volume", "z_score": 2.3, "is_anomaly": true, "label": "异常放量"},
      {"name": "range", "z_score": 1.1, "is_anomaly": false, "label": "正常"},
      {"name": "gap", "z_score": 2.8, "is_anomaly": true, "label": "异常跳空"},
      {"name": "body", "z_score": 1.5, "is_anomaly": false, "label": "正常"},
      {"name": "vol_price_div", "z_score": 0, "is_anomaly": false, "label": "无"}
    ],
    "anomaly_count": 2,
    "convergence": true,
    "capitulation_score": 72,
    "summary": "2维异常收敛(量能+跳空) | 割肉72分"
  }
}
```

---

## 验证方法

1. **单元验证**: 用一只已知的异常波动股测试 `compute_anomaly_profile()`，确认 z-score 和 anomaly 标记正确
2. **集成验证**: `python run.py --mode review --start 1w`，对比融合前后的排序差异
3. **深度验证**: Web 页面打开个股详情，确认异常维度和割肉指标正确展示
4. **回归验证**: 确保无异常数据时（bars < 30），系统降级为纯缠论评分，不报错

---

## 关键设计原则

1. **渐进增强**: anomaly 和 fused 都是 Optional，无数据时系统降级为原有逻辑
2. **零新 API**: 所有计算基于已有的日线 OHLCV，不需要新数据源
3. **独立可测**: anomaly.py 和 fusion.py 都可独立调用，不依赖缠论模块
4. **可调参数**: 所有阈值和权重都在 config.py 集中管理，方便后续校准
