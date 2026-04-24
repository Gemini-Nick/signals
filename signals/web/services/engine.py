# -*- coding: utf-8 -*-
"""
Engine Bridge — 桥接现有分析引擎和 Web API

包装 IndexScreener、MarketContext 等，缓存分析结果供 API 读取。
不包含任何分析逻辑，只做包装和缓存。
"""
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
import threading
import time


@dataclass
class ReviewState:
    """Review mode cached state"""
    start_date: str = ""
    start_label: str = ""
    market_context: Optional[object] = None
    index_reports: List[object] = field(default_factory=list)
    analyzers: Dict[str, object] = field(default_factory=dict)
    gain_list: List[object] = field(default_factory=list)
    composite_list: List[object] = field(default_factory=list)
    merged_list: List[object] = field(default_factory=list)
    concepts: List[object] = field(default_factory=list)
    oversold_list: List[object] = field(default_factory=list)
    scored_symbols: List[object] = field(default_factory=list)
    rotation_stage: str = ""
    rotation_detail: str = ""
    allocation_suggestion: str = ""
    # Status
    is_running: bool = False
    phase: str = ""  # "L1" / "L2" / "L3" / "" (complete)
    error: str = ""
    completed: bool = False
    # Timing (seconds)
    timing: Dict[str, float] = field(default_factory=dict)  # {"L1": 12.3, "L2": 8.1, ...}
    phase_detail: str = ""  # 当前阶段子步骤描述, e.g. "加载A股指数..."
    # 信号回放时间线 (Phase 4)
    replay_timelines: Dict[str, list] = field(default_factory=dict)  # symbol → List[SignalChange]


@dataclass
class EngineState:
    """Web 引擎缓存状态"""
    # L1 分析结果
    market_context: Optional[object] = None     # MarketContext
    index_reports: List[object] = field(default_factory=list)  # IndexReport[]

    # L2 行业结果
    gain_list: List[object] = field(default_factory=list)       # IndustryRanking[]
    composite_list: List[object] = field(default_factory=list)  # IndustryRanking[]
    merged_list: List[object] = field(default_factory=list)     # IndustryRanking[]
    concepts: List[object] = field(default_factory=list)        # ConceptRanking[]
    oversold_list: List[object] = field(default_factory=list)   # IndustryRanking[]

    # L3 信号结果
    scored_symbols: List[object] = field(default_factory=list)  # ScoredSymbol[]

    # 底层分析器引用 (用于图表数据)
    index_screener: Optional[object] = None     # IndexScreener
    analyzers: Dict[str, object] = field(default_factory=dict)  # name -> IndexAnalyzer

    # L2 统计数据 (用于恐慌检测、抄底候选等)
    l2_stats: Dict = field(default_factory=dict)  # {zt_total, dt_total, name_df, ...}

    # 板块动量 (预测维度)
    momentum_signals: List[object] = field(default_factory=list)  # SectorMomentumSignal[]

    # 时段模式
    session_mode: Optional[object] = None  # SessionMode

    # 状态
    last_update: float = 0.0
    is_running: bool = False
    error: str = ""
    loading_phase: str = ""  # "L1"/"L2"/"L3"/""(完成)


def _l1_action(market_state: str, has_buy: bool, has_sell: bool,
               trend: str) -> str:
    """
    L1 策略指引 — 基于 market_state + 信号 + 趋势多维度判断。

    | market_state | has_buy | has_sell | → action |
    |-------------|---------|----------|----------|
    | 急跌 | ✓ | - | 恐慌抄底窗口 |
    | 急跌 | - | ✓ | 回避，勿追跌 |
    | 急跌 | - | - | 等待企稳信号 |
    | 企稳 | ✓ | - | 确认买入 |
    | 企稳 | - | - | 观望待方向 |
    | 反弹 | ✓ | - | 积极关注 |
    | 反弹 | - | ✓ | 反弹减仓 |
    | 平稳+上涨 | ✓ | - | 持仓待涨 |
    | 平稳+下跌 | - | ✓ | 减仓观望 |
    | 平稳+震荡 | - | - | 轻仓观望 |
    """
    if market_state == "急跌":
        if has_buy:
            return "恐慌抄底窗口"
        elif has_sell:
            return "回避，勿追跌"
        else:
            return "等待企稳信号"
    elif market_state == "缓跌":
        if has_buy:
            return "逢低关注"
        elif has_sell:
            return "减仓观望"
        else:
            return "观望等企稳"
    elif market_state == "企稳":
        if has_buy:
            return "确认买入"
        elif has_sell:
            return "谨慎，等确认"
        else:
            return "观望待方向"
    elif market_state == "反弹":
        if has_buy:
            return "积极关注"
        elif has_sell:
            return "反弹减仓"
        else:
            return "轻仓跟随"
    else:  # 平稳
        if has_buy and has_sell:
            return "信号分歧，轻仓"
        elif has_buy:
            if "上涨" in trend:
                return "持仓待涨"
            else:
                return "可关注"
        elif has_sell:
            if "下跌" in trend:
                return "减仓观望"
            else:
                return "需回避"
        else:
            return "轻仓观望"


