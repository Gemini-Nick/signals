# 🐲 隆小侠 LONG CLAW

> **实线虚线分析框架** — 基于缠中说禅理论，构建 **指数 → 行业 → 标的** 三层联动分析系统，
> 覆盖 A 股、港股、美股，自动识别买卖点、中枢结构与背驰信号。

---

## 五大模块

| 模块 | 路径 | 功能 |
|------|------|------|
| **Core** | `signals/core/` | 信号检测引擎：一买/二买/三买/背驰买卖 + 评分系统 + 多级别共振 |
| **Data** | `signals/data/` | 多数据源统一接口：A股(AKShare/Tushare) + 港股(Futu) + 美股(IB/Alpaca/Futu/yfinance 四级降级) |
| **Layers** | `signals/layers/` | 三层联动：指数大势 → 行业强弱（双榜系统） → 个股筛选 |
| **Research** | `signals/research/` | 研报导入(MD/PDF/图片OCR) + 自动归档(notes/YYYY/MM/) + 时间衰减 |
| **Notify** | `signals/notify/` | 飞书群聊推送分析结果 |

---

## 运行模式

```bash
python run.py                                      # 盘中监测（默认）
python run.py --mode index                          # 仅指数报告（快速）
python run.py --mode review --start 2024-09-24      # 盘后复盘（指定日期）
python run.py --mode review --start 924             # 盘后复盘（日期预设）
python run.py --mode review --start ytd             # 盘后复盘（今年以来）
python run.py --list-dates                           # 查看所有日期预设
python run.py --mode import --file 研报.pdf          # 导入研究笔记
python run.py --mode intraday --industries 有色金属,半导体   # 指定行业
```

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

### A 股数据降级链

| 场景 | 降级链 |
|------|--------|
| 日线（盘后） | AKShare → Tushare(SSL降级) |
| 分钟线（盘中） | AKShare(Sina) → Tushare(限速) → Futu |
| 行业板块 | 东财(AKShare) → 自动重试 |

### 美股数据降级链

| 场景 | 降级链 |
|------|--------|
| 盘中 | IB Gateway → Futu → yfinance |
| 盘后 | Alpaca → yfinance |

通过 `create_us_source(mode)` 工厂函数自动组装，未安装的数据源自动跳过。
每个源最多重试 3 次，致命错误（依赖缺失/认证失败）立即跳过。

---

## 技术栈

| 组件 | 用途 |
|------|------|
| [czsc](https://github.com/waditu/czsc) | 缠论核心引擎（笔、段、中枢、买卖点识别） |
| [AKShare](https://github.com/akfamily/akshare) | A 股指数 / 行业 / 个股行情 |
| [Futu OpenD](https://openapi.futunn.com/) | 港股 + 美股盘中数据 |
| [yfinance](https://github.com/ranaroussi/yfinance) | 美股免费兜底（日线+分钟线） |
| [Tushare](https://tushare.pro/) | A 股补充行情（SSL降级备选） |
| [ib_async](https://github.com/ib-api-reloaded/ib_async) | IB Gateway 美股盘中优先（可选） |
| [alpaca-py](https://github.com/alpacahq/alpaca-py) | Alpaca 美股盘后优先（可选） |
| [python-dotenv](https://github.com/theskumar/python-dotenv) | 凭证安全管理 |

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
# 编辑 .env，填入你的 Tushare Token、Futu OpenD 地址等
# IB Gateway / Alpaca 为可选配置，不填则自动跳过

# 4. 运行
python run.py                          # 盘中监测
python run.py --mode index             # 仅看指数
python run.py --mode review --start 924   # 盘后复盘（924新政以来）
```

---

## 项目架构

```
🐲 隆小侠 LONG CLAW
├── run.py                  # 总入口（四种模式调度 + 交易时段路由）
├── config.py               # 全局配置（.env 凭证 + 指数/白名单/行业/日期预设）
├── .env.example            # 环境变量模板
├── requirements.txt        # Python 依赖
├── notes/                  # 研究笔记归档（YYYY/MM/ 子目录）
├── signals/
│   ├── core/               # 缠论核心引擎
│   │   ├── analyzer.py     #   分析器（笔、段、中枢）
│   │   ├── detectors.py    #   买卖点 / 背驰检测
│   │   ├── freq_utils.py   #   多周期工具
│   │   ├── market_hours.py #   交易时段判断（A+H / 美股自动路由）
│   │   └── scorer.py       #   评分系统
│   ├── layers/             # 三层联动分析
│   │   ├── index_screener.py   # Layer 1 指数筛选调度
│   │   ├── index_analyzer.py   # Layer 1 指数分析
│   │   ├── index_report.py     # Layer 1 报告输出
│   │   ├── market_context.py   # 市场环境上下文
│   │   ├── industry.py         # Layer 2 行业分析（双榜系统 + 7维评分）
│   │   ├── screener.py         # Layer 3 盘中标的筛选（三级降级链）
│   │   └── review_screener.py  # Layer 3 盘后复盘
│   ├── data/               # 多数据源统一接口
│   │   ├── fetcher.py      #   核心数据源 + USDataSource 降级链
│   │   ├── ib_source.py    #   IB Gateway 美股盘中数据源（可选）
│   │   ├── alpaca_source.py#   Alpaca 美股盘后数据源（可选）
│   │   └── us_factory.py   #   美股数据源工厂（按模式组装降级链）
│   ├── research/           # 研报子系统
│   │   └── research.py     #   多格式导入 + 自动归档 + 双维度展示
│   └── notify/             # 消息推送
│       └── feishu.py       #   飞书群聊通知
```

---

## 配置说明

核心配置项位于 `config.py`（凭证通过 `.env` 加载）：

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `INDEX_AK_CODES` | A 股指数（AKShare 格式） | 7 个主要宽基指数 |
| `INDEX_FUTU_CODES` | 港股指数（Futu 格式） | 恒生科技 |
| `INDEX_US_CODES` | 美股指数 ETF（Futu 格式） | SPY / QQQ / DIA |
| `INDEX_FREQS` | 指数分析周期 | `daily`, `30min`, `15min` |
| `INDEX_LOOKBACK_DAYS` | 日线回溯天数 | 180（≈120 交易日） |
| `WHITELIST` | 白名单标的 | 自定义 |
| `WATCH_INDUSTRIES` | 关注行业板块 | 空（跳过 Layer 2） |
| `RANK_TOP_N` | 行业双榜各取前 N 名 | 10 |
| `DATE_PRESETS` | 盘后复盘日期预设 | 15 个（相对+历史节点） |
| `MONITOR_FREQS` | 标的监控周期 | `15min`, `30min` |
| `NOTES_DIR` | 研究笔记目录 | `notes` |

---

## License

Private repository — for personal use.
