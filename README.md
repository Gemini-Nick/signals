# 🐲 隆小侠 LONG CLAW

> **实线虚线分析框架** — 基于缠中说禅理论，构建 **指数 → 行业 → 标的** 三层联动分析系统，
> 覆盖 A 股、港股、美股，自动识别买卖点、中枢结构与背驰信号。

---

## 九大模块

| 模块 | 路径 | 功能 |
|------|------|------|
| **Core** | `signals/core/` | 信号检测引擎 + 评分系统 + 跳空缺口检测 + 异常检测 + 信号融合 + 行业主题聚类 |
| **Data** | `signals/data/` | 多数据源统一接口：MongoDB + A股(AKShare) + 港股(Futu) + 美股(Futu/yfinance) + 社交舆情 |
| **Layers** | `signals/layers/` | 三层联动：指数大势 → 行业强弱（双榜+轮动） → 个股筛选 |
| **Sync** | `signals/sync/` | 数据同步引擎：6 模块定时同步 + MongoDB 持久化 + 隧道代理 + 指数退避重试 |
| **Web** | `signals/web/` | 全功能 Web UI — 6 页 SPA + 9 个 API 路由（Dashboard/Chart/Stock/Review/Backtest/Analog） |
| **Web2** | `signals/web2/` | 精简版 Web — 行业主题聚合 + MACD 回测（独立 FastAPI 应用） |
| **Research** | `signals/research/` | 研报导入(MD/PDF/图片OCR) + 自动归档(notes/YYYY/MM/) + 时间衰减 |
| **WeChat** | `signals/wechat/` | 微信 Agent 技能（weclaw + Claude Code CLI 集成） |
| **Notify** | `signals/notify/` | 飞书推送（分析结果 + 行业聚类报告） |

---

## 运行模式

```bash
python run.py                                      # 盘中监测（默认）
python run.py --mode index                          # 仅指数报告（快速）
python run.py --mode review --start 2024-09-24      # 盘后复盘（指定日期）
python run.py --mode review --start 924             # 盘后复盘（日期预设）
python run.py --mode backtest                       # 信号回测评估
python run.py --mode import --file 研报.pdf          # 导入研究笔记
python run.py --mode web --port 8000                # Web 全功能版
python run.py --mode web2 --port 8001               # Web2 精简版（行业聚类+回测）
python -m signals.sync --once                       # 数据同步（一次性全量）
python -m signals.sync --once --module index_daily   # 数据同步（单模块）
python -m signals.sync --daemon                      # 同步守护进程
```

### 模式定位

| 模式 | 定位 | 解决的问题 |
|------|------|------------|
| `intraday` | 盘中实时 | 三层联动信号推送，自动路由 A+H / 美股 |
| `index` | 快速 | 只看 11 只指数的缠论结构，不扫个股 |
| `review` | 盘后决策 | 今天有什么机会？检测信号 → 排名 → 自动存档 |
| `backtest` | 调优 | 我的信号系统准不准？评估历史信号表现 |
| `import` | 归档 | 导入 MD/PDF/图片 研究笔记 |
| `web` | 可视化 | 全功能 SPA：Dashboard + K线图表 + 个股分析 + 复盘 + 回测 + 历史对照 |
| `web2` | 轻量可视化 | 行业主题聚合（15 类主题分类）+ MACD 回测验证 |
| `sync` | 数据同步 | 6 模块定时同步到 MongoDB（指数/个股/行业/成分股） |

---

## Web UI

两个独立的 FastAPI 应用，可按需启动。

### Web — 全功能版 (端口 6006/8000)

| 页面 | 功能 |
|------|------|
| Dashboard | 大盘方向/情绪/指数卡片/行业排行/标的信号 |
| Chart | TradingView K线 + 缠论笔段/中枢/MA叠加 |
| Stock | 个股深度分析（多级别结构/异常检测/完全分类/风控） |
| Review | 盘后复盘（异步运行/进度轮询/三层结果） |
| Backtest | 回测验证（胜率/期望/校准/MFE-MAE） |
| Analog | 历史对照（形态相似度匹配） |

### Web2 — 精简版 (端口 6008/8001)

