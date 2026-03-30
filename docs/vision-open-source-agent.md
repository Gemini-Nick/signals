# Signals 项目 → 个人 AI Agent → 开源缠论 Agent 平台

## Context

你目前使用 Claude Code Max Plan 开发 Signals 项目（缠论三层联动分析系统）。

**短期目标**：打造个人 AI Agent，自动化盯盘、分析、推送，通过微信/飞书/企业微信交互。
**长期目标**：先自用打磨，再开源给散户社区。

---

## 战略判断：这个项目还有必要做吗？

### 结论：不仅有必要，而且占据了一个黄金空白点

**1. 通用 Agent 不会取代领域工具**
2026 年行业趋势明确：**领域专用 Agent 完胜通用 Agent**。领域模型幻觉率降低 70-85%。金融领域的前沿架构是多个专业 Agent 协作（如 TradingAgents 框架），而不是一个通用 ChatGPT 做所有事。你的三层架构（指数→行业→标的）天然契合这个 multi-agent 范式。

**2. 缠论 + AI Agent = 市场空白**
当前开源项目格局：
| 项目 | Stars | AI集成 | 缺陷 |
|------|-------|--------|------|
| czsc (waditu) | ~4,000 | 无 (Rust引擎) | 纯算法，无智能解读 |
| chan.py (Vespa314) | ~1,500 | 无 | 纯算法，无交互 |
| chanlun-pro | 中等 | 少量ML | 半商业化 |
| 壹缠量化 | 商业 | ChatGPT问答 | 不开源 |
| **你的 Signals** | 新项目 | **Claude Agent + 缠论技能** | **唯一的 AI Agent + 缠论** |

**没有任何开源项目将 LLM Agent 与缠论分析结合。** 你的项目是唯一一个。

**3. 社区需求巨大且痛点明确**
缠论在中国有**数十万活跃实践者**（GitHub stars + 知乎 + 微信社群）。核心痛点：
- 手动画笔画段太累 → 你已解决（czsc 引擎 + 自动检测）
- 不同人画出不同结构 → AI 可以提供一致性判断
- 多级别递归太难理解 → LLM 可以用自然语言解释
- 信号延迟 → 你的盘中监测已解决
- 工具碎片化 → 你的三层一体架构已解决

**4. 你的差异化壁垒**
- **缠论 + AI 解读**：不仅检测信号，还能用 Claude 解释"为什么是一买"
- **三层联动**：从宏观到微观的完整研判链
- **多市场覆盖**：A股 + 港股 + 美股
- **消息推送**：飞书/企微/微信实时触达

---

## 项目演进三阶段

### 阶段一：个人工具 (当前 → 3个月)
打磨自用，验证 Agent 能力
- Agent SDK 封装 run.py，定时自动运行
- 飞书/企微双向交互，微信推送
- 每天用它做真实交易辅助，积累使用经验
- **关键指标**：每天自动推送有效信号 ≥ 3 次

### 阶段二：小范围内测 (3-6个月)
邀请 10-20 个缠论交易者内测
- 简化部署流程（Docker 一键启动）
- 编写用户文档和配置向导
- 收集反馈，优化信号质量
- 添加简单 Web Dashboard
- **关键指标**：内测用户日活 ≥ 50%

### 阶段三：开源发布 (6-12个月)
正式开源，面向散户社区
- GitHub 开源 + 完善 README / 文档
- 知乎/微信公众号推广
- 插件架构：允许用户自定义策略（不仅限缠论）
- 可选商业化：付费 SaaS 版（云托管 + 数据源 + 推送）
- **目标**：1,000 GitHub Stars (参考 czsc 4,000 stars)

---

## 当前工具格局（2026.03）

| 工具 | 定位 | 价格 | 你的关系 |
|------|------|------|----------|
| **Claude Code** (Max) | 交互式编码 Agent | $100-200/mo | ✅ 正在使用 |
| **Claude Agent SDK** | 编程构建自定义 Agent | API 按量 (~$5/$25/M) | 🎯 核心推荐 |
| **OpenAI Codex CLI** | 编码 Agent (GPT-5.3) | $20-200/mo | 🔧 可作辅助 |
| **OpenClaw** | 自托管全能 Agent 网关 | 免费 (自付 API) | 🌐 消息层候选 |

---

## 推荐方案：三层架构

### Layer A: Claude Code Max — 开发层 (保持现状)
- 日常 Signals 项目开发、重构、新功能
- 交互式 debug 和代码审查
- CLAUDE.md + Skills + Scheduled Tasks
- **不需要改变**，继续用

### Layer B: Claude Agent SDK — 自动化核心 (新建)
用 Python 构建你的**自主运行 Agent**：

```
signals-agent/
├── agent.py          # Claude Agent SDK 主循环
├── tools/
│   ├── run_signals.py   # 封装 run.py 的各个 mode
│   ├── market_query.py  # 查询最新信号/持仓
│   └── research.py      # 自动研报摘要
├── scheduler.py      # APScheduler 定时任务
├── channels/
│   ├── feishu_bot.py    # 飞书机器人 (已有基础)
│   ├── wecom_bot.py     # 企业微信机器人
│   └── wechat.py        # 微信 (通过 OpenClaw 或 itchat)
├── api.py            # FastAPI webhook 入口
└── config.yaml       # Agent 配置
```

