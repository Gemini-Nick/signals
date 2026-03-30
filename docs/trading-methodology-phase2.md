# 道长策略方法论 Phase 2：从信号系统到交易决策系统（全栈）

## Context

### Phase 1 已实现（2026.3.6）
均线关键位、板块超跌、交叉确认、三线轮动、经典形态、底仓弹性分层、逆向标签、配置比例 — 全部完成。

### Phase 2 驱动（2026.3.9 道长新复盘）
| 道长原话 | 能力缺口 | 后端 | 前端 |
|---------|---------|------|------|
| "这里不是低点，是情绪博弈点" | 评分不感知情绪 | scorer.py | 信号卡片显示情绪标签 |
| "不破2917就有反复" | MA只展示数字 | ma_levels.py | 指数卡片新增情景分叉面板 |
| "化工要休息，周期股要有节奏" | 无疲劳检测 | **新** sector_rhythm.py | 行业表新增节奏列+兑现提醒 |
| "科技连跌后有高低切换" | 无风格切换 | market_context.py | Banner 新增风格切换提示 |
| 深证成指2020.11验证 | 无历史匹配 | **新** analog_matcher.py | **新** 历史对照 Tab 页 |
| 减化工+加港股+降仓位 | 无汇总决策 | market_context.py | **新** 决策简报面板 |
| 轮动减速期注意兑现 | 轮动无持续/速度 | rotation.py | 轮动面板加进度条+速度标签 |

---

## 现有前端架构概要

```
index.html
├── Page 1: Dashboard (市况总览)
│   ├── .banner          → 大盘方向+情绪+建议 (MarketContext)
│   ├── .cards-grid      → 指数卡片×11 (IndexReport[])
│   ├── #industry-section → 行业全景表+超跌+概念 (IndustryRanking[])
│   ├── #sentiment-section → 情绪+轮动+配置+攻防 (MarketContext)
│   ├── #action-section   → 操作建议8板块 (ActionSummary)
│   └── #signal-section   → 今日信号(指数+行业+个股)
├── Page 2: Chart (图表)
│   └── TradingView LightweightCharts v4 (K线+笔+中枢+信号+MA+MACD)
└── Page 3: Stock (个股分析)
    └── 搜索→StockDeepDive全分析
```

API 层：
- `/api/index/context` → MarketContext
- `/api/index/reports` → IndexReport[]
- `/api/index/summary` → ActionSummary (engine.get_action_summary())
- `/api/industry/ranking` → gain_list + composite_list + oversold + concepts + stats
- `/api/screener/results` → ScoredSymbol[]
- `/api/chart/{symbol}?freq=` → OHLCV + 笔 + 中枢 + 信号 + MA + MACD
- `/api/stock/analyze/{symbol}` → StockDeepDive

---

## 七项优化（后端+前端一体化）

---

### P3-1: 情绪感知评分

#### 后端
**文件：** `signals/core/scorer.py`

`score_signals()` 新增 `sentiment_phase` 参数，在 base×freq×decay 后乘以情绪系数：
```python
_SENTIMENT_BUY_MULT = {"恐慌": 1.25, "修复": 1.10, "回落": 1.00, "亢奋": 0.80, "未知": 1.00}
_SENTIMENT_SELL_MULT = {"恐慌": 0.75, "修复": 0.90, "回落": 1.10, "亢奋": 1.25, "未知": 1.00}
```

**穿透：** `run.py` → `screener.scan_once(symbols, market_context=ctx)` → `score_signals(..., sentiment_phase=ctx.sentiment_phase)`

#### 前端

**serializers.py：** `serialize_scored_symbol()` 新增 `sentiment_tag` 字段：
```python
"sentiment_tag": "恐慌×1.25"  # 或 "" if 未知
```

**dashboard.js - renderSignalList()：** 个股信号行增加情绪标签 badge：
```html
<span class="badge sentiment-boost">恐慌×1.25</span>  <!-- 绿色=加分 -->
<span class="badge sentiment-penalty">亢奋×0.80</span> <!-- 红色=减分 -->
```

**app.css：** 新增 `.badge.sentiment-boost` (绿底) 和 `.badge.sentiment-penalty` (红底) 样式。

---

### P3-2: 关键价位情景分叉

#### 后端
**文件：** `signals/core/ma_levels.py`

新增 `ScenarioBranch` 数据类 + `build_scenario_branches(ctx, custom_levels)` 函数，从 MAContext.key_levels 中 distance_pct < 5% 的生成 IF/THEN。

