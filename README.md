# 🐲 隆小侠 LONG CLAW

> **实线虚线分析框架** — 基于缠中说禅理论，构建 **指数 → 行业 → 标的** 三层联动分析系统，
> 覆盖 A 股、港股、美股，自动识别买卖点、中枢结构与背驰信号。

---

## 五大模块

| 模块 | 路径 | 功能 |
|------|------|------|
| **Core** | `signals/core/` | 信号检测引擎：一买/二买/三买/背驰买卖 + 评分系统 + 多级别共振 |
| **Data** | `signals/data/` | 多市场数据源：A股(AKShare) + 港股(Futu) + 美股(Futu优先/yfinance兜底) |
| **Layers** | `signals/layers/` | 三层联动：指数大势 → 行业强弱 → 个股筛选 |
| **Research** | `signals/research/` | 研报导入(MD/PDF/图片OCR) + 自动归档(notes/YYYY/MM/) + 时间衰减 |
| **Notify** | `signals/notify/` | 飞书群聊推送分析结果 |

---

## 运行模式

```bash
python run.py                                      # 盘中监测（默认）
python run.py --mode index                          # 仅指数报告（快速）
python run.py --mode review --start 2024-09-24      # 盘后复盘
python run.py --mode import --file 研报.pdf          # 导入研究笔记
python run.py --mode import --file 锂电池.pdf --source 中信证券 --author 张三
python run.py --mode intraday --industries 有色金属,半导体   # 指定行业
```

---

## 指数覆盖（Layer 1）

| 市场 | 指数 |
|:----:|------|
| **A股** (7) | 上证50、沪深300、创业板指、科创50、超大盘、中证500、中证1000 |
| **港股** (1) | 恒生科技 |
| **美股** (3) | 标普500 (SPY)、纳斯达克 (QQQ)、道琼斯 (DIA) |

日线 + 30min + 15min 三级联动分析。

---

## 技术栈