**核心能力：**
1. **定时分析** — 每天 9:15 开盘前跑 `--mode index`，15:30 收盘后跑 `--mode review`
2. **信号推送** — 检测到买卖点 → 自动组织分析报告 → 推送飞书/企微/微信
3. **自然语言控制** — "跑一下今天的复盘" / "大盘现在什么情况" → 调用对应模式
4. **研报自动摘要** — 新研报入库 → Claude 总结要点 → 推送摘要
5. **Hooks 权限控制** — 写入操作需确认，读取自动放行

**为什么是 Agent SDK 而不是纯 API：**
- Agent SDK 内置 Read/Write/Edit/Bash 工具，直接操作你的代码库
- 支持 Subagent（分市场并行分析）
- Hooks 机制管理权限
- Session 持久化保持上下文

### Layer C: 消息通道层 — 微信 + 飞书 + 企业微信

#### 飞书（已有基础，最快上线）
- `signals/notify/feishu.py` 已有单向推送
- 扩展为**飞书机器人**：Event Subscription 接收消息 → FastAPI webhook → Agent SDK 处理 → 回复
- 飞书开放平台文档完善，支持富文本卡片、按钮交互

#### 企业微信（企业场景首选）
- 企业微信群机器人 Webhook（推送最简单，5分钟搞定）
- 企业微信应用消息 API（双向交互，需企业管理员权限）
- 支持 Markdown 消息格式

#### 微信（个人场景，两种方案）
- **方案 A: OpenClaw 网关** — 最成熟的微信接入方案，内置微信协议支持，但有安全风险（CVE-2026-25253），**仅内网部署**
- **方案 B: 企业微信应用** — 通过企业微信触达微信用户（推荐），无安全风险，但需要企业微信账号

**推荐优先级**: 飞书机器人 → 企业微信群机器人 → 微信（OpenClaw）

---

## Skills vs Agent SDK vs 系统 Cron — 何时用什么

| 场景 | 方案 | 需要 Claude？ | 需要你在场？ |
|------|------|:---:|:---:|
| 你在开发时想快速看大盘 | **CC Skill** (`/market-status`) | 是 (Max 订阅) | 是 |
| 每天定时跑分析+推送飞书 | **系统 cron/launchd** | 否 | 否 |
| 定时跑分析+AI 写缠论解读+推送 | **Agent SDK + cron** | 是 (API 按量) | 否 |
| 飞书/企微发消息控制 Agent | **Agent SDK + FastAPI** | 是 (API 按量) | 否 |

**核心洞察**：你的 `run.py` 已经是完整的分析+推送工具，大部分自动化只需要 cron。Agent SDK 只在你需要 Claude "动脑子"解读信号时才需要。

---

## 实施路线

### Phase 1: Skills 增强 (1-2 天) ← 最快见效
在 CC 里创建交互式技能，你在电脑前时用：

```
.claude/skills/
├── market-status/
│   └── SKILL.md    # /market-status → 跑 index 模式 + 缠论解读
├── signals-review/
│   └── SKILL.md    # /review → 跑 review 模式 + 买卖点分析
└── industry-scan/
    └── SKILL.md    # /industry → Layer 2 行业扫描 + 轮动建议
```

每个 Skill 用 Bash 调用 `python run.py --mode xxx`，拿到输出后用缠论技能解读。
**效果**：输入 `/market-status`，30 秒后看到 AI 写的大盘缠论分析。

### Phase 2: 系统 Cron 自动化 (半天)
不需要 Claude，纯系统调度：

```bash
# Mac launchd 或 crontab
15 9 * * 1-5  cd ~/Desktop/Signals && python run.py --mode index   # 开盘前
30 15 * * 1-5 cd ~/Desktop/Signals && python run.py --mode review  # 收盘后
*/30 9-15 * * 1-5 cd ~/Desktop/Signals && python run.py --mode intraday  # 盘中每30分钟
```

你的代码已有飞书推送 (`signals/notify/feishu.py`)，cron 跑完自动推送。
**效果**：不打开电脑也能收到飞书信号推送（前提：Mac 不休眠或上云）。

### Phase 3: Agent SDK — AI 解读层 (1 周)
当你想让推送内容从"原始信号数据"升级为"缠论自然语言分析"时：

```python
# signals-agent/auto_report.py
from claude_agent_sdk import query, ClaudeAgentOptions

async def generate_report():
    # 1. 跑 run.py 拿原始信号
    result = subprocess.run(["python", "run.py", "--mode", "index"], capture_output=True)

    # 2. 让 Claude 用缠论思维解读
    async for msg in query(
        prompt=f"用缠论分析以下信号数据，给出操作建议：\n{result.stdout}",
        options=ClaudeAgentOptions(allowed_tools=["Read"]),
    ):
        report = msg  # Claude 写的自然语言分析

    # 3. 推送飞书
    feishu_push(report)
```

