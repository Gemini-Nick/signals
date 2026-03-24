# 🐲 隆小侠 LONG CLAW

> 实线虚线分析框架 — 指数研判 → 行业研判 → 标的筛选

## 项目架构

```
signals/
├── core/          # 分析引擎（信号检测、评分、异常检测、信号融合）
├── data/          # 多数据源统一接口（AKShare/Futu/yfinance + 社交数据）
├── layers/        # 三层联动分析
│   ├── index      # Layer 1: 指数研判（11只指数，日线+30M+15M 三级联动）
│   ├── industry   # Layer 2: 行业研判（板块强度评分 + 超跌 + 轮动）
│   └── screener   # Layer 3: 标的筛选（白名单快扫 + 行业批扫 + 异常融合）
├── web/           # Web UI (FastAPI + TradingView SPA)
│   ├── api/       # REST API 路由（index/industry/screener/stock/social/review/backtest）
│   ├── services/  # 引擎桥接层（WebEngine + 序列化器）
│   └── static/    # 前端资源（HTML/JS/CSS）
├── research/      # 研报系统（多格式导入、自动归档、双维度展示）
└── notify/        # 飞书推送
```

## 五大模块

| 模块 | 路径 | 功能 |
|------|------|------|
| **Core** | `signals/core/` | 信号检测 + 评分 + 异常检测(anomaly) + 信号融合(fusion) + 主题发现 |
| **Data** | `signals/data/` | 多市场数据源 + 社交舆情(social_fetcher) + K线缓存(bar_cache) |
| **Layers** | `signals/layers/` | 三层联动：指数大势 → 行业强弱 → 个股筛选 |
| **Web** | `signals/web/` | FastAPI + TradingView SPA（6页 + 9路由 + 回测/复盘/社交） |
| **Research** | `signals/research/` | 研报导入(MD/PDF/图片OCR) + 自动归档(notes/YYYY/MM/) + 时间衰减 |
| **Notify** | `signals/notify/` | 飞书群聊推送分析结果 |

## 运行模式

```bash
python run.py                                    # 盘中监测（默认）
python run.py --mode index                       # 仅指数报告（快速）
python run.py --mode review --start 2024-09-24   # 盘后复盘
python run.py --mode import --file 研报.pdf       # 导入研究笔记
python run.py --mode web [--port 8000]           # Web UI + API 服务
```

## Web UI

6 页 SPA + 9 个 API 路由，TradingView 图表集成。

| 页面 | 功能 |
|------|------|
| Dashboard | 大盘方向/情绪/指数卡片/行业排行/标的信号 |
| Chart | TradingView K线 + 缠论笔段/中枢/MA叠加 |
| Stock | 个股深度分析（多级别结构/异常检测/完全分类/风控） |
| Review | 盘后复盘（异步运行/进度轮询/三层结果） |
| Backtest | 回测验证（胜率/期望/校准/MFE-MAE） |
| Analog | 历史对照（形态相似度匹配） |

API 基础路径: `http://localhost:8000/api/`

## 分支说明

| 分支 | 说明 |
|------|------|
| `main` | V1 基础版：三层联动框架初版 |
| `claude/research-us-data-eval-*` | 美股 5 大免费数据源测评（AKShare/yfinance/Stooq/Futu/东财） |
| `claude/notes-arch-refactor-*` | 研报多格式导入 + 架构模块化重构 |
| `claude/us-data-futu-yf-*` | 美股数据流集成（Futu 优先 + yfinance 兜底 + Layer 1 美股指数） |

## 指数覆盖（Layer 1）

- **A股 (7)**: 上证50、沪深300、创业板指、科创50、超大盘、中证500、中证1000
- **港股 (1)**: 恒生科技
- **美股 (3)**: 标普500(SPY)、纳斯达克(QQQ)、道琼斯(DIA)

## 缠论技能自动加载

每次 session 开始时，自动读取以下文件以加载 czsc-thinking 缠论分析技能：

- `/Users/zhangqilong/Desktop/czsc_skills/skills/czsc-thinking/SKILL.md`
- `/Users/zhangqilong/Desktop/czsc_skills/skills/czsc-thinking/references/chan-theory-core.md`
- `/Users/zhangqilong/Desktop/czsc_skills/skills/czsc-thinking/examples/usage-scenarios.md`
- `/Users/zhangqilong/Desktop/czsc_skills/skills/czsc-thinking/scripts/README.md`

加载后，所有分析默认采用缠论思维框架：先明确级别，识别结构，判断买卖点，完全分类，不预测只分析当下。

## 微信 Agent 模式（weclaw 集成）

当通过 weclaw CLI/ACP 模式接收到微信消息时，你是「隆小侠」微信分析助手。

### 处理流程

1. **内置技能**：运行 `python scripts/wechat_run.py "<消息原文>"`
   - 「行业/板块」和「复盘/盘后」直接调分析引擎（无需 Web 服务）
   - 退出码 0 = 成功，直接输出结果文本
   - 退出码 1 = 无匹配
2. **其他所有需求**：Claude Code 直接用自身能力回答（分析、回测、策略、闲聊等）

### 内置技能（直接调引擎，不依赖 Web）

| 技能 | 触发词 | 说明 |
|------|--------|------|
| 行业板块分析 | 行业、板块、排行 | 直接调 `get_industry_representatives()` |
| 盘后复盘 | 复盘、盘后、review | 直接调 `IndexScreener` + `review_stock_daily()` |

### 输出要求

- 纯文本，不要 Markdown 格式（微信不渲染）
- 控制在 2000 字以内（微信消息长度限制）
- 使用 emoji 增强可读性
- 结构化但紧凑
