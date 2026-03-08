# Signals AI 研判层：在缠论之上叠加「思考」

> 版本：v1.0 | 日期：2026-03-08
> 状态：待审批 — 等待用户研究后决定是否启动实施
> 研究起点：[nanochat](https://github.com/karpathy/nanochat) → LLM 量化应用全景 → AI 研判层设计

---

## Context

均线+缠论已经能很好地处理技术分析（级别→结构→买卖点→完全分类）。系统缺的不是更多技术指标，而是三个层面的**深度思考能力**：

1. **研判上下文理解** — 理解研报/新闻背后的逻辑链（政策→行业→个股），而非关键词提取
2. **信号决策推理** — CZSC 给出买点后，综合大势/情绪/板块/资金面推理「该不该在这个买点行动」
3. **复盘与归因** — 分析「为什么这次三级共振没兑现」「这次轮动切换的驱动因素是什么」

### 为什么不做 Alpha 因子挖掘？

研究了 AlphaAgent、RD-Agent-Quant、QuantaAlpha 等 LLM 因子挖掘框架后，认为：
- 这些框架生成的本质上是 `RANK(DELTA(CLOSE,5)) * TS_STD(VOLUME,10)` 这类价量技术因子
- 与现有的均线+缠论是同一层面的东西，并没有带来"思考"层面的提升
- 真正缺的不是更多指标，而是**对信号的综合研判能力**

---

## 架构设计：Layer 0 — AI 研判层

```
现有系统                              AI 研判层（NEW）
─────────                            ────────────────
L1 指数研判 → MarketContext    ──┐
                                ├──→  ① 上下文引擎 (Context Engine)
研报笔记 → ResearchNote        ──┘       政策传导链 / 行业逻辑 / 宏观关联

L2 行业排名 → IndustryRanking  ──┐
L3 个股筛选 → ScoredSymbol     ──┼──→  ② 决策推理 (Decision Reasoner)
L1 MarketContext               ──┘       "该不该在这个买点行动？"

盘后全部数据                    ──────→  ③ 复盘归因 (Review Analyst)
                                          "为什么没兑现？" + 经验积累
```

核心原则：**AI 不替代缠论判断，而是在缠论给出技术信号后，叠加「这个信号在当前环境下值不值得行动」的推理。**

---

## 模块一：上下文引擎 (Context Engine)

### 要解决的问题

现有 `auto_extract_info()` 只做关键词提取。看到「央行降准 50bp」只能匹配到"银行"行业。但一个有经验的交易者会推理出：

```
央行降准 50bp
  → 流动性宽松 → 利好成长股（科技/创业板）
  → 银行净息差压力 → 短期利空银行（但长期信贷量补价）
  → 地产链边际改善 → 顺周期可能启动
  → 与当前「科技领涨」轮动阶段的关系 → 可能加速科技行情 or 触发轮动切换
```

### 实现方案

```python
# signals/ai/context_engine.py (NEW)

class ContextEngine:
    """将研报/新闻转化为可操作的交易上下文"""

    async def analyze_research_note(
        self,
        text: str,
        market_context: MarketContext,       # 当前大势
        rotation_stage: str,                 # 当前轮动阶段
    ) -> ContextAnalysis:
        """
        输入：研报原文 + 当前市场状态
        输出：
          - transmission_chain: 传导链推理（政策→行业→标的）
          - affected_rotation_lines: 影响哪些轮动线
          - alignment_with_current: 与当前轮动/情绪阶段是否一致
          - actionable_implications: 可操作的含义
          - time_horizon: 影响窗口（日内/周度/月度）
        """

    async def analyze_news_event(
        self,
        event_description: str,
        market_context: MarketContext,
    ) -> ContextAnalysis:
        """处理突发事件：关税、降息、地缘政治等"""
```

**输出数据结构**：

```python
@dataclass
class ContextAnalysis:
    transmission_chain: List[str]    # ["央行降准→流动性宽松→成长股受益→科技板块"]
    affected_sectors: Dict[str, str] # {"半导体": "利好", "银行": "短空长多"}
    rotation_implication: str        # "可能加速科技领涨" / "或触发轮动切换到顺周期"
    alignment: str                   # "顺势" / "逆势" / "中性"
    time_horizon: str                # "短期(1-3日)" / "中期(1-4周)" / "长期(1-3月)"
    confidence: float                # 推理置信度
    reasoning: str                   # 完整推理过程（可展示给用户）
```

### 集成点

- 替换 `signals/research/research.py` 中的 `auto_extract_info()`
- 新增字段融入 `ResearchNote`：`transmission_chain`、`rotation_implication`
- 在 Layer 2 执行前，将上下文分析结果注入行业扫描逻辑：
  - 如果上下文分析认为「顺周期即将启动」，自动将相关行业加入扫描池
  - 如果上下文认为「科技见顶风险」，降低科技板块信号权重

---

## 模块二：决策推理 (Decision Reasoner)

### 要解决的问题

CZSC 给出「创业板指 日线+30M 二买，评分 78」。但交易者还需要判断：

- 大势环境：恐慌阶段还是修复阶段？恐慌期的二买是左侧抄底（风险高），修复期的二买是右侧确认（更安全）
- 板块配合：创业板指买点 + 科技板块领涨 = 共振。创业板指买点 + 顺周期领涨 = 可能是假信号
- 资金面：行业资金流入还是流出？龙头股还在不在？
- 研报支撑：有没有研报认同这个方向？研报观点和技术信号是否共振？
- 历史参考：类似环境下，这种信号的兑现率如何？

### 实现方案

```python
# signals/ai/decision_reasoner.py (NEW)

class DecisionReasoner:
    """在 CZSC 信号基础上，推理是否应该行动"""

    async def evaluate_signal(
        self,
        scored_symbol: ScoredSymbol,         # L3 评分结果
        market_context: MarketContext,       # L1 大势
        industry_ranking: IndustryRanking,   # L2 所属行业排名
        research_notes: List[ResearchNote],  # 相关研报
        context_analysis: ContextAnalysis,   # 上下文引擎输出
    ) -> DecisionAdvice:
        """
        综合所有维度，给出行动建议 + 完整推理链
        """

    async def evaluate_index_signal(
        self,
        index_report: IndexReport,           # 指数信号
        market_context: MarketContext,
        rotation_stage: str,
    ) -> DecisionAdvice:
        """评估指数级别的信号（如大盘买点）"""
```

**输出数据结构**：

```python
@dataclass
class DecisionAdvice:
    action: str                          # "建议行动" / "观望等待" / "谨慎参与"
    conviction: float                    # 0.0-1.0 综合置信度

    # 多维度评估
    macro_alignment: str                 # "大势支撑" / "大势压制" / "大势中性"
    sector_alignment: str                # "板块共振" / "板块分歧" / "板块逆势"
    sentiment_alignment: str             # "情绪配合" / "情绪过热" / "情绪恐慌"
    research_alignment: str              # "研报共振" / "研报冲突" / "无覆盖"
    capital_flow: str                    # "资金流入" / "资金流出" / "资金中性"

    # 关键推理
    reasoning: str                       # 完整推理过程
    key_risks: List[str]                 # ["情绪阶段偏亢奋", "行业资金已连续流入3天"]
    position_suggestion: str             # "底仓30%试探" / "弹性仓60%" / "不建议"
    stop_loss_logic: str                 # "跌破中枢下沿 xx 元止损"
```

### 核心推理 Prompt 设计

```python
DECISION_PROMPT = """
你是一位资深缠论交易者。基于以下多维度数据，判断是否应该在这个买点行动。

## 技术信号
{scored_symbol.details}
评分: {scored_symbol.total_score}
MA确认: {scored_symbol.ma_confirmation}

## 大势环境
方向: {market_context.overall_direction} (强度: {market_context.direction_strength})
情绪阶段: {market_context.sentiment_phase}
轮动: {market_context.rotation_stage}

## 板块位置
行业: {industry.name}
涨幅排名: {industry.gain_rank} / 综合排名: {industry.composite_rank}
超跌评分: {industry.oversold_score}
轮动线: {industry.rotation_line}

## 研报观点
{research_summary}

## 上下文分析
传导链: {context_analysis.transmission_chain}
轮动含义: {context_analysis.rotation_implication}

请按以下框架推理：
1. 缠论技术面：信号本身的质量（级别、共振、置信度）
2. 大势配合度：当前环境是否支持这类信号
3. 板块共振度：所属板块是否在强势方向上
4. 情绪匹配度：情绪阶段对应什么操作风格
5. 研报/事件催化：有没有基本面支撑
6. 综合判断：行动/观望/谨慎，并给出仓位建议和止损逻辑
"""
```

### 集成点

- 在 `signals/layers/screener.py` 的 L3 扫描完成后调用
- 仅对评分 >= SCORE_THRESHOLD 的标的进行 AI 推理（控制 API 成本）
- 推理结果附加到飞书卡片中，作为新面板展示

---

## 模块三：复盘归因 (Review Analyst)

### 要解决的问题

现有 review 模式只是回看历史信号和价格走势。缺乏：
- 「为什么 9/24 的三级共振信号兑现了 30% 涨幅，而 1/12 的类似信号只涨了 3%？」
- 「这波从科技切换到顺周期的驱动因素是什么？」
- 「恐慌阶段买入的胜率历史上是多少？这次有什么不同？」

### 实现方案

```python
# signals/ai/review_analyst.py (NEW)

class ReviewAnalyst:
    """盘后复盘：归因分析 + 经验积累"""

    async def analyze_signal_outcome(
        self,
        signal: SignalEvent,                  # 原始信号
        entry_context: MarketContext,         # 信号发出时的环境
        current_price: float,                 # 当前价格
        days_elapsed: int,                    # 经过天数
    ) -> SignalReview:
        """单个信号的复盘：兑现了吗？为什么？"""

    async def analyze_rotation_transition(
        self,
        from_stage: str,                      # 之前的轮动阶段
        to_stage: str,                        # 当前的轮动阶段
        transition_date: str,                 # 切换日期
        industry_rankings_before: List,       # 切换前的行业排名
        industry_rankings_after: List,        # 切换后的行业排名
        news_events: List[str],              # 期间事件
    ) -> RotationReview:
        """轮动切换归因：什么驱动了这次切换？"""

    async def generate_session_review(
        self,
        market_context: MarketContext,
        industry_rankings: List[IndustryRanking],
        scored_symbols: List[ScoredSymbol],
        context_analyses: List[ContextAnalysis],
    ) -> str:
        """生成当日完整复盘报告"""

    def accumulate_insight(self, review: SignalReview):
        """将复盘洞察存入经验数据库，供后续决策推理参考"""
```

**输出数据结构**：

```python
@dataclass
class SignalReview:
    signal: SignalEvent
    outcome: str                      # "完全兑现" / "部分兑现" / "未兑现" / "反向"
    return_pct: float                 # 信号后的收益率
    attribution: List[str]            # 归因因素：["大势修复配合", "板块龙头带动"]
    failure_factors: List[str]        # 失败因素：["情绪过热回落", "板块资金流出"]
    pattern_match: str                # 与历史哪种模式最相似
    lesson: str                       # 经验总结（一句话）
```

### 经验积累机制

复盘不是一次性的——核心价值在于**经验积累**。每次复盘的洞察存入本地数据库，供决策推理时参考：

```python
# signals/ai/memory.py (NEW)

class TradingMemory:
    """交易经验数据库"""

    def __init__(self, db_path=".data/trading_memory.db"):
        # SQLite 存储复盘洞察

    def store_insight(self, insight: SignalReview):
        """存储单次复盘洞察"""

    def query_similar_situations(
        self,
        market_context: MarketContext,
        signal_type: str,
    ) -> List[HistoricalInsight]:
        """查询历史上类似环境下的经验
        例如：恐慌阶段 + 二买 → 历史上8次，5次兑现(62.5%)
        """

    def get_pattern_statistics(self) -> dict:
        """统计各模式的兑现率"""
```

这样，Decision Reasoner 在推理时可以引用历史经验：
```
「类似环境（修复阶段+科技领涨+日线二买），历史 8 次中 5 次兑现，
  平均收益 12%，平均持有 15 天。但当前情绪比历史均值偏高，需谨慎。」
```

---

## 集成到现有流程

### 盘中模式 (intraday) 增强

```python
# run.py → run_intraday() 修改

# 现有流程不变
market_ctx = index_screener.initialize()          # L1
rankings = industry.compute_rankings()            # L2
scored = screener.scan(symbols)                   # L3

# NEW: AI 研判层叠加
ai = AIReasoningLayer(llm_client)

# 1. 上下文引擎：分析当日重要事件/研报
for note in today_notes:
    ctx = await ai.context_engine.analyze(note, market_ctx)

# 2. 决策推理：对高分标的逐个推理
for symbol in scored:
    if symbol.total_score >= SCORE_THRESHOLD:
        advice = await ai.decision_reasoner.evaluate(
            symbol, market_ctx, industry, notes, ctx
        )
        symbol.ai_advice = advice  # 附加到输出

# 3. 飞书推送：增加 AI 研判面板
card = build_card(market_ctx, rankings, scored)  # 现有
card.add_panel("AI 研判", ai_summary)             # NEW
```

### 复盘模式 (review) 增强

```python
# run.py → run_review() 修改

# 现有复盘流程
market_ctx, rankings, scored = review_pipeline(start_date)

# NEW: AI 复盘归因
review = await ai.review_analyst.generate_session_review(
    market_ctx, rankings, scored
)

# 存储经验
for signal_review in review.signal_reviews:
    ai.memory.store_insight(signal_review)

# 输出复盘报告（终端 + 飞书）
print(review.report)
feishu.send_card(review.to_card())
```

---

## 飞书输出增强

在现有飞书卡片中新增 AI 研判面板（可折叠）：

```
+-------------------------------------+
| 大盘研判  2026-03-08 14:30           |
+-------------------------------------+
| [现有 L1 指数分析]                    |
| [现有 L2 行业排名]                    |
| [现有 L3 个股筛选]                    |
+-------------------------------------+
| > AI 研判 (NEW, 可折叠)              |
|                                     |
| 今日关键事件传导链                    |
| 央行降准50bp -> 流动性宽松            |
| -> 利好科技成长，与当前科技领涨共振    |
| -> 银行短期承压，但信贷量或补价        |
|                                     |
| 信号决策建议                         |
| - 创业板指 二买 (78分)               |
|   建议行动: 大势修复+板块共振+研报支撑 |
|   仓位: 弹性仓 50%, 止损中枢下沿 2180 |
|                                     |
| - XX半导体 三买 (65分)               |
|   谨慎参与: 板块偏热, 连板高度过高     |
|   仓位: 底仓 20%, 止损均线 xx 元      |
|                                     |
| 历史参考                             |
| 修复阶段+科技领涨+二买 历史兑现率 62%  |
+-------------------------------------+
```

---

## 实施文件清单

| 文件 | 类型 | 说明 |
|------|------|------|
| `signals/ai/__init__.py` | NEW | AI 研判层包 |
| `signals/ai/context_engine.py` | NEW | 上下文引擎：研报/事件→传导链推理 |
| `signals/ai/decision_reasoner.py` | NEW | 决策推理：信号+环境→行动建议 |
| `signals/ai/review_analyst.py` | NEW | 复盘归因：信号→结果→经验 |
| `signals/ai/memory.py` | NEW | 交易经验数据库 (SQLite) |
| `signals/ai/llm_client.py` | NEW | LLM 调用封装 (Claude/DeepSeek) |
| `signals/ai/prompts.py` | NEW | Prompt 模板集中管理 |
| `signals/research/research.py` | MODIFY | `auto_extract_info()` → 接入上下文引擎 |
| `signals/layers/screener.py` | MODIFY | L3 扫描后调用决策推理 |
| `signals/notify/feishu.py` | MODIFY | 增加 AI 研判面板 |
| `run.py` | MODIFY | intraday/review 模式接入 AI 层 |
| `config.py` | MODIFY | 增加 AI 相关配置（API key, 开关） |

---

## 技术选型

| 组件 | 方案 | 理由 |
|------|------|------|
| **推理 LLM** | Claude Sonnet (claude-sonnet-4-6) | 推理质量高，中文表现好，Tool Use 成熟 |
| **廉价批量任务** | DeepSeek-V3 | 研报解析等批量任务降本 |
| **经验数据库** | SQLite (.data/trading_memory.db) | 与现有 minute_cache.db 一致 |
| **LLM 调用方式** | 直接 anthropic SDK / openai SDK | 场景简单，不需要 LangChain/LlamaIndex |

---

## 实施优先级

```
Phase 1 (1 周): 上下文引擎
├── llm_client.py + prompts.py (基础设施)
├── context_engine.py (研报→传导链推理)
├── 替换 auto_extract_info()
└── 飞书卡片增加「事件传导链」面板

Phase 2 (1 周): 决策推理
├── decision_reasoner.py
├── 集成到 screener.py (高分标的自动推理)
├── 飞书卡片增加「信号决策建议」面板
└── 可选：交互式查询（命令行问答）

Phase 3 (1 周): 复盘归因
├── review_analyst.py + memory.py
├── 集成到 review 模式
├── 经验积累机制
└── 决策推理引用历史经验
```

## 成本估算

| 场景 | 频率 | 估算 token | 月度成本 |
|------|------|-----------|----------|
| 研报上下文分析 | ~50篇/月 | ~2K/篇 | ~$3 (DeepSeek) |
| 信号决策推理 | ~10次/日 x 22天 | ~3K/次 | ~$15 (Claude Sonnet) |
| 盘后复盘 | 22次/月 | ~5K/次 | ~$8 (Claude Sonnet) |
| **月度合计** | | | **~$26** |

## 验证方式

1. Phase 1 完成后：对比「关键词提取」vs「LLM 上下文分析」在历史研报上的准确度
2. Phase 2 完成后：回测高分信号 + AI 建议行动 vs 无 AI 的兑现率差异
3. Phase 3 完成后：复盘报告质量人工评估 + 经验库查询准确度

---

## 研究背景（附录）

本方案源自对以下项目的深度调研：

| 项目 | 结论 |
|------|------|
| [nanochat](https://github.com/karpathy/nanochat) | 最小化 LLM 训练框架，设计模式（Task评估/FastAPI UI/Single Dial配置）可参考 |
| [AlphaAgent](https://github.com/RndmVariableQ/AlphaAgent) | LLM 因子挖掘，但产出仍是技术指标层面，非"思考" |
| [RD-Agent-Quant](https://github.com/microsoft/RD-Agent) | 微软因子+模型联合优化，适合长期方向 |
| [TradingAgents-CN](https://github.com/hsliuping/TradingAgents-CN) | A股多Agent分析，架构参考 |
| [D.E. Shaw LLM架构](https://resonanzcapital.com/insights/ai-use-by-hedge-funds-made-tangible-from-lego-bots-to-alpha-assistants) | Gateway+DocLab+Agent 三层，可精简复制 |
| StockBench / FINSABER | LLM 自主交易未能稳定跑赢 buy-and-hold，应作为研究加速器 |