**集成：**
- `IndexReport`（index_report.py）新增 `scenario_branches: list`
- `index_analyzer.py` 的 `report()` 调用 `build_scenario_branches(ma_ctx)`
- `config.py` 新增 `CUSTOM_KEY_LEVELS: dict = {}`

#### 前端

**serializers.py - serialize_index_report()：** 新增 `scenarios` 数组：
```python
"scenarios": [{
    "level_name": "5周线",
    "level_price": 2970.0,
    "distance_pct": -2.5,
    "is_support": True,
    "urgency": "接近",
    "hold": "反弹延续,关注科技轮动",
    "break": "中期走弱,切防守降仓",
}]
```

**dashboard.js - renderIndexCards()：** 指数卡片内部新增可折叠的情景分叉面板：
```html
<div class="card-scenarios" data-expanded="false">
  <div class="scenario-toggle">🔀 情景分叉 <span class="urgency-dot urgent"></span></div>
  <div class="scenario-body">
    <div class="scenario-branch support">
      IF 守住 2970 (5周线) → 反弹延续,关注科技轮动
    </div>
    <div class="scenario-branch break">
      IF 跌破 2970 (5周线) → 中期走弱,切防守降仓
    </div>
  </div>
</div>
```

只对有 urgency=="接近" 的指数显示，折叠状态默认收起，urgency=="接近" 时自动展开。

**app.css：** 新增 `.card-scenarios`, `.scenario-branch.support` (绿左边框), `.scenario-branch.break` (红左边框), `.urgency-dot` (红/黄/灰圆点)。

---

### P3-3: 板块节奏检测（兑现信号）

#### 后端
**新文件：** `signals/core/sector_rhythm.py`

```python
@dataclass
class SectorRhythm:
    name: str
    rhythm_score: float     # 0-100
    phase: str              # "启动"/"加速"/"高潮"/"衰竭"/"休整"
    consecutive_up: int
    gain_from_low: float    # %
    rsi14: float
    volume_trend: str       # "放量"/"缩量"/"持平"
    action_hint: str        # "可加仓"/"持有"/"兑现"/"回避"
    detail: str

def compute_sector_rhythm(name: str, bars: List[RawBar]) -> Optional[SectorRhythm]:
    # 四维评分：连涨天数(25) + RSI超买(25) + 距低点涨幅(25) + 量能衰减(25)
```

**集成：**
- `industry.py` 的 `get_industry_representatives()` 中顺带计算 rhythm（复用已有K线数据）
- `IndustryRanking` 新增 `rhythm_phase`, `rhythm_score`, `rhythm_hint` 字段

#### 前端

**API - industry.py `/api/industry/ranking`：** 每个行业返回新增字段：
```python
"rhythm_phase": "衰竭",
"rhythm_score": 89,
"rhythm_hint": "兑现",
```

**dashboard.js - renderIndustryPanorama()：** 行业全景表新增"节奏"列：
```html
<th>节奏</th>
...
<td class="rhythm rhythm-exhaust">衰竭 89</td>  <!-- 红色背景 -->
<td class="rhythm rhythm-peak">高潮 72</td>      <!-- 橙色背景 -->
<td class="rhythm rhythm-accel">加速 35</td>     <!-- 绿色背景 -->
```

**dashboard.js：** 行业板块上方新增"兑现提醒"横幅（rhythm_phase 为"衰竭"或"休整"时显示）：
```html
<div class="rhythm-alert">
  ⏰ 兑现提醒: 石油化工(衰竭89) · 化学原料(高潮72) — 注意板块节奏
</div>
```

**app.css：** `.rhythm` 列样式 + `.rhythm-alert` 横幅（黄底警告色）+ phase 对应颜色映射：
- 启动=青绿, 加速=绿, 高潮=橙, 衰竭=红, 休整=灰

---

### P3-4: 轮动持续时间 & 速度

#### 后端
**文件：** `signals/core/rotation.py`

`RotationStage` 新增字段：
```python
duration_days: int = 0        # 连续领涨天数
velocity: str = "稳定"         # "加速"/"稳定"/"减速"
peak_warning: bool = False    # duration>10 且 减速
peak_detail: str = ""
```

新增 `_load_rotation_history()`, `_save_rotation_snapshot()`, `_calc_duration_velocity()` 三个函数，用 `.data/cache/rotation_history.json` 持久化快照。