| 组件 | 用途 |
|------|------|
| [czsc](https://github.com/waditu/czsc) | 缠论核心引擎（笔、段、中枢、买卖点识别） |
| [AKShare](https://github.com/akfamily/akshare) | A 股指数 / 行业 / 个股行情 |
| [Futu OpenD](https://openapi.futunn.com/) | 港股 + 美股数据 |
| [yfinance](https://github.com/ranaroussi/yfinance) | 美股数据兜底 |
| [Tushare](https://tushare.pro/) | 补充行情与基本面 |
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

# 4. 运行
python run.py                          # 盘中监测
python run.py --mode index             # 仅看指数
python run.py --mode review --start 2024-09-24   # 盘后复盘
```

---

## 项目架构

```
🐲 隆小侠 LONG CLAW
├── run.py                  # 总入口（四种模式调度）
├── config.py               # 全局配置（.env 凭证 + 指数/白名单/监控级别）
├── validate.py             # 数据校验
├── .env.example            # 环境变量模板
├── requirements.txt        # Python 依赖
├── notes/                  # 研究笔记归档（YYYY/MM/ 子目录）
│   └── 2026/03/
├── signals/
│   ├── core/               # 缠论核心引擎
│   │   ├── analyzer.py     #   分析器（笔、段、中枢）
│   │   ├── detectors.py    #   买卖点 / 背驰检测
│   │   ├── freq_utils.py   #   多周期工具
│   │   └── scorer.py       #   评分系统
│   ├── layers/             # 三层联动分析
│   │   ├── index_screener.py   # Layer 1 指数筛选调度
│   │   ├── index_analyzer.py   # Layer 1 指数分析
│   │   ├── index_report.py     # Layer 1 报告输出
│   │   ├── market_context.py   # 市场环境上下文
│   │   ├── industry.py         # Layer 2 行业分析
│   │   ├── screener.py         # Layer 3 盘中标的筛选
│   │   └── review_screener.py  # Layer 3 盘后复盘
│   ├── data/               # 多数据源统一接口
│   │   └── fetcher.py      #   Tushare / AKShare / Futu / yfinance
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
| `MONITOR_FREQS` | 标的监控周期 | `15min`, `30min` |
| `NOTES_DIR` | 研究笔记目录 | `notes` |

---

## 版本演进

```
版本   日期         里程碑
───────────────────────────────────────────────────────────────
V1     2026-03-03   三层缠论分析体系初版（A股 + 港股）
V2     2026-03-04   架构分包重构 + 研究笔记系统 + 凭证安全治理
V3     2026-03-04   项目改名 🐲 隆小侠 + 美股数据流集成（Futu + yfinance）
```

### 详细 Commit 历史

```
 commit   类型       内容
─────────────────────────────────────────────────────────────────────
 165eb44  feat       三层缠论分析体系 V1（Layer 1 + Layer 3）
 be907a6  feat       研究笔记多格式导入 + 双维度独立展示
 bcd9439  feat       飞书 Bot — 群聊上传文件自动导入研报
 dcde70f  feat       盘中/盘后模式交互式选择研究笔记
 4bdf242  feat       研究笔记按年月子目录存储（notes/YYYY/MM/）
 bbe5e4d  refactor   飞书 Bot → Coze 云端归档 + 本地懒加载
 2479ecb  refactor   架构分包 + 项目治理（凭证安全、依赖管理）
 aa58b55  feat       美股数据流集成 — Futu 优先 + yfinance 兜底
 40f1ad8  chore      项目改名 🐲 隆小侠 LONG CLAW + 实线虚线框架
```

---

## 分支清理指南

> 项目经过多轮迭代产生了 9 个分支，其中多数已完成使命或重复。
> 建议整合为单一 `main` 分支后，执行以下清理。

### 分支全景

```
main (165eb44) ── V1 初始版本
 │
 ├──▶ integrate-research-notes (2479ecb) ── V2 架构重构
 │     │
 │     ├──▶ notes-arch-refactor (2479ecb) ── ⚠️ 与上方完全相同
 │     │
 │     ├──▶ review-research-findings (aa58b55) ── V2 + 美股数据流
 │     │     │
 │     │     └──▶ us-data-futu-yf (40f1ad8) ── V3 🐲 隆小侠（最新最全）
 │     │
 │     └── (美股数据独立探索线)
 │
 ├──▶ research-us-data-eval (9d51a22) ── 美股数据源探索（已完成）
 │
 ├──▶ feat/explore-us-data (9d51a22) ── ⚠️ 与上方完全相同
 │
 ├──▶ us-stock-trading-setup (7846a39) ── 美股交易架构文档（已弃用）
 │
 └──▶ analyze-branches-readme (13fc58b) ── 本分支（README + 整合）
```

### 各分支状态

| 分支 | 状态 | 说明 |
|------|:----:|------|
| `main` | 🔄 待更新 | V1 初始版本，应更新为 V3 最新代码 |
| `claude/integrate-research-notes-*` | 🏷️ 老版本 | V2，已被 us-data-futu-yf 完全包含 |
| `claude/notes-arch-refactor-*` | ⚠️ 重复 | 与 integrate-research-notes 指向同一 commit |
| `claude/review-research-findings-*` | 🏷️ 老版本 | V2+美股，已被 us-data-futu-yf 包含 |
| `claude/us-data-futu-yf-*` | ✅ 最新 | V3 🐲 隆小侠，9 commits，最新最全 |
| `claude/research-us-data-eval-*` | 🏷️ 已完成 | 探索任务结束，成果已合入 V3 |
| `feat/explore-us-data` | ⚠️ 重复 | 与 research-us-data-eval 指向同一 commit |
| `claude/us-stock-trading-setup-*` | 🗑️ 已弃用 | 仅含设计文档，不再需要 |
| `claude/analyze-branches-readme-*` | 📝 当前 | 整合完成后可删除 |

### 一键清理命令

将本分支合入 main 后，可执行以下命令删除远程旧分支：

```bash
# 先将 main 更新到最新
git checkout main
git merge claude/analyze-branches-readme-c2Em8

# 删除所有远程旧分支
git push origin --delete claude/integrate-research-notes-OEQ1J
git push origin --delete claude/notes-arch-refactor-J3f3p
git push origin --delete claude/review-research-findings-J3f3p
git push origin --delete claude/us-data-futu-yf-J3f3p
git push origin --delete claude/research-us-data-eval-J3f3p
git push origin --delete feat/explore-us-data
git push origin --delete claude/us-stock-trading-setup-6NSqg
git push origin --delete claude/analyze-branches-readme-c2Em8

# 清理本地跟踪
git fetch --prune
```

清理后仅保留一个干净的 `main` 分支，包含全部 V3 代码 + README。

---

## License

Private repository — for personal use.
