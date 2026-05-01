# 盘后产业链重塑任务：postmarket_chain_rebuild

## 结论

盘后重塑不能再做成“几个概念热度 + 少数代表股”。它必须先建立一个全市场产业链知识底座，再让技术分析、交易地图、盯盘池和单票解释消费这个底座。

目标不是回答“今天哪个板块涨”，而是每天盘后重建：

```text
同花顺/东财行业板块全集
  + 同花顺/东财概念板块全集
  + A/H/US 全市场证券主体
  + 产业链 taxonomy
  + security -> chain/node 多标签归属
  + 上下游/供应商/客户关系边
  + 当日行情和技术状态
```

只有先完成这层，后面的 30m、日线、周线技术分析才知道个股是在什么产业链位置上发出信号。

## 当前问题

现有 `chain_heat_snapshots` 本质是：

```text
行业/概念热度 -> 匹配 industry_chains.yaml -> 取 representatives -> 生成交易地图预览
```

它不是全市场产业链图谱，主要问题是：

- 同花顺/东财提到的行业和概念没有形成稳定的源板块全集。
- `industry_chains.yaml` 的 representatives 被误用成覆盖池。
- 个股归属不是 `security -> chain/node` 全量多标签。
- A/H/US 没有统一 `issuer_id`，无法识别 A+H、ADR、同主体多上市地。
- 上下游、供应商、客户关系没有独立图谱，容易被概念名或用户聊天内容带偏。
- 技术分析先跑了，产业链上下文后补，导致交易解释像 MACD token 堆叠。

## 任务边界

`postmarket_chain_rebuild` 是一个新的盘后基础任务，不替代实时行情同步，也不直接筛票。

它负责：

- 汇总并规范化同花顺、东财全部行业板块。
- 汇总并规范化同花顺、东财全部概念板块。
- 拉取每个行业/概念板块的成分股，并保留来源差异。
- 建立 A/H/US 全市场证券主数据。
- 建立 `issuer_id`，把同一公司不同上市证券关联起来。
- 把源板块映射到稳定产业链 taxonomy。
- 给每只股票生成多标签产业链归属。
- 给产业链节点生成上下游顺序。
- 写入供应商、客户、竞争、替代、同集团等关系边。
- 输出覆盖率、缺口、失败源、陈旧度。

它不负责：

- 盘中实时抓取全部行情。
- 直接生成买卖建议。
- 用没有来源的推断填供应商/客户关系。
- 用少数代表股冒充全市场覆盖。

## 数据源分层

### 1. 源板块全集

新增集合：`source_board_catalog`

每天盘后从现有同步模块沉淀源板块全集：

- 同花顺行业：`board_ths`
- 东财行业：`board_em`
- 同花顺概念：`concept_ths`
- 东财概念：`concept_em`
- canonical 行业：`board_ranking`
- canonical 概念：`concept_ranking`

字段：

```text
source_board_id
source: ths / eastmoney
kind: industry / concept
raw_name
canonical_name
source_code
as_of
rank_fields
provider_status
normalization_status
```

要求：

- 同花顺和东财源数据都要保留，不能只保留 canonical。
- 源名称不可直接当产业链名称，需要先进入映射层。
- 任何空名称、NaN 名称、无法映射的源板块必须写 `normalization_status`，不能静默吞掉。

### 2. 源板块成分股

沿用并强化：

- `board_constituents`
- `concept_constituents`

要求：

- 行业和概念都要覆盖。
- 每个成分股要保留 `source_board_id`、`source`、`kind`。
- 同花顺和东财成分差异不合并丢失，先作为多源证据保留。
- 对同名板块不同源代码的情况，要保留源级身份。

### 3. A/H/US 证券主数据

新增集合：`security_master`

字段：

```text
security_id          # A:SH:600000 / HK:00700 / US:NASDAQ:NVDA
market               # A / HK / US
exchange
symbol
raw_code
name
currency
listing_status
issuer_id
primary_listing_id
linked_listing_ids
data_sources
as_of
```

要求：