def _l2_verdict(rhythm: str, cs: float, zt: int, net_inflow: float,
                gain_pct: float, market_state: str) -> tuple:
    """
    L2 行业操作建议 — 基于 rhythm_phase + 综合分 + 资金流 + market_state。

    返回 (verdict, verdict_detail)
    """
    verdict = "观望"
    detail = ""

    if rhythm in ("高潮", "衰竭"):
        verdict = "高抛兑现"
        detail = f"板块已到{rhythm}阶段"
    elif rhythm == "启动":
        if cs >= 60 and (net_inflow or 0) > 0:
            verdict = "刚进攻"
            detail = "新启动+资金进场"
        else:
            verdict = "关注启动"
            detail = "启动初期待确认"
    elif rhythm == "加速":
        if cs >= 70:
            verdict = "追强"
            detail = "强者恒强"
        else:
            verdict = "关注"
            detail = "加速中"
    elif rhythm == "休整":
        verdict = "等待"
        detail = "等二次启动"
    else:
        # 无 rhythm 数据 → 兜底逻辑
        if cs >= 70 and zt >= 3:
            verdict = "关注"
            detail = f"综合{cs:.0f}+涨停{zt}"
        elif cs >= 40:
            verdict = "观望"
            detail = ""
        else:
            verdict = "回避"
            detail = ""

    # 恐慌叠加
    if market_state == "急跌" and rhythm not in ("衰竭",):
        verdict = f"恐慌中→{verdict}"

    return verdict, detail