#### 前端

**serializers.py - serialize_market_context()：** 新增字段：
```python
"rotation_duration": 12,
"rotation_velocity": "减速",
"rotation_peak_warning": True,
"rotation_peak_detail": "科技已领涨12日,占比50%→35%,注意轮动切换",
```

**dashboard.js - renderSentimentSection()：** 轮动面板改造：

现有的轮动显示（文字描述）改为可视化组件：
```html
<div class="rotation-panel">
  <div class="rotation-stage">💡 科技领涨</div>
  <div class="rotation-meta">
    <span class="rotation-duration">📅 12天</span>
    <span class="rotation-velocity decel">⬇ 减速</span>
  </div>
  <div class="rotation-progress">
    <!-- 进度条：占比可视化 -->
    <div class="progress-bar tech" style="width:38%">科技 38%</div>
    <div class="progress-bar cycle" style="width:30%">顺周期 30%</div>
    <div class="progress-bar consumer" style="width:15%">消费 15%</div>
  </div>
  <div class="rotation-warning" v-if="peak_warning">
    ⚠️ 科技领涨进入减速期,注意轮动切换
  </div>
</div>
```

**app.css：** `.rotation-panel`, `.rotation-progress` (flex bar), `.rotation-velocity.accel`(绿) / `.decel`(红) / `.stable`(灰), `.rotation-warning`(黄底)。进度条颜色：科技=蓝, 顺周期=橙, 消费=绿。

---

### P3-5: 高低风格切换检测

#### 后端
**文件：** `signals/layers/market_context.py`

新增 `StyleSwitch` 数据类 + `detect_style_switch(reports)` 函数。

需要 `IndexReport` 新增 `recent_5d_return: float`（在 index_analyzer.py 从已有 daily_bars 计算）。

`MarketContext` 新增 `style_switch` 字段。

#### 前端

**serializers.py - serialize_market_context()：** 新增：
```python
"style_switch": {
    "detected": True,
    "direction": "低切高",
    "evidence": "科创50连跌5日后今日+2.3%, 上证50-0.5%",
    "confidence": "强",
    "suggestion": "关注超跌科技股反弹机会",
} or None
```

**dashboard.js - renderBanner()：** Banner 区域新增风格切换提示条（检测到时才显示）：
```html
<div class="style-switch-alert">
  🔄 风格切换: 低切高 — 科创50连跌5日后今日+2.3%
  <span class="switch-suggestion">→ 关注超跌科技股反弹机会</span>
</div>
```

插入位置：在现有 Banner 的 position_suggestion 下方。

**app.css：** `.style-switch-alert`（蓝底渐变条，图标动画）。

---

### P3-6: 历史形态匹配（独立模式）

#### 后端
**新文件：** `signals/core/analog_matcher.py`

```python
@dataclass
class HistoricalAnalog:
    match_start: str; match_end: str; index_name: str
    similarity: float; window_days: int
    next_10d_return: float; next_30d_return: float
    what_happened: str; key_observation: str

def find_analogs(current_bars, history_bars, window=30, top_k=3) -> List[HistoricalAnalog]:
    # Pearson 相关系数匹配收益率序列
```

**独立运行：** `run.py --mode analog [--symbol 沪深300]`，自行加载1500天数据，结果缓存到 `.data/cache/analog_latest.json`。

**config.py：** `ANALOG_LOOKBACK_DAYS=1500, ANALOG_WINDOW=30, ANALOG_TOP_K=3, ANALOG_MIN_SIMILARITY=0.70, ANALOG_INDICES=["沪深300","创业板指","上证50"]`

#### 前端

**新 API 端点 - `signals/web/api/analog.py`：**
```python
@router.get("/run/{index_name}")     # 触发单指数匹配（异步执行）
@router.get("/results")               # 获取最新缓存结果
@router.get("/chart/{index_name}")    # 获取匹配的历史K线（用于叠加显示）
```

**index.html：** 导航栏新增第4个 Tab：**历史对照**
```html
<button class="tab-btn" data-page="analog-page">📊 历史对照</button>
```