- A 股覆盖全市场，数量应接近当日全市场股票数。
- 港股和美股先允许 seed/批量导入，但模型必须从第一天支持。
- A+H、ADR、红筹、中概同主体用 `issuer_id` 串起来。
- 行情是 `security_id` 级别，产业链身份优先是 `issuer_id` 级别。

### 4. 稳定产业链 taxonomy

新增集合：`chain_taxonomy_snapshots`

`industry_chains.yaml` 继续作为人工可读 taxonomy 源，但不能只靠 representatives。

taxonomy 分三层：

```text
chain          # 光模块/CPO、半导体、电新、商业航天、消费、医药、银行等
node           # 上游材料、设备、设计、制造、封测、组件、应用、终端等
role           # resource / material / equipment / component / manufacturing / application / terminal / service
```

关键链条第一批必须覆盖：

- 光模块/CPO：光芯片、光器件、光模块、交换机/服务器、数据中心、运营商/云厂商。
- 半导体/芯片：EDA/IP、设计、设备、材料、晶圆制造、封测、功率、存储、模拟、MCU、先进封装、应用端。
- 电新/电池：锂矿/锂盐、镍钴锰、磷化工、正极、负极、隔膜、电解液、添加剂、铜箔/铝箔、结构件、设备、电芯、PACK、储能、整车、回收。
- 商业航天/军工航天：卫星制造、火箭、发动机、材料、测控、地面站、卫星互联网、北斗导航、军工电子。
- 小金属/资源：钨、钼、锑、稀土、锂、钴、镍、锡、铜、铝等。
- 消费：食品饮料、乳制品、白酒、家电、旅游、零售、医美等。
- 医药：创新药、CXO、原料药、中药、医疗器械、医疗服务、药店。
- 金融/高股息防守：银行、保险、券商、电力、煤炭、运营商、交运、公用事业。

要求：

- taxonomy 是稳定交易语言；同花顺/东财板块是动态来源证据。
- 一个源概念可以映射多个 chain/node。
- 一个股票可以属于多个 chain/node，但必须标主链和证据强弱。

### 5. 源板块到产业链映射

新增集合：`source_board_chain_mappings`

字段：

```text
source_board_id
raw_name
canonical_name
chain_id
node_id
mapping_type       # direct / alias / keyword / llm_reviewed / manual_override
confidence
evidence_text
as_of
review_status
```

要求：

- 所有同花顺/东财提到的行业和概念都必须进入映射队列。
- 能映射的写 chain/node。
- 暂时不能映射的写 `review_status=unmapped` 和原因。
- 不允许因为不认识就丢掉，例如商业航天、小金属、光模块不能只靠用户聊天出现才进入系统。

### 6. 股票产业链归属

新增集合：`security_chain_memberships`

字段：

```text
security_id
issuer_id
chain_id
node_id
role
membership_type    # core / supplier / customer / equipment / material / application / theme / weak_related
confidence
exposure_score
is_primary_chain
source_boards
evidence_sources
evidence_docs
as_of
stale_level
```

归属来源优先级：

1. 人工审核/明确覆盖表。
2. 同花顺/东财行业成分股。
3. 同花顺/东财概念成分股。
4. 公司主营、年报、公告、招股书等结构化证据。
5. A/H/US 同主体继承。
6. 名称/关键词弱匹配。

要求：

- 热度不能单独创造长期产业链归属。
- 概念成分只能作为证据之一，不能无脑等于主业。
- 弱相关必须标 `weak_related`，不能混进核心产业链。

### 7. 上下游和供应商/客户关系图

新增集合：`chain_relationship_edges`

字段：

```text
edge_id
from_id              # issuer_id or chain_node_id
to_id                # issuer_id or chain_node_id
edge_type            # upstream / downstream / supplier / customer / competitor / substitute / same_group
chain_id
node_id
direction
confidence
evidence_source      # filing / announcement / exchange_doc / manual / source_board / inferred_taxonomy
evidence_url_or_doc
evidence_text
as_of
stale_level
```

规则：

- 产业链上下游顺序可以来自 taxonomy。
- 公司级供应商/客户关系必须来自年报、公告、招股书、研报摘录或人工审核。
- 同花顺/东财概念板块不能直接证明供应商/客户关系，只能证明“同主题/同板块出现”。
- 没有证据时写 unknown，不允许编。