| 页面 | 功能 |
|------|------|
| 行业聚类 | 15 类主题分类 → 聚合评分 → Top 3 强势主题 + 本周趋势 |
| MACD 回测 | TradingView K线 + MACD 信号检测 + 缠论笔段中枢叠加 |

行业聚类采用**先分主题再聚合**算法：通过关键词词典将 ~450 个行业板块分类到 15 个主题（化工/周期资源/新能源/科技硬件/AI软件/消费/家居家电/医药生物/金融/地产基建/汽车交运/军工/电力公用/农牧/传媒文娱），盘中每 30 分钟自动刷新。

---

## 指数覆盖（Layer 1）

| 市场 | 指数 |
|:----:|------|
| **A股** (7) | 上证50、沪深300、创业板指、科创50、超大盘、中证500、中证1000 |
| **港股** (1) | 恒生科技 |
| **美股** (3) | 标普500 (SPY)、纳斯达克 (QQQ)、道琼斯 (DIA) |

日线 + 30min + 15min 三级联动分析，盘中模式按交易时段自动路由 A+H / 美股。

---

## 数据源架构

云端部署时，MongoDB 作为降级链首选源（由 Sync 模块定时填充），本地开发时自动跳过。

### A 股

| 场景 | 降级链 |
|------|--------|
| 指数日线 | **MongoDB** → AKShare `stock_zh_index_daily` |
| 个股日线 | **MongoDB** → AKShare |
| 分钟线（盘中） | **MongoDB** → AKShare(Sina) → Tushare(限速) → Futu |
| 行业涨幅排行 | **MongoDB** → 同花顺 THS → 东财 EM → 磁盘缓存 |
| 行业 K 线 | **MongoDB** → 东财 EM → 同花顺 THS → 磁盘缓存(JSON) |
| 行业成分股 | **MongoDB** → 东财 EM → pytdx → 磁盘缓存 |
| 概念排行 | 新浪 → 东财 → 同花顺 → 磁盘缓存 |

### 港美股

| 场景 | 降级链 |
|------|--------|
| 港股 | **MongoDB** → Futu OpenD |
| 美股盘中 | **MongoDB** → Futu → yfinance |
| 美股盘后 | yfinance |

熔断器模式：东财/THS 连续失败自动熔断，切换数据源。磁盘缓存 24h 过期（成分股 7 天）。
隧道代理：云端通过 `EM_PROXY_URL` 自动换 IP，规避东财频繁访问封禁。

---

## 信号闭环：review → backtest

```
review 每天盘后运行 ──→ 检测买卖点 ──→ 评分排名 ──→ 自动存档到 SQLite
                                                         │
                         ┌───────────────────────────────┘
                         ▼
backtest 每月运行 ──→ 读取已存档信号（≥20天前）──→ 加载前瞻K线
                         │
                         ▼
         计算 T+5/10/20 收益、MFE/MAE、胜率、盈亏比
                         │
                         ▼
             SQS 评分 ──→ 权重建议 ──→ 优化 SIGNAL_WEIGHTS
                         │
                         ▼
             下次 review 使用优化后的权重 ──→ 信号质量提升
```

---

## 微信 Agent — weclaw + Claude Code

通过 weclaw 桥接微信消息到 Claude Code CLI，实现微信端的智能分析助手「隆小侠」。

### 工作流

```
┌────────┐       ┌──────────┐       ┌──────────────────────────┐
│  微信    │──────→│  weclaw   │──────→│  Claude Code CLI          │
│  用户    │  消息  │  serve    │ fork  │                          │
└────────┘       └──────────┘       │  ① 读 CLAUDE.md           │
     ▲                               │  ② 理解用户意图            │
     │                               │  ③ 需要数据？              │
     │                               │     是 → 读代码，调引擎    │
     │                               │     否 → 直接回答          │
     │                               │  ④ 纯文本输出              │
     │            stdout             └────────────┬─────────────┘
     └────────────────────────────────────────────┘
```

没有中间层。CC 读 CLAUDE.md 知道自己的角色和引擎能力，然后自己决定怎么回答。

### 部署

```bash
# 1. 配置 weclaw
cp deploy/weclaw/config.example.json ~/.weclaw/config.json
# 编辑 cwd 为 signals 项目路径

# 2. 启动（只需这一步）
weclaw serve
```