class WebEngine:
    """
    Web UI 的分析引擎桥接层。

    用法:
        engine = WebEngine()
        engine.run_l1()  # 运行 L1 指数分析
        ctx = engine.get_market_context()
        reports = engine.get_index_reports()
        analyzer = engine.get_index_analyzer("沪深300", "daily")
    """

    def __init__(self):
        self._state = EngineState()
        self._review = ReviewState()
        self._lock = threading.Lock()

    @property
    def state(self) -> EngineState:
        return self._state

    @property
    def review_state(self) -> ReviewState:
        return self._review

    def run_l1(self, manage_state: bool = True):
        """运行 Layer 1 指数分析（同步，阻塞）"""
        if manage_state:
            with self._lock:
                if self._state.is_running:
                    return
                self._state.is_running = True
                self._state.error = ""

        try:
            from signals.layers.index_screener import IndexScreener

            screener = IndexScreener()
            screener.initialize()
            ctx = screener.analyze()

            with self._lock:
                self._state.index_screener = screener
                self._state.market_context = ctx
                self._state.index_reports = ctx.reports
                self._state.analyzers = screener.analyzers
                self._state.last_update = time.time()

        except Exception as e:
            with self._lock:
                self._state.error = str(e)
            raise
        finally:
            if manage_state:
                with self._lock:
                    self._state.is_running = False

    def run_l2(self, session=None):
        """运行 Layer 2 行业分析（同步，阻塞）"""
        try:
            from signals.layers.industry import get_industry_representatives
            kwargs = {}
            if session and not session.a_live:
                from datetime import datetime as _dt
                kwargs["date_str"] = _dt.now().strftime("%Y%m%d")
            gain, composite, merged, concepts, oversold, sentiment_stats = get_industry_representatives(**kwargs)
            with self._lock:
                self._state.gain_list = gain
                self._state.composite_list = composite
                self._state.merged_list = merged
                self._state.concepts = concepts
                self._state.oversold_list = oversold
                self._state.l2_stats = sentiment_stats or {}
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("L2 行业分析失败: %s", e)

        # 板块动量扫描（预测维度，嵌入 L2 主流程）
        try:
            from signals.core.sector_momentum import scan_hot_sectors
            concept_names = [c.name for c in (self._state.concepts or [])[:30]]
            if concept_names:
                momentum = scan_hot_sectors(concept_names=concept_names, top_n=10)
                with self._lock:
                    self._state.momentum_signals = momentum
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("板块动量扫描失败: %s", e)

    def run_l3(self, session=None):
        """运行 Layer 3 标的筛选（并行处理多行业 + 动量板块领涨股）"""
        if session and session.use_daily_l3:
            self._run_l3_daily()
        else:
            self._run_l3_intraday()

    def _run_l3_daily(self):
        """盘后 L3: 使用日线 review_screener（不拉分钟线）"""
        try:
            import config
            from datetime import datetime as _dt, timedelta
            merged = self._state.merged_list
            ranking_stocks = []
            for r in (merged or []):
                ranking_stocks.extend(r.pool_codes)
            ranking_stocks = list(dict.fromkeys(ranking_stocks))
            all_symbols = list(dict.fromkeys(
                config.WHITELIST + ranking_stocks))

            if not all_symbols:
                return

            from signals.layers.review_screener import review_stock_daily
            start = (_dt.now() - timedelta(days=60)).strftime("%Y-%m-%d")
            scored = review_stock_daily(
                all_symbols, start, with_minute=False,
                l2_stats=self._state.l2_stats,
                market_ctx=self._state.market_context,
            )

            # 注入公司名称
            try:
                from signals.core.stock_names import get_resolver
                resolver = get_resolver()
                if merged:
                    resolver.inject_from_rankings(merged)
                for s in scored:
                    if not s.name:
                        s.name = resolver.get_name(s.symbol)
            except Exception:
                pass

            with self._lock:
                self._state.scored_symbols = scored
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("L3 日线分析失败: %s", e)

    def _run_l3_intraday(self):
        """盘中 L3: 使用 IntraDayScreener（分钟线）"""
        try:
            from signals.layers.screener import IntraDayScreener
            merged = self._state.merged_list
            if not merged:
                return
            all_scored = []

            # 行业列表 + 动量板块领涨股入池
            industries = [ind.name for ind in merged[:8]]
            from concurrent.futures import ThreadPoolExecutor, as_completed

            # 传递 l2_stats 和 market_ctx 给 screener
            l2_stats = self._state.l2_stats
            market_ctx = self._state.market_context

            def _scan_one(ind_name):
                try:
                    s = IntraDayScreener()
                    s._l2_stats = l2_stats
                    s._market_ctx = market_ctx
                    return s.run_industry(ind_name)
                except Exception:
                    return []

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = {pool.submit(_scan_one, name): name
                           for name in industries}
                for f in as_completed(futures):
                    scored = f.result()
                    if scored:
                        all_scored.extend(scored)

            # 动量板块领涨股作为额外入池来源
            momentum_symbols = set()
            for sig in (self._state.momentum_signals or [])[:5]:
                for mover in (sig.top_movers or [])[:3]:
                    if mover.code:
                        momentum_symbols.add(mover.code)
            # 去掉已扫过的
            existing = {s.symbol for s in all_scored}
            new_momentum = [s for s in momentum_symbols if s not in existing]
            if new_momentum:
                try:
                    s = IntraDayScreener(symbols=new_momentum)
                    s._l2_stats = l2_stats
                    s._market_ctx = market_ctx
                    s.init_analyzers()
                    momentum_scored = s.scan_once()
                    all_scored.extend(momentum_scored)
                except Exception:
                    pass

            # 按分数排序去重（优先融合分）
            seen = set()
            unique = []
            for s in sorted(all_scored,
                            key=lambda x: x.fused_total if x.fused_total else x.total_score,
                            reverse=True):
                if s.symbol not in seen:
                    seen.add(s.symbol)
                    unique.append(s)
            # 注入公司名称
            try:
                from signals.core.stock_names import get_resolver
                resolver = get_resolver()
                if self._state.merged_list:
                    resolver.inject_from_rankings(self._state.merged_list)
                for s in unique:
                    if not s.name:
                        s.name = resolver.get_name(s.symbol)
            except Exception:
                pass

            with self._lock:
                self._state.scored_symbols = unique
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("L3 标的筛选失败: %s", e)

    def get_market_context(self):
        """获取缓存的 MarketContext"""
        return self._state.market_context

    def get_index_reports(self) -> list:
        """获取缓存的 IndexReport 列表"""
        return self._state.index_reports or []

    def get_index_analyzer(self, name: str):
        """
        按指数名称获取 IndexAnalyzer。
        返回 IndexAnalyzer 或 None。
        """
        return self._state.analyzers.get(name)

    def get_symbol_analyzer(self, name: str, freq: str):
        """
        获取特定指数+特定周期的 SymbolAnalyzer。

        :param name: 指数名称，如 "沪深300"
        :param freq: "daily" / "30min" / "15min"
        :return: SymbolAnalyzer 或 None
        """
        idx_az = self._state.analyzers.get(name)
        if idx_az is None:
            return None

        freq_map = {
            "daily": "_daily",
            "30min": "_f30",
            "15min": "_f15",
        }
        attr = freq_map.get(freq)
        if attr is None:
            return None

        return getattr(idx_az, attr, None)

    def find_index_name(self, symbol: str) -> Optional[str]:
        """根据 symbol 代码查找指数名称"""
        for report in self.get_index_reports():
            if report.symbol == symbol:
                return report.name
        # 也尝试从 analyzers 中查找
        for name, az in self._state.analyzers.items():
            if az.symbol == symbol:
                return name
        return None

    def get_industry_data(self) -> dict:
        """获取缓存的 L2 行业数据"""
        return {
            "gain_list": self._state.gain_list or [],
            "composite_list": self._state.composite_list or [],
            "merged_list": self._state.merged_list or [],
            "concepts": self._state.concepts or [],
            "oversold_list": self._state.oversold_list or [],
        }

    def get_concepts(self) -> list:
        """获取缓存的概念板块排行"""
        return self._state.concepts or []

    def get_scored_symbols(self) -> list:
        """获取缓存的 ScoredSymbol 列表"""
        return self._state.scored_symbols or []

    def get_l2_stats(self) -> dict:
        """获取 L2 统计数据（含 name_df 等）"""
        return self._state.l2_stats or {}

    def get_momentum_signals(self) -> list:
        """获取板块动量信号列表"""
        return self._state.momentum_signals or []

    def get_industry_ranking_by_name(self, name: str):
        """按行业名称查找 IndustryRanking 对象"""
        for r in (self._state.gain_list or []) + (self._state.composite_list or []):
            if r.name == name:
                return r
        for r in (self._state.merged_list or []):
            if r.name == name:
                return r
        return None

    def resolve_sector(self, query: str) -> dict:
        """
        智能板块解析：自然语言 → 行业列表 + 概念列表。

        匹配逻辑（按优先级）：
        1. 精确匹配行业名（如 "光伏设备"）
        2. 轮动线匹配（如 "新能源" → 5 个行业）
        3. 主题关键词匹配（如 "电气新能源" → 匹配含 "新能源" 的关键词）
        4. 概念模糊匹配（遍历缓存概念列表）
        """
        import config as _cfg

        result = {
            "query": query,
            "match_type": "none",
            "matched_industries": [],
            "matched_concepts": [],
        }

        # 所有已知行业名
        rotation_map = getattr(_cfg, "ROTATION_LINE_MAP", {})
        all_industry_names = set(rotation_map.keys())

        # 1. 精确匹配行业名
        if query in all_industry_names:
            result["match_type"] = "exact"
            result["matched_industries"] = [query]
            ranking = self.get_industry_ranking_by_name(query)
            if ranking:
                result["matched_industries_info"] = [{
                    "name": query,
                    "rotation_line": ranking.rotation_line,
                    "gain_pct": ranking.gain_pct,
                }]
            return result

        # 2. 轮动线匹配（如 "新能源" → 光伏设备/风电设备/电网设备/电机/其他电源设备）
        for ind_name, rot_line in rotation_map.items():
            if rot_line and query in rot_line:
                result["matched_industries"].append(ind_name)
        if result["matched_industries"]:
            result["match_type"] = "rotation_line"
            return result

        # 3. 主题关键词匹配
        try:
            from signals.core.theme_tracker import THEME_KEYWORD_MAP
            for theme, keywords in THEME_KEYWORD_MAP.items():
                if any(kw in query for kw in keywords) or query in theme:
                    # 找到关联行业
                    for ind_name, rot_line in rotation_map.items():
                        if rot_line and theme in rot_line:
                            if ind_name not in result["matched_industries"]:
                                result["matched_industries"].append(ind_name)
                    if result["matched_industries"]:
                        result["match_type"] = "theme_keyword"
                        break
        except ImportError:
            pass

        # 4. 概念模糊匹配
        concepts = self._state.concepts or []
        for c in concepts:
            if query in c.name or c.name in query:
                result["matched_concepts"].append(c.name)

        # 也检查行业名的模糊包含
        if not result["matched_industries"]:
            for ind_name in all_industry_names:
                if query in ind_name or ind_name in query:
                    result["matched_industries"].append(ind_name)

        if result["matched_industries"] or result["matched_concepts"]:
            if not result["match_type"] or result["match_type"] == "none":
                result["match_type"] = "fuzzy"

        return result

    def get_action_summary(self) -> dict:
        """
        生成操作建议 JSON（道长策略）。
        整合 L1 大势 + L2 行业 + L3 标的 + 恐慌检测 + 抄底候选 + 主题追踪。
        """
        import logging
        log = logging.getLogger(__name__)
        ctx = self._state.market_context
        reports = self._state.index_reports or []
        scored = self._state.scored_symbols or []
        l2_stats = self._state.l2_stats or {}
        name_df = l2_stats.get("name_df")
        oversold_list = self._state.oversold_list or []
        concepts = self._state.concepts or []
        gain_list = self._state.gain_list or []
        composite_list = self._state.composite_list or []

        result = {}

        # ── 大势 ──
        if ctx:
            dir_map = {"偏多": "📈", "偏空": "📉", "分化": "↔️"}
            result["market"] = {
                "direction": ctx.overall_direction,
                "emoji": dir_map.get(ctx.overall_direction, ""),
                "style": getattr(ctx, "recommended_style", "未知"),
                "sentiment": getattr(ctx, "sentiment_phase", "未知"),
                "position_suggestion": getattr(ctx, "position_suggestion", ""),
            }

        # ── 恐慌检测（必须在 L1/L2 之前，提供 market_state）──
        panic_data = {"score": 0, "level": "正常", "detail": "",
                      "action_hint": "", "velocity": 0, "market_state": "平稳"}
        market_state = "平稳"
        panic_velocity = 0.0
        try:
            from signals.core.panic_detector import assess_intraday_panic
            pa = assess_intraday_panic(reports, self._state.analyzers, name_df)
            market_state = pa.market_state
            panic_velocity = pa.velocity
            panic_data = {
                "score": pa.score,
                "level": pa.level,
                "detail": pa.detail,
                "velocity": pa.velocity,
                "acceleration": pa.acceleration,
                "market_state": pa.market_state,
                "is_stabilizing": pa.is_stabilizing,
                "action_hint": "",
            }
            # action_hint 基于市场状态 + 分数
            if pa.market_state == "急跌":
                panic_data["action_hint"] = "急跌中 → 等待速率放缓再考虑抄底"
            elif pa.market_state == "企稳":
                panic_data["action_hint"] = "市场企稳中 → 可开始关注反弹机会"
            elif pa.market_state == "反弹":
                panic_data["action_hint"] = "反弹进行中 → 轻仓跟随，注意压力位"
            elif pa.score >= 60:
                panic_data["action_hint"] = "恐慌=底部信号 → 关注超跌+支撑位的反弹机会"
            elif pa.score >= 40:
                panic_data["action_hint"] = "市场偏弱 → 控制仓位，等待企稳信号"
            elif pa.score >= 20:
                panic_data["action_hint"] = "市场平稳，维持现有仓位"
            else:
                panic_data["action_hint"] = "市场平稳，持仓稳定"
        except Exception as e:
            log.warning("恐慌检测失败: %s", e)
        result["panic"] = panic_data

        # ── L1 策略指引（基于 market_state 多维度判断）──
        result["l1_guidance"] = []
        for r in reports:
            if not getattr(r, "data_available", False):
                continue
            sigs = []
            daily_sig = getattr(r, "daily_latest_signal", "无")
            f30_sig = getattr(r, "f30_latest_signal", "无")
            f15_sig = getattr(r, "f15_latest_signal", "无")
            if daily_sig != "无":
                sigs.append(f"日:{daily_sig}")
            if f30_sig != "无":
                sigs.append(f"30M:{f30_sig}")
            if f15_sig != "无":
                sigs.append(f"15M:{f15_sig}")
            if not sigs:
                continue
            has_buy = any("买" in s for s in sigs)
            has_sell = any("卖" in s for s in sigs)
            trend = getattr(r, "daily_trend", "未知")
            action = _l1_action(market_state, has_buy, has_sell, trend)
            result["l1_guidance"].append({
                "name": r.name,
                "trend": trend,
                "signals": sigs,
                "aligned": getattr(r, "three_level_aligned", False),
                "action": action,
            })

        # ── L2 行业操作建议（基于 rhythm_phase + 资金流 + market_state）──
        result["l2_actions"] = []
        for ind in composite_list[:5]:
            cs = getattr(ind, "composite_score", 0)
            zt = getattr(ind, "zt_count", 0)
            rhythm = getattr(ind, "rhythm_phase", "")
            net_inflow = getattr(ind, "net_inflow", 0)
            gain_pct = getattr(ind, "gain_pct", 0)
            verdict, verdict_detail = _l2_verdict(
                rhythm, cs, zt, net_inflow, gain_pct, market_state)
            top_stock = ""
            if getattr(ind, "candidates", None):
                top_stock = ind.candidates[0].name
            result["l2_actions"].append({
                "name": ind.name,
                "score": round(cs, 1),
                "zt": zt,
                "verdict": verdict,
                "verdict_detail": verdict_detail,
                "rhythm": rhythm or "—",
                "gain_pct": round(gain_pct, 2) if gain_pct else 0,
                "net_inflow": round(net_inflow, 2) if net_inflow else 0,
                "top_stock": top_stock,
            })

        # ── 抄底候选（门槛降到30）──
        bottom_candidates = []
        if panic_data["score"] >= 30:
            try:
                from signals.layers.industry import get_bottom_fishing_candidates
                themes = ["CLAW", "算力", "新能源"]
                bots = get_bottom_fishing_candidates(
                    name_df, oversold_list, panic_data["score"], themes, top_n=5)
                for b in bots:
                    urgency = "关注" if panic_data["score"] >= 40 else "关注（非急迫）"
                    bottom_candidates.append({
                        "name": b.name, "gain_pct": round(b.gain_pct, 2),
                        "oversold_score": round(b.oversold_score, 1),
                        "type": b.sector_type, "line": b.rotation_line,
                        "urgency": urgency,
                    })
            except Exception as e:
                log.warning("抄底候选失败: %s", e)
        result["bottom_candidates"] = bottom_candidates

        # ── L3 操作建议（买入/风险/关注）──
        from config import SCORE_THRESHOLD
        above = [r for r in scored
                 if r.total_score >= SCORE_THRESHOLD and r.signal_count > 0]
        buys = [r for r in above if r.direction == "偏多"]
        sells = [r for r in scored
                 if r.direction == "偏空" and abs(r.total_score) >= 30
                 and r.signal_count > 0]

        # 信号简要
        def _brief(signals):
            freq_abbrev = {
                "15分钟": "15M", "30分钟": "30M", "60分钟": "60M",
                "日线": "日", "周线": "周",
            }
            sig_freq = {}
            for sig in signals:
                abbr = freq_abbrev.get(sig.freq, sig.freq)
                sig_freq.setdefault(sig.signal_type, []).append(abbr)
            parts = []
            for sig_type, freqs in sig_freq.items():
                parts.append(f"{sig_type}({'+'.join(freqs)})")
            return " ".join(parts[:4])

        result["buy_opportunities"] = []
        for r in buys[:8]:
            buy_freqs = {s.freq for s in r.signals if "买" in s.signal_type}
            is_multi_tf = len(buy_freqs) > 1
            result["buy_opportunities"].append({
                "symbol": r.symbol,
                "name": getattr(r, "name", r.symbol),
                "score": round(r.total_score, 1),
                "direction": r.direction,
                "signals_brief": _brief(r.signals),
                "resonance_tag": "★共振" if is_multi_tf else "",
            })

        result["risk_alerts"] = []
        for r in sells[:5]:
            result["risk_alerts"].append({
                "symbol": r.symbol,
                "name": getattr(r, "name", r.symbol),
                "score": round(r.total_score, 1),
                "direction": r.direction,
            })

        # 重点关注
        result["focus_list"] = []
        for r in above:
            buy_freqs = {s.freq for s in r.signals if "买" in s.signal_type}
            is_multi_tf = len(buy_freqs) > 1
            if is_multi_tf:
                result["focus_list"].append({
                    "symbol": r.symbol,
                    "name": getattr(r, "name", r.symbol),
                    "score": round(r.total_score, 1),
                    "tags": ["多级别共振"],
                })

        # ── 行业研判 ──
        strong = []
        for ind in composite_list[:5]:
            info = f"{ind.name}(综合{ind.composite_score:.0f}"
            if ind.zt_count > 0:
                info += f",涨停{ind.zt_count}只"
            info += ")"
            strong.append(info)
        weak = []
        if name_df is not None and not name_df.empty:
            from signals.core.theme_tracker import _find_name_col, _find_change_col
            nc = _find_name_col(name_df)
            cc = _find_change_col(name_df)
            if nc and cc:
                import pandas as pd
                df_sorted = name_df.copy()
                df_sorted[cc] = pd.to_numeric(df_sorted[cc], errors='coerce')
                bottom5 = df_sorted.nsmallest(3, cc)
                for _, row in bottom5.iterrows():
                    weak.append(f"{row[nc]}({float(row[cc]):+.1f}%)")
        result["industry_verdict"] = {
            "strong": strong,
            "weak": weak,
            "note": "⚠ 涨幅靠前的行业可能是高抛兑现，关注综合评分和涨停密度",
        }

        # ── 结论（即使无 L3 也有意义）──
        if buys:
            conclusion = f"有{len(buys)}只偏多标的，可关注买入机会。"
            if sells:
                conclusion = "市场分化，精选偏多标的，回避偏空品种。"
        elif sells:
            conclusion = "存在卖出信号，控制仓位，回避偏空标的。"
        else:
            # 无 L3 数据时，基于 L1 + L2 生成结论
            l1_buy_names = [g["name"] for g in result.get("l1_guidance", [])
                            if g["action"] == "可关注"]
            l2_focus = [a["name"] for a in result.get("l2_actions", [])
                        if a["verdict"] == "关注"]
            parts = []
            if l1_buy_names:
                parts.append(f"指数级别 {'、'.join(l1_buy_names[:3])} 有买入信号")
            if l2_focus:
                parts.append(f"{'、'.join(l2_focus[:3])} 板块涨停密集")
            if parts:
                conclusion = "、".join(parts) + "，可重点关注。"
            else:
                conclusion = "暂无明确买卖机会，维持观望。"
            if not scored:
                conclusion += " 暂无个股级别信号。"

        if ctx:
            conclusion += f" 大盘{ctx.overall_direction}+{getattr(ctx, 'sentiment_phase', '')}阶段"
            if getattr(ctx, "recommended_style", ""):
                conclusion += f"，精选{ctx.recommended_style}"
        result["conclusion"] = conclusion

        return result

    def run_review(self, start_date: str, label: str = ""):
        """
        盘后复盘：后台线程运行 L1→L2→L3。
        前端通过 review_state 轮询进度。
        """
        with self._lock:
            if self._review.is_running:
                return
            # 重置状态
            self._review = ReviewState(
                start_date=start_date,
                start_label=label,
                is_running=True,
                phase="L1",
            )

        def _worker():
            import time as _time
            import logging
            _logger = logging.getLogger("signals.review")
            rv = self._review
            def _log(msg):
                print(msg, flush=True)
                _logger.info(msg)
            _t_total = _time.monotonic()
            try:
                # ── L1: 指数复盘 ──
                rv.phase = "L1"
                rv.phase_detail = "加载指数数据..."
                _log(f"[复盘] L1 开始 — 指数分析 (start={start_date})")
                _t0 = _time.monotonic()
                from signals.layers.index_screener import IndexScreener
                screener = IndexScreener()
                ctx = screener.run_review(start_date)
                rv.market_context = ctx
                rv.index_reports = ctx.reports if ctx else []
                rv.analyzers = screener.analyzers
                rv.timing["L1"] = round(_time.monotonic() - _t0, 1)
                _log(f"[复盘] L1 完成 — {rv.timing['L1']}s "
                     f"({len(rv.index_reports)} 指数)")

                # ── L2: 行业筛选 ──
                rv.phase = "L2"
                rv.phase_detail = "获取行业排名..."
                _log("[复盘] L2 开始 — 行业筛选")
                _t0 = _time.monotonic()
                from signals.layers.industry import get_industry_representatives
                from datetime import datetime as _dt
                import config
                pool_date = _dt.now().strftime("%Y%m%d")
                try:
                    gain, composite, merged, concepts, oversold, _ = \
                        get_industry_representatives(
                            config.RANK_TOP_N, date_str=pool_date)
                except Exception as e:
                    _log(f"[复盘] L2 异常: {e}")
                    gain, composite, merged, concepts, oversold = [], [], [], [], []
                rv.gain_list = gain
                rv.composite_list = composite
                rv.merged_list = merged
                rv.concepts = concepts
                rv.oversold_list = oversold
                rv.timing["L2_rank"] = round(_time.monotonic() - _t0, 1)
                _log(f"[复盘] L2 行业排名 — {rv.timing['L2_rank']}s "
                     f"({len(merged)} 行业)")

                # 轮动
                _t0 = _time.monotonic()
                if ctx and (gain or composite):
                    try:
                        from signals.core.rotation import (
                            detect_rotation_stage, suggest_allocation)
                        rot = detect_rotation_stage(gain, composite)
                        rv.rotation_stage = rot.stage
                        rv.rotation_detail = rot.format_line()
                        _, alloc_str = suggest_allocation(
                            rot, ctx.sentiment_phase)
                        rv.allocation_suggestion = alloc_str
                    except Exception:
                        pass
                rv.timing["L2_rotation"] = round(_time.monotonic() - _t0, 1)
                rv.timing["L2"] = round(
                    rv.timing["L2_rank"] + rv.timing["L2_rotation"], 1)
                _log(f"[复盘] L2 完成 — {rv.timing['L2']}s "
                     f"(排名{rv.timing['L2_rank']}s + 轮动{rv.timing['L2_rotation']}s)")

                # ── L3: 个股复盘 ──
                rv.phase = "L3"
                rv.phase_detail = "准备标的列表..."
                _log("[复盘] L3 开始 — 个股分析")
                _t0 = _time.monotonic()
                ranking_stocks = []
                for r in merged:
                    ranking_stocks.extend(r.pool_codes)
                ranking_stocks = list(dict.fromkeys(ranking_stocks))
                all_symbols = list(dict.fromkeys(
                    config.WHITELIST + ranking_stocks))
                rv.phase_detail = f"分析 {len(all_symbols)} 只个股..."
                _log(f"[复盘] L3 标的: 白名单{len(config.WHITELIST)} + "
                     f"行业{len(ranking_stocks)} = {len(all_symbols)} 只")

                from signals.layers.review_screener import review_stock_daily
                scored = review_stock_daily(all_symbols, start_date)
                # 注入名称
                try:
                    from signals.core.stock_names import get_resolver
                    resolver = get_resolver()
                    if merged:
                        resolver.inject_from_rankings(merged)
                    for s in scored:
                        if not s.name:
                            s.name = resolver.get_name(s.symbol)
                except Exception:
                    pass
                rv.scored_symbols = scored
                rv.timing["L3"] = round(_time.monotonic() - _t0, 1)
                _log(f"[复盘] L3 完成 — {rv.timing['L3']}s ({len(scored)} 个股)")

                # ── 信号回放: 对 top-10 标的生成信号时间线 ──
                _t0 = _time.monotonic()
                rv.phase_detail = "生成信号回放时间线..."
                try:
                    from signals.core.replay import replay_stock
                    from signals.data.bar_cache import get_cache
                    from czsc import Freq as _Freq
                    from datetime import datetime as _dt2
                    today = _dt2.now().strftime("%Y%m%d")
                    replay_count = 0
                    for sc in scored[:10]:
                        cache_key = f"{sc.symbol.replace('.', '_')}_{today}"
                        cached = get_cache().get(cache_key)
                        if cached and len(cached) >= 30:
                            from signals.layers.review_screener import _records_to_rawbars
                            daily_bars = _records_to_rawbars(cached, sc.symbol)
                            timeline = replay_stock(sc.symbol, daily_bars, _Freq.D)
                            if timeline:
                                rv.replay_timelines[sc.symbol] = timeline
                                replay_count += 1
                    rv.timing["replay"] = round(_time.monotonic() - _t0, 1)
                    _log(f"[复盘] 回放完成 — {rv.timing['replay']}s ({replay_count} 标的)")
                except Exception as e:
                    _log(f"[复盘] 回放跳过: {e}")
                    rv.timing["replay"] = 0

                rv.timing["total"] = round(_time.monotonic() - _t_total, 1)
                rv.phase = ""
                rv.phase_detail = ""
                rv.completed = True
                _log(f"[复盘] 全部完成 — 总计{rv.timing['total']}s "
                     f"(L1:{rv.timing.get('L1', 0)}s "
                     f"L2:{rv.timing.get('L2', 0)}s "
                     f"L3:{rv.timing.get('L3', 0)}s) "
                     f"{len(rv.index_reports)}指数 {len(merged)}行业 {len(scored)}个股")
            except Exception as e:
                import traceback
                _log(f"[复盘] 失败: {e}")
                traceback.print_exc()
                rv.error = str(e)
                rv.phase = ""
            finally:
                rv.is_running = False

        t = threading.Thread(target=_worker, daemon=True, name="review-worker")
        t.start()

    def refresh(self) -> bool:
        """重新运行全部分析（L1+L2+L3）"""
        if self._state.is_running:
            return False
        self._state.last_update = 0
        self.run_all_async()
        return True

    def is_ready(self) -> bool:
        """是否已完成至少一次分析"""
        return self._state.last_update > 0

    def get_status(self) -> dict:
        """返回引擎状态摘要（含时段信息）"""
        session = self._state.session_mode
        result = {
            "ready": self.is_ready(),
            "running": self._state.is_running,
            "last_update": self._state.last_update,
            "error": self._state.error,
            "index_count": len(self._state.analyzers),
            "signal_count": len(self._state.scored_symbols),
            "loading_phase": self._state.loading_phase,
        }
        if session:
            from datetime import datetime as _dt
            data_as_of = ""
            if self._state.last_update:
                data_as_of = _dt.fromtimestamp(
                    self._state.last_update).strftime("%H:%M")
            result.update({
                "session_mode": session.name,
                "session_label": session.label,
                "a_live": session.a_live,
                "hk_live": session.hk_live,
                "us_live": session.us_live,
                "refresh_interval": session.refresh_interval,
                "data_as_of": data_as_of,
            })
        return result

    def run_all_async(self):
        """L1+L2 并行启动，L3 等 L1+L2 都完成后执行。自动检测时段。"""
        import time as _time
        from signals.core.market_hours import get_session_mode

        session = get_session_mode()
        with self._lock:
            if self._state.is_running:
                return
            self._state.is_running = True
            self._state.error = ""
            self._state.loading_phase = "L1"
            self._state.session_mode = session

        def _worker():
            import logging
            log = logging.getLogger(__name__)
            t0 = _time.monotonic()
            try:
                # 预加载 layers 包，避免 L1/L2 并行线程争抢模块锁导致 deadlock
                import signals.layers  # noqa: F401

                # ── Phase 1: L1 + L2 并行 ──────────────────────
                self._state.loading_phase = "L1"
                print(f"   [后台] 时段={session.label} "
                      f"(A={'✅' if session.a_live else '❌'} "
                      f"H={'✅' if session.hk_live else '❌'} "
                      f"US={'✅' if session.us_live else '❌'})")
                print("   [后台] L1+L2 并行启动...")

                l2_error = [None]

                def _run_l2():
                    try:
                        self.run_l2(session=session)
                    except Exception as e:
                        l2_error[0] = e

                # L2 在独立线程中并行运行
                l2_thread = threading.Thread(
                    target=_run_l2, daemon=True, name="engine-l2")
                l2_thread.start()

                # L1 在主 worker 线程中运行
                log.info("后台加载: L1 指数分析...")
                print("   [后台] 运行 Layer 1 指数分析...")
                self.run_l1(manage_state=False)
                t1 = _time.monotonic() - t0
                print(f"   [后台] L1 完成 ({t1:.1f}s)")

                # 等 L2 完成（L2 通常比 L1 慢）
                self._state.loading_phase = "L2"
                log.info("后台加载: 等待 L2 行业分析...")
                l2_thread.join(timeout=120)  # 最多等 2 分钟
                t2 = _time.monotonic() - t0
                if l2_error[0]:
                    print(f"   [后台] L2 异常: {l2_error[0]}")
                else:
                    print(f"   [后台] L2 完成 ({t2:.1f}s)")

                # ── Phase 2: L3 串行（依赖 L1+L2 结果）────────
                self._state.loading_phase = "L3"
                l3_mode = "日线复盘" if session.use_daily_l3 else "分钟线盘中"
                log.info("后台加载: L3 标的筛选 (%s)...", l3_mode)
                print(f"   [后台] 运行 Layer 3 标的筛选 ({l3_mode})...")
                self.run_l3(session=session)
                t3 = _time.monotonic() - t0
                print(f"   [后台] L3 完成 ({t3:.1f}s)")

                self._state.loading_phase = ""
                total = _time.monotonic() - t0
                log.info("后台加载: 全部完成")
                print(f"   [后台] ✅ 全部分析完成 (总计 {total:.1f}s)")

                # ── 自动刷新定时器 ──
                if session.refresh_interval > 0:
                    self._schedule_refresh(session.refresh_interval)

            except Exception as e:
                log.error("后台加载失败: %s", e, exc_info=True)
                self._state.error = str(e)
                self._state.loading_phase = ""
            finally:
                with self._lock:
                    self._state.is_running = False
                    if self._state.loading_phase:
                        self._state.loading_phase = ""

        t = threading.Thread(target=_worker, daemon=True, name="engine-loader")
        t.start()

    def _schedule_refresh(self, interval: int):
        """盘中自动刷新：等待 interval 秒后重新检测时段并刷新。"""
        def _tick():
            time.sleep(interval)
            from signals.core.market_hours import get_session_mode
            new_session = get_session_mode()
            self._state.session_mode = new_session
            if new_session.refresh_interval > 0 and not self._state.is_running:
                print(f"   [自动刷新] {new_session.label} — 重新加载...")
                self.run_all_async()
            else:
                print(f"   [自动刷新] {new_session.label} — 已停止刷新")

        t = threading.Thread(target=_tick, daemon=True, name="auto-refresh")
        t.start()


# 全局单例
_engine: Optional[WebEngine] = None


def get_engine() -> WebEngine:
    """获取全局 WebEngine 单例"""
    global _engine
    if _engine is None:
        _engine = WebEngine()
    return _engine
