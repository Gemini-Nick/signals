# Signals — 缠论三层联动智能分析系统

> 基于缠中说禅理论（Chan Theory），构建 **指数 → 行业 → 标的** 三层联动分析框架，
> 自动识别买卖点、中枢结构与背驰信号，辅助 A 股与港股投资决策。

---

## 功能特性

| 层级 | 模块 | 说明 |
|:----:|------|------|
| **Layer 1** | 指数研判 | 覆盖 7 大 A 股指数 + 恒生科技，日线 / 30min / 15min 三级联动 |
| **Layer 2** | 行业强度 | 可配置关注板块，强势行业自动提取成分股送入 Layer 3 |
| **Layer 3** | 标的筛选 | 白名单 + 行业成分股统一评分，识别一/二/三类买卖点、趋势与背驰 |

**运行模式**

- `intraday` — 盘中实时监测（默认）
- `review` — 盘后复盘，支持自定义起始日期回溯历史结构
- `index` — 仅输出指数报告，快速了解大市方向

---

## 技术栈

| 组件 | 用途 |
|------|------|
| [czsc](https://github.com/waditu/czsc) | 缠论核心引擎（笔、段、中枢、买卖点识别） |
| [AKShare](https://github.com/akfamily/akshare) | A 股指数 / 行业 / 个股行情数据 |
| [Futu OpenD](https://openapi.futunn.com/) | 港股数据（恒生科技等） |
| [Tushare](https://tushare.pro/) | 补充行情与基本面数据 |

---

## 快速开始

```bash
# 1. 克隆仓库
git clone https://github.com/Gemini-Nick/signals.git
cd signals

# 2. 安装依赖
pip install czsc akshare futu-api tushare

# 3. 配置凭证
#    编辑 config.py，填入你的 Tushare Token 和 Futu OpenD 地址

# 4. 运行
python run.py                          # 盘中监测
python run.py --mode index             # 仅看指数
python run.py --mode review --start 2024-09-24   # 盘后复盘
```

---

## 项目架构

### V1 — 初始版本（`main` 分支）

```
signals/
├── run.py                  # 总入口（三种模式调度）
├── config.py               # 全局配置（指数、白名单、监控级别）
├── signals/
│   ├── analyzer.py         # 缠论核心分析器
│   ├── detectors.py        # 买卖点 / 背驰检测器
│   ├── freq_utils.py       # 多周期工具
│   ├── scorer.py           # 评分系统
│   ├── index_analyzer.py   # Layer 1 指数分析
│   ├── index_report.py     # 指数报告输出
│   ├── index_screener.py   # 指数筛选调度
│   ├── market_context.py   # 市场环境上下文
│   ├── industry.py         # Layer 2 行业分析
│   ├── screener.py         # Layer 3 盘中标的筛选
│   ├── review_screener.py  # Layer 3 盘后复盘筛选
│   └── validate.py         # 数据校验
└── monitor/
    └── data_fetcher.py     # 数据拉取（AKShare / Futu）
```

### V2 — 架构重构版（`integrate-research-notes` 分支）

```
signals/
├── run.py
├── config.py
├── .env.example            # [新增] 环境变量模板（凭证安全化）
├── requirements.txt        # [新增] 依赖清单
├── validate.py             # [移至根目录]
├── notes/                  # [新增] 研究笔记（YYYY/MM/ 子目录）
│   └── 2026/03/
├── signals/
│   ├── core/               # 缠论核心引擎
│   │   ├── analyzer.py
│   │   ├── detectors.py
│   │   ├── freq_utils.py
│   │   └── scorer.py
│   ├── layers/             # 三层分析模块
│   │   ├── index_analyzer.py
│   │   ├── index_report.py
│   │   ├── index_screener.py
│   │   ├── market_context.py
│   │   ├── industry.py
│   │   ├── screener.py
│   │   └── review_screener.py
│   ├── data/               # 数据源适配
│   │   └── fetcher.py
│   ├── research/           # [新增] 研究笔记子系统
│   │   └── research.py
│   └── notify/             # [新增] 消息推送（飞书 / Coze）
│       └── feishu.py
```

---

## 分支说明

| 分支 | 版本 | 状态 | 说明 |
|------|:----:|:----:|------|
| `main` | V1 | 🏷️ 老版本 | 初始三层缠论分析体系，平铺结构，硬编码凭证 |
| `claude/integrate-research-notes-OEQ1J` | V2 | ✅ 最新 | 架构分包重构 + 研究笔记系统 + 凭证安全治理 |
| `claude/analyze-branches-readme-c2Em8` | — | 📝 文档 | 分支梳理与 README 编写 |

### V1 → V2 演进详情

```
 commit   日期         类型       内容
─────────────────────────────────────────────────────────────────────
 165eb44  2026-03-03   feat       三层缠论分析体系 V1（Layer 1 + Layer 3）
 be907a6  2026-03-03   feat       研究笔记多格式导入 + 双维度独立展示
 bcd9439  2026-03-04   feat       飞书 Bot — 群聊上传文件自动导入研报
 dcde70f  2026-03-04   feat       盘中/盘后模式交互式选择研究笔记
 4bdf242  2026-03-04   feat       研究笔记按年月子目录存储（notes/YYYY/MM/）
 bbe5e4d  2026-03-04   refactor   飞书 Bot → Coze 云端归档 + 本地懒加载
 2479ecb  2026-03-04   refactor   架构分包 + 项目治理（凭证安全、依赖管理）
```

### V2 关键改进

- **架构分包** — `signals/` 拆分为 `core/`、`layers/`、`data/`、`research/`、`notify/` 五个子包
- **凭证安全** — 硬编码 Token 改为 `.env` + `python-dotenv`，新增 `.env.example` 模板
- **研究笔记** — 668 行新增代码，支持多格式导入、按年月归档、盘中/盘后交互选择
- **消息推送** — 集成飞书 Bot 与 Coze 云端归档
- **依赖管理** — 新增 `requirements.txt` 正式管理依赖

---

## 配置说明

核心配置项位于 `config.py`：

| 配置 | 说明 | 默认值 |
|------|------|--------|
| `INDEX_AK_CODES` | A 股指数列表（AKShare 格式） | 上证50 / 沪深300 / 创业板指 等 7 个 |
| `INDEX_FUTU_CODES` | 港股指数（Futu 格式） | 恒生科技 |
| `INDEX_FREQS` | 指数分析周期 | `daily`, `30min`, `15min` |
| `INDEX_LOOKBACK_DAYS` | 指数日线回溯天数 | 180（≈120 交易日） |
| `WHITELIST` | 用户白名单标的 | 自定义 |
| `WATCH_INDUSTRIES` | 关注行业板块 | 空（跳过 Layer 2） |
| `MONITOR_FREQS` | 标的监控周期 | `15min`, `30min` |
| `SCORE_THRESHOLD` | 评分淘汰线 | 60 |
| `MAX_POOL_SIZE` | 标的池上限 | 50 |

---

## License

Private repository — for personal use.
