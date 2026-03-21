# Web1 → Web2 迁移整合计划

## Context

Web1 有 8 个页面 + 12 个 API 路由，但经过代码级审查，**很多功能是半成品或低价值的**。本计划先做功能价值筛选，只迁移真正有用的部分。

---

## 功能价值评估（代码级审查结果）

### 值得迁移

| 功能 | 评估 | 理由 |
|------|------|------|
| **Chart** (chart.py/chart.js) | ✅ 核心工具 | K线+缠论笔/中枢叠加+MACD+MA，每日必用的分析工具 |
| **Review** (review.py/review.js) | ✅ 完整可用 | 异步后台L1/L2/L3全量分析，带进度轮询，输出完整复盘数据 |
| **Stock** (stock.py/stock.js) | ✅ 深度分析 | 多级别CZSC+异常检测+融合评分+风控+分层仓位，自包含无依赖 |
| **Dashboard 数据流** | ✅ 85%真实数据 | 三层联动总览，但需要评估是否和现有 Cluster 页合并 |

### 不迁移（丢弃）

| 功能 | 评估 | 理由 |
|------|------|------|
| **Prediction** (prediction.py) | ❌ 任意阈值 | 买卖预测靠硬编码阈值(30/40/40)，无统计依据，噪音大 |
| **Social** (social.py) | ❌ 滞后指标 | 千股千评是滞后共识，微博仅覆盖Top50大盘股，预测力弱 |
| **Plan** (plan.py) | ❌ 模板化 | 永远输出3个固定场景类型（延续/回调/反转），不适应实际行情 |
| **Analog** (analog.py) | ❌ 不可交易 | Pearson相关性匹配有数学基础，但相似≠因果，只能当视觉参考 |
| **Screener** (screener.py) | ❌ 22行薄包装 | WebEngine 的简单包装，Dashboard 内联即可 |
| **Web1 Backtest** | ❌ 已被超越 | Web2 回测严格优于（入场因子+2D扫描+高级出场+CSV导出） |

### 待定

| 功能 | 评估 | 问题 |
|------|------|------|
| **Trade** (trade.py) | ⚠️ 框架完整但空库 | SQLite CRUD+评分+统计全有，但需手动录入，目前数据库是空的。3月11日 commit 964fdc7 加入，是否有在用/计划用？ |

---

## 丢弃理由详述

### 1. Prediction（预测总览）
- 买入/卖出预测靠**硬编码阈值**：`dynamics_merged_score > 30` 且 `fused_total > 40` 判定买入，`sell_warning_score > 40` 判定卖出
- 阈值 (30/40/40) **没有任何回测验证或统计依据**
- 板块预测更简单：`signal_level` 包含"强"/"中"→买入，`bearish_ratio >= 30%` →卖出
- 每次运行输出约15个买入候选+10个卖出警告，噪音大，容易给用户错误信心
- **替代**: Dashboard 的三层联动数据本身就包含信号强度

### 2. Social（社交舆情）
- 千股千评是**滞后共识指标**（市场已经反映的信息），微博热度仅覆盖 Top50 大盘股
- 概念发现用东财接口，每个关键词最多匹配5个概念板块，搜索面窄
- `/brief` 做了 Top10 热门+Top10 异动截断，截断逻辑随意
- **替代**: web2 Cluster 页已用 K-means 做行业聚类分析，覆盖板块热度发现的核心需求

### 3. Plan（盘前计划）
- 不管当前市场状态如何，**永远输出3个固定场景类型**（趋势延续/回调确认/反转）
- 只覆盖5只指数（沪深300/上证50/创业板指/科创50/中证500），忽略其余6只
- 场景框架是写死的模板，不会根据行情变化调整分类方式
- **替代**: 复盘页(Review)已提供完整三层分析结果，盘前看前一天复盘数据更有参考价值

### 4. Analog（历史对照）
- 数学基础扎实（Pearson相关性），但**相似性≠因果性**
- 找到"走势和某年某月很像"不代表接下来走势相同，市场regime/政策/资金面完全不同
- 长窗口时相关性阈值降到0.15，匹配质量低
- 最多当视觉参考，但容易给用户虚假安全感

### 5. Screener（标的筛选）
- 22行代码，纯粹是 WebEngine 的薄包装
- 等同于 `engine.get_scored_symbols()` 直接调用
- Dashboard 页面已内联展示相同的筛选结果

### 6. Web1 Backtest
- Web2 回测工作台**严格优于** Web1 版本：
  - Web2 有入场因子（Gap/Trend Breakout/Vol Contraction），Web1 没有
  - Web2 有2D参数扫描+热力图，Web1 只有1D
  - Web2 有高级出场（固定止盈/MA离场/利润回撤/批量出场），Web1 只有基础止损
  - Web2 有CSV导出+K线磁盘缓存（24h过期+重试），Web1 没有

---

## 架构决策

### 服务层：轻量化替代 WebEngine

不移植 WebEngine 单例(1145行)，新建 3 个聚焦模块：

| 模块 | 来源 | 用途 |
|------|------|------|
| `serializers.py` (432行) | 直接复制 web1 | CZSC→JSON 转换，Chart/Dashboard 必需 |
| `date_utils.py` (48行) | 直接复制 web1 | 日期预设解析，Review 必需 |
| `market_cache.py` (~200行) | 从 WebEngine 提取 | 后台 L1/L2/L3 缓存，Dashboard/Review 必需 |

### 自包含 API（无需服务层）

Stock 和 Trade 的 API 完全自包含，直接 import `signals.core`/`signals.layers`，无需经过 market_cache。

---

## 迁移阶段

### Phase 0: 基础设施
- 创建 `signals/web2/services/`（serializers + date_utils + market_cache）
- 扩展 app.py lifespan 启动 market_cache
- index.html + app.js 扩展路由框架
- **验证**: 现有聚类+回测不受影响

### Phase 1: Chart 页面
- 复制 chart.py → web2，改用 market_cache
- 复制 chart.js，添加 HTML+CSS
- **验证**: 选指数 → K线+笔/中枢+MACD+MA，日/30M/15M 切换

### Phase 2: Review 页面
- 复制 review.py → web2，ReviewState 独立管理
- 复制 review.js，添加 HTML+CSS
- **验证**: 选日期 → 异步复盘 → 轮询进度 → 三层结果

### Phase 3: Stock 页面
- 复制 stock.py → web2（自包含，零改动）
- 复制 stock.js，添加 HTML+CSS
- **验证**: 输入代码 → 多级别分析+异常+融合+风控

### Phase 4: Dashboard（视 Phase 0-3 完成情况决定范围）
- 复制 index.py + industry.py → web2
- 复制 dashboard.js，评估与 Cluster 页的整合方案
- **验证**: 大盘方向+指数卡片+行业排行+信号列表

### Phase 5: 清理
- 删除 `signals/web/`，web2 重命名为 web
- 更新 run.py 和所有 import

---

## 关键文件

| 文件 | 作用 |
|------|------|
| `signals/web/services/engine.py` | WebEngine 提取源（market_cache 的输入） |
| `signals/web/services/serializers.py` | 直接复制到 web2 |
| `signals/web/api/chart.py` | Phase 1 迁移对象 |
| `signals/web/api/review.py` | Phase 2 迁移对象 |
| `signals/web/api/stock.py` | Phase 3 迁移对象（自包含） |
| `signals/web/api/index.py` + `industry.py` | Phase 4 迁移对象 |
| `signals/web2/app.py` | 逐阶段扩展路由 |
| `signals/web2/static/index.html` | 逐阶段添加页面 |