---

## AutoDL 部署

```bash
# 拉取最新代码
git pull origin main

# 全部启动（mongo + futu + sync + web + web2）
bash deploy/autodl/start.sh

# 按需启动单个服务
bash deploy/autodl/start.sh web 6006     # 全功能版
bash deploy/autodl/start.sh web2 6008    # 精简版
bash deploy/autodl/start.sh mongo        # MongoDB
bash deploy/autodl/start.sh sync         # 同步守护进程

# 停止
bash deploy/autodl/stop.sh               # 全部停止
bash deploy/autodl/stop.sh web2          # 只停 web2
```

AutoDL 开放端口：6006 (Web) / 6008 (Web2)，通过控制台「自定义服务」访问。

### Docker Compose（完整微服务）

```bash
cd deploy && docker-compose up -d
```

| 服务 | 说明 | 端口 |
|------|------|------|
| `futu-opend` | Futu 网关 | 11111 |
| `mongo` | MongoDB 8.0（时序数据） | 27017 |
| `sync-worker` | 数据同步守护进程 | — |
| `signals` | 分析引擎 + Web | 6006 |
| `redis` | 缓存层（可选） | 6379 |

### 定时任务

| 时间 | 任务 |
|------|------|
| 09:25 | 指数快报 |
| 09:45 ~ 14:30 | 盘中五轮扫描 |
| 15:15 | 盘后复盘 |
| 16:30 | 全量数据同步 |
| 22:30 / 00:00 | 美股扫描 |
| 03:00 | MongoDB 备份（30天轮转） |
| 周日 10:00 | 行业成分股全量更新 |

---

## 项目架构

```
🐲 隆小侠 LONG CLAW
├── run.py                  # 总入口（七种模式调度 + 交易时段路由）
├── config.py               # 全局配置（凭证 + 指数/白名单/行业/轮动/日期预设）
├── signals/
│   ├── core/               # 核心引擎
│   │   ├── analyzer.py     #   缠论分析器（笔、段、中枢）
│   │   ├── detectors.py    #   买卖点 / 背驰 / 形态 / 跳空缺口检测
│   │   ├── gap_detector.py #   跳空缺口信号（突破/持续/衰竭/普通）
│   │   ├── scorer.py       #   评分系统（信号+交叉确认）
│   │   ├── backtest.py     #   信号回测引擎（存档+前瞻评估+SQS）
│   │   ├── clustering.py   #   行业主题聚类（15类分类+聚合评分）
│   │   ├── cluster_store.py#   聚类结果持久化（JSON+周历史）
│   │   ├── ma_levels.py    #   MA 均线关键位（支撑/阻力/趋势）
│   │   ├── rotation.py     #   三线轮动（科技/顺周期/消费）
│   │   ├── risk.py         #   风控模块（缠论止损+分层仓位）
│   │   ├── market_hours.py #   交易时段判断（A+H / 美股路由）
│   │   └── freq_utils.py   #   多周期工具
│   ├── layers/             # 三层联动分析
│   │   ├── index_screener.py   # Layer 1 指数筛选调度
│   │   ├── index_analyzer.py   # Layer 1 指数分析
│   │   ├── market_context.py   # 市场环境上下文（情绪+轮动+配置建议）
│   │   ├── industry.py         # Layer 2 行业分析（双榜+超跌+轮动+多源降级）
│   │   ├── screener.py         # Layer 3 盘中标的筛选
│   │   └── review_screener.py  # Layer 3 盘后复盘
│   ├── data/               # 多数据源统一接口
│   │   ├── fetcher.py      #   核心数据源 + 降级链
│   │   ├── db_source.py    #   MongoDB 数据源封装
│   │   ├── us_factory.py   #   美股数据源工厂
│   │   ├── minute_cache.py #   分钟线 SQLite 缓存
│   │   └── bar_cache.py    #   K 线缓存（磁盘 + MongoDB）
│   ├── sync/               # 数据同步引擎
│   │   ├── engine.py       #   调度引擎（ThreadPoolExecutor + 定时任务）
│   │   ├── db.py           #   MongoDB 连接管理
│   │   ├── proxy.py        #   隧道代理（东财 IP 限流规避）
│   │   ├── retry.py        #   指数退避重试装饰器
│   │   └── modules/        #   6 个同步模块（index/stock/board 日线+分钟线）
│   ├── web/                # Web 全功能版
│   │   ├── api/            #   9 个 REST API 路由
│   │   ├── services/       #   引擎桥接层
│   │   └── static/         #   前端 SPA（HTML/JS/CSS）
│   ├── web2/               # Web2 精简版
│   │   ├── app.py          #   FastAPI + lifespan 调度器
│   │   ├── api/            #   cluster + backtest API
│   │   └── static/         #   前端 SPA
│   ├── wechat/             # 微信 Agent
│   │   ├── skills.py       #   Web API 技能（行业分析 + 盘后复盘）
│   │   └── __init__.py
│   ├── research/           # 研报子系统
│   │   └── research.py     #   多格式导入 + 自动归档
│   └── notify/             # 消息推送
│       ├── feishu.py       #   飞书群聊通知
│       └── cluster_notify.py   # 行业聚类飞书推送
├── deploy/
│   ├── docker-compose.yml  # 5 服务编排（Futu + MongoDB + Sync + Web + Redis）
│   ├── init-mongo.js       # MongoDB 初始化（集合 + 索引 + TTL）
│   ├── autodl/             # AutoDL 部署（start.sh / stop.sh / gen_cache.py）
│   ├── weclaw/             # weclaw 微信桥接配置
│   └── cron/               # 定时任务（盘中扫描 + 同步 + 备份）
├── scripts/                # 辅助脚本（缓存预生成 + wechat_run.py）
└── notes/                  # 研究笔记归档（YYYY/MM/）
```