## DAG 位置

新增任务：

```text
postmarket_chain_rebuild
```

建议放在：

```text
fullmarket_spot_snapshot
stock_daily
board_ranking
board_cons
    -> postmarket_chain_rebuild
        -> chain_heat_snapshots
        -> concept_relationship_graph
        -> terminal_realtime_pool
        -> strategy_snapshot
```

`chain_heat_snapshots` 之后只做“当日热度叠加”，不能再负责建立产业链归属。

## 盘后输出

新增/重构集合：

- `source_board_catalog`
- `source_board_chain_mappings`
- `security_master`
- `security_listing_links`
- `chain_taxonomy_snapshots`
- `security_chain_memberships`
- `chain_relationship_edges`
- `chain_node_security_rollups`
- `chain_coverage_reports`
- `chain_rebuild_runs`
- `data_freshness(domain=chain_rebuild)`

## 技术分析如何消费

技术分析前置条件：

```text
股票 -> 主体 -> 产业链归属 -> 节点位置 -> 上下游关系 -> 当日热度/节奏 -> 技术形态
```

单票解释必须能回答：

- 它属于哪条主链。
- 它处于产业链哪个节点。
- 这个节点今天在整条链里是领涨、补涨、退潮还是防守。
- 它的上游是谁，下游是谁，直接客户/供应商有哪些证据。
- 技术买点是在主线加速、分歧承接、二波观察，还是弱相关蹭概念。

右侧单票解释不能再输出：

```text
30分钟 MACD绿柱扩大_零上 / 5分钟 MACD...
```

而应该输出：

```text
交易身份：电池隔膜 / 电池材料中游
产业位置：锂电材料 -> 隔膜
链条状态：电新修复，隔膜节点未确认领涨
能不能动：等 30m 承接，不追上影线
证据来源：东财概念成分、同花顺行业、公司主营、链条节点映射
失效条件：跌破 10 日线或 30m 承接失败
```

## 验收标准

- 同花顺/东财行业板块和概念板块都有源级 catalog，且空名称/NaN 不进入正常 catalog。
- 每个源板块要么映射到 chain/node，要么进入 unmapped 队列并显示原因。
- A 股全市场证券数接近当日全市场数量，不低于 5000 级别。
- HK/US 使用同一套 `security_id`、`issuer_id`、`listing_links` 模型。
- 光模块、半导体、电新、商业航天、小金属等关键链条不能是 representatives-only。
- 每条 `security_chain_membership` 必须有 evidence 和 confidence。
- 公司级供应商/客户关系必须有来源；没有来源时显示 unknown。
- `chain_heat_snapshots` 消费 membership/rollup，不再从 YAML representatives 直接造股票池。
- `terminal_stock_pool` 的 chain_context 来自 `security_chain_memberships`。
- 同一交易日重复运行幂等，不重复写入。
- provider 部分失败时可复用上日 membership，但要在 `chain_rebuild_runs` 和 `data_freshness` 标陈旧。

## 第一阶段实施

第一阶段先落 A 股全市场和图谱模型，HK/US 做 schema 与少量 seed，不阻塞 A 股。

文件范围：

- `signals/sync/modules/postmarket_chain_rebuild.py`
- `signals/sync/postmarket.py`
- `signals/sync/engine.py`
- `signals/core/industry_chains.yaml`
- `signals/core/concept_carriers.py`
- `signals/sync/modules/chain_heat.py`
- `signals/sync/modules/terminal_pool.py`
- `signals/web/api/workbench.py`
- `tests/test_postmarket_chain_rebuild.py`

第一阶段交付：

- 源板块 catalog。
- 源板块到 chain/node 映射。
- A 股 `security_master`。
- A 股 `security_chain_memberships`。
- `chain_node_security_rollups`。
- `chain_coverage_reports`。
- 交易终端右侧展示产业链证据来源。

第二阶段：

- A/H/US `issuer_id` linking。
- 港股、美股产业链 seed 与继承规则。
- 供应商/客户关系边接入年报、公告、人工审核证据。
- UI 增加产业链上下游视图。

