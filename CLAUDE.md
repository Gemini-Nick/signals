# 🐲 隆小侠 LONG CLAW

> 实线虚线分析框架 — 指数研判 → 行业研判 → 标的筛选

## 项目架构

```
signals/
├── core/          # 分析引擎（信号检测、评分、频率映射）
├── data/          # 多数据源统一接口（Tushare/AKShare/Futu/yfinance）
├── layers/        # 三层联动分析
│   ├── index      # Layer 1: 指数研判（11只指数，日线+30M+15M 三级联动）
│   ├── industry   # Layer 2: 行业研判（板块强度评分）
│   └── screener   # Layer 3: 标的筛选（白名单快扫 + 行业批扫）
├── research/      # 研报系统（多格式导入、自动归档、双维度展示）
└── notify/        # 飞书推送
```

## 五大模块

| 模块 | 路径 | 功能 |
|------|------|------|
| **Core** | `signals/core/` | 信号检测引擎：一买/二买/三买/背驰买卖 + 评分系统 + 多级别共振 |
| **Data** | `signals/data/` | 多市场数据源：A股(AKShare) + 港股(Futu) + 美股(Futu优先/yfinance兜底) |
| **Layers** | `signals/layers/` | 三层联动：指数大势 → 行业强弱 → 个股筛选 |
| **Research** | `signals/research/` | 研报导入(MD/PDF/图片OCR) + 自动归档(notes/YYYY/MM/) + 时间衰减 |
| **Notify** | `signals/notify/` | 飞书群聊推送分析结果 |

## 运行模式

```bash
python run.py                                    # 盘中监测（默认）
python run.py --mode index                       # 仅指数报告（快速）
python run.py --mode review --start 2024-09-24   # 盘后复盘
python run.py --mode import --file 研报.pdf       # 导入研究笔记
```

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