---

## 技术栈

| 组件 | 用途 |
|------|------|
| [czsc](https://github.com/waditu/czsc) | 缠论核心引擎（Rust 加速，笔/段/中枢/买卖点） |
| [FastAPI](https://fastapi.tiangolo.com/) | Web 后端框架（异步 + lifespan 调度） |
| [TradingView Lightweight Charts](https://tradingview.github.io/lightweight-charts/) | 前端 K 线图表（v4） |
| [AKShare](https://github.com/akfamily/akshare) | A 股指数 / 行业 / 个股行情 |
| [Futu OpenD](https://openapi.futunn.com/) | 港股 + 美股盘中数据 |
| [yfinance](https://github.com/ranaroussi/yfinance) | 美股免费兜底（日线+分钟线） |
| [MongoDB](https://www.mongodb.com/) | 时序数据持久化（8.0，TTL 自动清理） |
| [pymongo](https://pymongo.readthedocs.io/) | MongoDB Python 驱动 |
| [tenacity](https://tenacity.readthedocs.io/) | 重试策略（指数退避） |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 凭证安全管理 |

---

## 版本历史

| Tag | 说明 |
|-----|------|
| `v0.7.0` | 大盘上下文层 + 聚类增强 + 日期标签升级 + RSS 资讯抓取 + 回测工作台升级 |
| `v0.6.0` | MongoDB 多源降级优化 + 微信 Agent 架构完善 + weclaw ACP 模式升级 |
| `v0.5.0` | MongoDB 数据降级链首选源 + 交易模拟引擎 + 市场状态面板 |
| `v0.4.0` | MongoDB 数据同步引擎 + 跳空缺口检测 + Docker 微服务编排 |
| `v0.3.0` | Web 双站 + 行业主题聚合 + MACD 回测 + AutoDL 部署 |
| `v0.2.0` | 道长策略融合 + AutoResearch + Skills |
| `v0.1.0` | 三层联动框架 + 缠论信号引擎 |

---

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Gemini-Nick/signals.git
cd signals

# 2. 安装依赖
pip install -r requirements.txt

# 3. 配置凭证（从模板创建 .env）
cp .env.example .env
# 编辑 .env，填入你的 Futu OpenD 地址等（可选，不填用 AKShare 免费源）

# 4. 运行
python run.py --mode index             # 看指数大盘
python run.py --mode web2 --port 8001  # 启动精简版 Web
```

---

## License

Private repository — for personal use.