**新文件 `signals/web/static/js/analog.js`：** 历史对照页面逻辑：
```
Page 4: 历史对照 (Historical Analog)
├── 指数选择器 (沪深300 / 创业板指 / 上证50) + [运行匹配] 按钮
├── 匹配结果卡片×3 (Top3)
│   ├── 相似度 + 匹配区间
│   ├── 后续走势: 10日/30日收益率
│   └── 关键观察 (文字)
├── 叠加图表 (TradingView)
│   ├── 当前走势 (实线)
│   └── 历史匹配走势 (虚线叠加，可切换显示哪个匹配)
└── 匹配说明 (算法简介)
```

图表叠加实现：用 TradingView `addLineSeries()` 叠加历史走势（归一化后的收益率曲线），虚线样式 `lineStyle: LineStyle.Dashed`。

**app.css：** `.analog-page`, `.analog-card` (匹配结果卡片), `.similarity-badge` (相似度圆形标签)。

---

### P3-7: 决策简报

#### 后端
**文件：** `signals/layers/market_context.py`

`MarketContext` 新增 `to_decision_brief()` 方法，返回结构化 dict（非纯文本）：
```python
def build_decision_brief(self) -> dict:
    return {
        "date": "2026-03-09",
        "direction": self.overall_direction,
        "sentiment": self.sentiment_phase,
        "key_scenarios": [...],      # 来自 P3-2 urgency=="接近" 的
        "style_switch": {...} or None,  # P3-5
        "rotation_status": {...},    # P3-4 duration+velocity+warning
        "rhythm_alerts": [...],      # P3-3 衰竭/休整的板块
        "analog_ref": {...} or None, # P3-6 缓存结果
        "action_items": [...],       # 3条操作建议（自动生成）
    }
```

操作建议自动生成规则：
- rhythm 衰竭/休整 → "XX减持兑现 (衰竭+高位+缩量)"
- peak_warning → "XX领涨减速,回踩均线再加"
- style_switch → 切换方向建议
- oversold + panic → "超跌关注,恐慌释放后分批"

**engine.py：** `get_decision_brief()` 新方法，调用 `ctx.build_decision_brief()`。

**新 API：** `/api/index/brief` → decision_brief JSON。

#### 前端

**dashboard.js：** 决策简报作为 Dashboard 最顶部的 **新首屏面板**（在 Banner 之上或替代 Banner 的功能扩展）：

```html
<div class="decision-brief">
  <div class="brief-header">
    🐲 决策简报 <span class="brief-date">2026-03-09</span>
    <span class="brief-tag bearish">偏空·恐慌</span>
  </div>

  <div class="brief-body">
    <!-- 关键判断 -->
    <div class="brief-section scenarios">
      <div class="brief-label">📍 关键判断</div>
      <div class="scenario-line hold">IF 守住 2970 → 震荡格局,关注科技轮动</div>
      <div class="scenario-line break">IF 跌破 2970 → 趋势走弱,切防守降仓</div>
    </div>

    <!-- 风格+轮动 (并排) -->
    <div class="brief-row">
      <div class="brief-section style" v-if="style_switch">
        🔄 低切高 — 科创超跌反弹(+2.3%)
      </div>
      <div class="brief-section rotation">
        📅 科技领涨12日(减速) ⚠️
      </div>
    </div>

    <!-- 兑现+历史 (并排) -->
    <div class="brief-row">
      <div class="brief-section rhythm">
        ⏰ 石油化工(衰竭89) 化学原料(高潮72)
      </div>
      <div class="brief-section analog" v-if="analog_ref">
        📊 与2020.11深证成指相似85%, 后30日+12.3%
      </div>
    </div>

    <!-- 操作建议 -->
    <div class="brief-section actions">
      <div class="brief-label">💡 操作</div>
      <ol class="action-list">
        <li>化工减持兑现 (衰竭+高位+缩量)</li>
        <li>科技持有不追高 (领涨减速,回踩5周线再加)</li>
        <li>港股超跌关注 (恐慌释放后分批布局)</li>
      </ol>
    </div>
  </div>
</div>
```

**dashboard.js - loadDashboard()：** 在 Step 1（加载L1数据）之后立即请求 `/api/index/brief`，渲染决策简报面板。

**app.css：** `.decision-brief`（深色卡片，渐变边框，圆角），`.brief-tag.bullish/bearish/neutral`，`.brief-section`，`.brief-row`（flex 两列），`.action-list`（有序列表，圆点标号）。

---

## 文件改动完整清单

### 后端 (Python)