**效果**：飞书收到的不是表格数据，而是 "上证50日线一买形成中，30分钟级别中枢上移，建议关注..." 这样的自然语言分析。

### Phase 4: 消息交互 + 上云 (2 周)
- FastAPI webhook 接收飞书/企微消息 → Agent SDK 处理 → 回复
- 飞书发 "大盘什么情况" → Agent 跑分析 → 飞书回复缠论解读
- 企业微信群机器人推送（5分钟搞定）
- 可选：OpenClaw 接入个人微信（仅内网）
- 迁移到云服务器（阿里云/腾讯云 2C4G ~¥100/月）
- Docker 容器化

### Phase 5: 多 Agent 协作 (2 周)
- Market Monitor Agent — 盘中持续监测，异动即时推送
- Analysis Agent — 深度缠论分析，生成级别报告
- Research Agent — 自动爬取板块新闻，摘要入库
- Subagent 架构通过 Agent SDK 原生支持

---

## Codex CLI 的角色

Codex CLI 适合作为**辅助工具**而非核心：

| 场景 | 用 Claude Code | 用 Codex |
|------|---------------|----------|
| 日常开发 | ✅ 主力 | |
| 代码审查 | | ✅ 内置 review agent |
| 快速原型 | | ✅ 多 agent 并行 |
| 深度重构 | ✅ 上下文理解更强 | |
| CI/CD 集成 | ✅ Agent SDK | ✅ `codex exec` |

**建议**: 安装 Codex CLI 作为代码审查辅助，但核心开发保持 Claude Code。

```bash
npm i -g @openai/codex
# 项目根目录创建 AGENTS.md（类似 CLAUDE.md）
```

---

## 成本估算（不计成本模式）

| 组件 | 月费 |
|------|------|
| Claude Code Max 20x | $200 |
| Claude API (Agent SDK) | ~$50-150 (取决于调用频率) |
| OpenAI Codex Pro (可选) | $200 |
| OpenClaw | 免费 (自托管) |
| 云服务器 (Phase 4) | ¥100-300/月 (~$15-45) |
| **总计** | **$265-595/月** |

---

## 关键文件

| 文件 | 用途 |
|------|------|
| `run.py` | 主入口，5 个模式需封装为 Agent Tools |
| `config.py` | 全局配置，Agent 需读取 |
| `signals/notify/feishu.py` | 已有推送，扩展为双向机器人 |
| `signals/layers/screener.py` | `run_loop()` 方法可适配为 Agent 轮询 |
| `signals/data/fetcher.py` | 多源数据，已有降级链 |

---

## 验证方式

1. **Phase 1 验证**: 终端运行 `python agent.py "分析一下今天大盘"`，确认自动调用 `run.py --mode index` 并返回结果
2. **Phase 2 验证**: 飞书发送 "跑复盘" → Agent 执行 → 飞书收到分析结果；定时任务按时触发
3. **Phase 3 验证**: 企业微信/微信发送指令 → 收到分析结果回复；三通道同步推送正常
4. **Phase 4 验证**: 云端 Agent 7x24 运行，盘中异动自动推送，多 Agent 并行延迟 < 3 分钟

---

## 开源准备清单（阶段三前需完成）

### 架构调整
- [ ] 将 `signals/` 打包为 pip 可安装的 Python 包
- [ ] 数据源抽象层（用户可插拔：AKShare/Tushare/Futu/自定义）
- [ ] 策略插件系统（缠论作为默认，支持自定义策略）
- [ ] Docker Compose 一键部署（Agent + 数据库 + 消息通道）

### 用户体验
- [ ] 配置向导 CLI（`signals init`）— 引导设置数据源、推送通道
- [ ] 简单 Web Dashboard（React/Vue）— 展示信号、回测、持仓
- [ ] 自然语言交互教程（"怎么问 Agent 分析大盘"）

### 社区建设
- [ ] GitHub README（中英双语）
- [ ] 知乎专栏文章（缠论 + AI Agent 系列）
- [ ] 微信群/Discord 用户社区
- [ ] 贡献者指南 CONTRIBUTING.md

### 竞品差异化定位
```
czsc/chan.py = 缠论计算引擎（底层库）
Signals = 缠论 AI Agent（上层应用）
         ↓
  "czsc 是发动机，Signals 是整车"
```

---

## 总结

| 维度 | 判断 |
|------|------|
| 还需要做吗？ | **必须做** — 通用 Agent 做不好领域分析，领域专用 Agent 是 2026 趋势 |
| 有市场吗？ | **有** — 缠论数十万实践者，无 AI Agent 工具 |
| 能开源成功吗？ | **能** — czsc 4,000 stars 证明需求存在，你补上 AI 这块拼图 |
| 最大风险？ | 信号质量 — 如果 AI 解读不准确，社区信任会崩 |
| 关键护城河？ | 缠论领域知识 + AI 自然语言解读 + 三层联动架构 |