```
Phase A (核心逻辑, 可并行):
  1. signals/core/scorer.py          → sentiment_phase 乘数
  2. signals/core/ma_levels.py       → ScenarioBranch + build_scenario_branches()
  3. signals/core/rotation.py        → duration/velocity/peak_warning + JSON持久化
  4. config.py                       → CUSTOM_KEY_LEVELS + ANALOG_* 配置

Phase B (新模块):
  5. NEW signals/core/sector_rhythm.py   → 板块节奏四维评分
  6. NEW signals/core/analog_matcher.py  → 历史形态匹配(独立模式)
  7. signals/layers/market_context.py    → StyleSwitch + build_decision_brief()

Phase C (集成穿透):
  8. signals/layers/industry.py       → IndustryRanking + rhythm 字段
  9. signals/layers/index_report.py   → + scenario_branches + recent_5d_return
  10. signals/layers/index_analyzer.py → 调用 build_scenario_branches()
  11. signals/layers/screener.py      → scan_once() 加 market_context 参数
  12. run.py                          → 穿透 sentiment + --mode analog
```

### API 层 (Python)

```
  13. signals/web/services/serializers.py → 新增序列化字段:
      - serialize_scored_symbol: +sentiment_tag
      - serialize_index_report: +scenarios
      - serialize_market_context: +rotation_duration/velocity/peak, +style_switch
  14. signals/web/services/engine.py     → +get_decision_brief()
  15. signals/web/api/index.py           → +/api/index/brief 端点
  16. NEW signals/web/api/analog.py      → /api/analog/* 端点
  17. signals/web/app.py                 → 注册 analog blueprint
```

### 前端 (HTML/JS/CSS)

```
  18. signals/web/static/index.html      → +决策简报面板 + 历史对照Tab + analog-page
  19. signals/web/static/js/dashboard.js → +renderDecisionBrief() + 改 renderBanner()
                                           + 改 renderIndexCards() (scenario折叠)
                                           + 改 renderIndustryPanorama() (rhythm列+提醒)
                                           + 改 renderSentimentSection() (轮动进度条)
  20. NEW signals/web/static/js/analog.js → 历史对照页面 (匹配卡片+叠加图表)
  21. signals/web/static/css/app.css     → 全部新组件样式
  22. signals/web/static/css/themes.css  → 新组件的双主题变量
```

**总计：22个文件（4新建 + 18修改）**

---

## 依赖关系

```
Phase A (后端核心, 互相独立):
  A1(scorer) ──────────────────────────┐
  A2(ma_levels+scenarios) ─────────────┤
  A3(rotation velocity) ───────────────┤
  A4(config) ──────────────────────────┤
                                       │
Phase B (新模块, 互相独立):            │
  B5(sector_rhythm) ───────────────────┤
  B6(analog_matcher) ──────────────────┤  (独立模式)
  B7(market_context: style+brief) ─────┤
                                       │
Phase C (集成):                        ↓
  C8-C12(后端集成) ───→ C13-C17(API层) ───→ C18-C22(前端)
```

A 层和 B 层共7个模块全部可并行开发。C 层依赖 A+B 完成后串行集成。

---

## 实施顺序建议

```
Step 1: 后端 Phase A (4个文件并行改)
Step 2: 后端 Phase B (3个文件并行改)
Step 3: 后端 Phase C 集成 (industry/index_report/screener/run.py)
Step 4: API 层 (serializers + engine + 路由)
Step 5: 前端 HTML 骨架 (index.html 结构)
Step 6: 前端 JS 逻辑 (dashboard.js 改造 + analog.js 新建)
Step 7: 前端 CSS 样式 (app.css + themes.css)
Step 8: 端到端验证
```

---

## 验证方案

### 后端验证
```bash
python run.py --mode index              # 情景分叉输出 + 轮动速度
python run.py --mode intraday           # 决策简报 + 节奏提醒 + 风格切换
python run.py --mode analog             # 历史匹配独立运行
python run.py --mode review --start 924 # 924恐慌期回测
```

### 前端验证
1. Dashboard 首屏：决策简报面板渲染正确
2. Banner：风格切换提示条（检测到时显示）
3. 指数卡片：情景分叉折叠面板（urgency==接近时自动展开）
4. 行业表：节奏列 + 兑现提醒横幅
5. 轮动面板：进度条 + 持续天数 + 速度标签 + 警告
6. 信号列表：情绪乘数 badge
7. 历史对照 Tab：匹配卡片 + 叠加图表
8. 双主题验证：anthropic / tradingview 两套主题都正确
