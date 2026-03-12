# -*- coding: utf-8 -*-
"""
行业工具：
- 获取行业列表 + 成分股（AKShare 东财 + 同花顺双源）
- 行业强度研判（多级降级方案）
  方法 A：行业板块 CZSC（东财 → 同花顺 → pytdx → 缓存）
  方法 B：成分股聚合评分（始终可用）
- 双榜行业排行：涨幅榜 + 综合强度榜
- 多维度个股入池：涨停/异动/领涨/强势/龙头/融资

数据源降级链：
  行业涨幅排行: 东财 → 同花顺 → 缓存
  行业 K 线:    东财 → 同花顺 → pytdx → 缓存
  行业成分股:   东财 → pytdx → 缓存
  概念板块排行: 东财 → 缓存
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

import pandas as pd

from signals.data.fetcher import no_proxy as _no_proxy, em_call_with_retry as _em_retry
from signals.dashboard import get_dashboard as _get_dashboard


# ─────────────────────────────────────────────────────────
# Dashboard 辅助（detail = 细节，log = 重要状态变更）
# ─────────────────────────────────────────────────────────

import logging as _logging
_file_log = _logging.getLogger("signals.industry")

def _detail(msg: str):
    """任务级详情输出（per-task status, 线程池回调等）"""
    dash = _get_dashboard()
    if dash:
        dash.detail(msg)
    else:
        print(msg, flush=True)
    _file_log.info(msg)

def _log(msg: str):
    """重要状态变更输出（熔断、模式切换等）"""
    dash = _get_dashboard()
    if dash:
        dash.log(msg)
    else:
        print(msg, flush=True)
    _file_log.info(msg)


# ─────────────────────────────────────────────────────────
# 基础辅助函数
# ─────────────────────────────────────────────────────────

def _code6_to_futu(code: str) -> str:
    """6位A股代码 → Futu格式（SH.600000 / SZ.000001 / BJ.830799）。"""
    code = str(code).strip().zfill(6)
    if code.startswith("6"):
        return f"SH.{code}"
    elif code.startswith(("0", "3")):
        return f"SZ.{code}"
    elif code.startswith(("8", "4")):
        return f"BJ.{code}"
    return ""


_NAME_TO_CODE: dict = {}
_NAME_CACHE_PATH = str(Path(__file__).resolve().parent.parent.parent / ".cache" / "name_to_code.json")

def _build_name_to_code_map() -> dict:
    """
    股票名称→6位代码映射（三级缓存）：
    1. 内存（_NAME_TO_CODE 非空直接返回）
    2. 本地 JSON（.cache/name_to_code.json，7天有效）
    3. AKShare API（兜底，~60s）
    """
    global _NAME_TO_CODE
    if _NAME_TO_CODE:
        return _NAME_TO_CODE

    import json, os
    from datetime import datetime, timedelta

    # 尝试本地缓存
    try:
        if os.path.exists(_NAME_CACHE_PATH):
            mtime = datetime.fromtimestamp(os.path.getmtime(_NAME_CACHE_PATH))
            if datetime.now() - mtime < timedelta(days=7):
                with open(_NAME_CACHE_PATH, "r", encoding="utf-8") as f:
                    _NAME_TO_CODE.update(json.load(f))
                _detail(f"  [✓] 名称映射从缓存加载（{len(_NAME_TO_CODE)} 只，"
                        f"缓存日期 {mtime:%m-%d}）")
                return _NAME_TO_CODE
    except Exception:
        pass

    # API 拉取
    import akshare as ak
    try:
        df = ak.stock_info_a_code_name()
        if df is not None and not df.empty:
            for _, row in df.iterrows():
                name = str(row.get("name", "")).strip()
                code = str(row.get("code", "")).strip().zfill(6)
                if name and code:
                    _NAME_TO_CODE[name] = code
            # 写入本地缓存
            try:
                os.makedirs(os.path.dirname(_NAME_CACHE_PATH), exist_ok=True)
                with open(_NAME_CACHE_PATH, "w", encoding="utf-8") as f:
                    json.dump(_NAME_TO_CODE, f, ensure_ascii=False)
            except Exception:
                pass
            _detail(f"  [✓] 名称映射已加载（{len(_NAME_TO_CODE)} 只，已缓存）")
    except Exception as e:
        _detail(f"  [!] 股票名称映射加载失败（{e.__class__.__name__}）")
    return _NAME_TO_CODE


# ─────────────────────────────────────────────────────────
# 行业龙头映射（静态：行业名 → (Futu代码, 股票名称)）
# ─────────────────────────────────────────────────────────

_INDUSTRY_LEADERS: dict = {
    "煤炭": ("SH.601088", "中国神华"),
    "石油石化": ("SH.600028", "中国石化"),
    "电力": ("SH.600900", "长江电力"),
    "燃气II": ("SH.600333", "长春燃气"),
    "钢铁": ("SH.600019", "宝钢股份"),
    "工业金属": ("SH.601600", "中国铝业"),
    "小金属": ("SZ.002460", "赣锋锂业"),
    "能源金属": ("SZ.002466", "天齐锂业"),
    "金属新材料": ("SZ.002182", "云海金属"),
    "化学原料": ("SH.600309", "万华化学"),
    "化学制品": ("SZ.002648", "卫星化学"),
    "农化制品": ("SZ.000902", "新洋丰"),
    "电子化学品": ("SZ.300236", "上海新阳"),
    "白酒": ("SH.600519", "贵州茅台"),
    "啤酒": ("SH.600600", "青岛啤酒"),
    "饮料乳品": ("SH.600887", "伊利股份"),
    "食品加工制造": ("SZ.300999", "金龙鱼"),
    "银行": ("SH.601398", "工商银行"),
    "证券": ("SH.601211", "国泰君安"),
    "保险": ("SH.601318", "中国平安"),
    "多元金融": ("SH.600030", "中信证券"),
    "房地产开发": ("SZ.000002", "万科A"),
    "房地产服务": ("SZ.002285", "世联行"),
    "建筑装饰": ("SH.601668", "中国建筑"),
    "建筑材料": ("SH.600585", "海螺水泥"),
    "半导体": ("SZ.002049", "紫光国微"),
    "消费电子": ("SZ.002475", "立讯精密"),
    "元件": ("SZ.300408", "三环集团"),
    "印制电路板": ("SZ.002036", "联创电子"),
    "光学光电子": ("SZ.000725", "京东方A"),
    "其他电子": ("SZ.002371", "北方华创"),
    "计算机设备": ("SZ.000977", "浪潮信息"),
    "软件开发": ("SZ.002230", "科大讯飞"),
    "互联网服务": ("SZ.002555", "三七互娱"),
    "IT设备": ("SZ.000977", "浪潮信息"),
    "通信设备": ("SH.600050", "中国联通"),
    "通信服务": ("SH.600050", "中国联通"),
    "汽车整车": ("SZ.002594", "比亚迪"),
    "汽车零部件": ("SH.600741", "华域汽车"),
    "摩托车": ("SH.603776", "春风动力"),
    "家用电器": ("SZ.000333", "美的集团"),
    "照明设备": ("SZ.300625", "三雄极光"),
    "化学制药": ("SH.600276", "恒瑞医药"),
    "生物制药": ("SZ.300760", "迈瑞医疗"),
    "中药": ("SZ.000538", "云南白药"),
    "医疗器械": ("SZ.300760", "迈瑞医疗"),
    "医疗服务": ("SZ.300015", "爱尔眼科"),
    "医药商业": ("SH.601607", "上海医药"),
    "电网设备": ("SH.600089", "特变电工"),
    "电机": ("SZ.002074", "国轩高科"),
    "风电设备": ("SH.600905", "三峡能源"),
    "光伏设备": ("SH.601012", "隆基绿能"),
    "其他电源设备": ("SZ.300014", "亿纬锂能"),
    "军工装备": ("SH.600893", "航发动力"),
    "军工电子": ("SH.600760", "中航沈飞"),
    "航空航天装备": ("SH.600893", "航发动力"),
    "船舶制造": ("SH.600150", "中国船舶"),
    "轨道交通装备": ("SH.601766", "中国中车"),
    "通用设备": ("SH.600031", "三一重工"),
    "专用设备": ("SZ.300124", "汇川技术"),
    "工程机械": ("SH.600031", "三一重工"),
    "仪器仪表": ("SH.600590", "泰豪科技"),
    "种植业与林业": ("SZ.000998", "隆平高科"),
    "养殖业": ("SZ.002714", "牧原股份"),
    "水产": ("SZ.002069", "獐子岛"),
    "旅游景区": ("SH.600138", "中青旅"),
    "酒店餐饮": ("SH.600754", "锦江酒店"),
    "航空运输": ("SH.600029", "南方航空"),
    "航运港口": ("SH.601919", "中远海控"),
    "环保设备": ("SZ.300070", "碧水源"),
    "环保服务": ("SZ.300070", "碧水源"),
    "游戏": ("SZ.002602", "世纪华通"),
    "影视院线": ("SZ.300251", "光线传媒"),
    "广告营销": ("SZ.002027", "分众传媒"),
    "教育": ("SZ.003032", "传智教育"),
    "造纸印刷": ("SZ.000488", "晨鸣纸业"),
    "橡胶": ("SH.601966", "玲珑轮胎"),
    "塑料": ("SZ.000973", "佛塑科技"),
    "水务": ("SH.600323", "瀚蓝环境"),
    "有色金属": ("SH.601899", "紫金矿业"),
}


# ─────────────────────────────────────────────────────────
# 磁盘缓存工具 — 所有降级链的最后兜底
# ─────────────────────────────────────────────────────────
_CACHE_DIR = Path(".data/cache")


def _save_cache(key: str, data):
    """保存 JSON 缓存。data 必须是 json-serializable (list/dict)。"""
    import json
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


def _load_cache(key: str, max_age_hours: float = 24):
    """加载 JSON 缓存，超过 max_age_hours 返回 None。"""
    import json, time
    path = _CACHE_DIR / f"{key}.json"
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def _save_bar_cache(key: str, bars):
    """K 线缓存（RawBar 是 Rust 类型不可 pickle，转为 JSON）。"""
    import json
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _CACHE_DIR / f"{key}_bars.json"
    data = []
    for b in bars:
        data.append({
            "dt": str(b.dt), "open": b.open, "high": b.high,
            "low": b.low, "close": b.close, "vol": b.vol,
            "amount": getattr(b, "amount", 0),
        })
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f)


def _load_bar_cache(key: str, max_age_hours: float = 24):
    """加载 K 线缓存，重建为 RawBar 列表。"""
    import json, time
    from czsc import RawBar, Freq
    from datetime import datetime

    path = _CACHE_DIR / f"{key}_bars.json"
    if not path.exists():
        return None
    age_hours = (time.time() - path.stat().st_mtime) / 3600
    if age_hours > max_age_hours:
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        bars = []
        for d in data:
            bars.append(RawBar(
                symbol=key.replace("bars_", ""), freq=Freq.D,
                dt=datetime.fromisoformat(str(d["dt"])),
                open=d["open"], high=d["high"], low=d["low"],
                close=d["close"], vol=d["vol"],
                amount=d.get("amount", 0),
            ))
        return bars if bars else None
    except Exception:
        return None


# ─────────────────────────────────────────────────────────
# 同花顺 (THS) 降级函数
# ─────────────────────────────────────────────────────────
_THS_CIRCUIT_OPEN = False  # 同花顺熔断标志


def _fetch_ths_industry_ranking(top_n: int = 10) -> Optional[list]:
    """
    同花顺行业涨幅排行（stock_board_industry_summary_ths）。
    返回与东财 get_top_industries_by_gain 相同格式的 list of dict。
    超时 15s，失败返回 None。
    """
    global _THS_CIRCUIT_OPEN
    if _THS_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        return ak.stock_board_industry_summary_ths()

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_call)
            df = future.result(timeout=15)
    except (FutureTimeout, Exception) as e:
        _THS_CIRCUIT_OPEN = True
        _log(f"  [⚡] 同花顺行业排行接口失败（{e.__class__.__name__}），熔断")
        return None

    if df is None or df.empty or "涨跌幅" not in df.columns:
        return None

    df = df.sort_values("涨跌幅", ascending=False).head(top_n)
    result = []
    for _, row in df.iterrows():
        leading = str(row.get("领涨股", ""))
        leading_gain = 0.0
        try:
            leading_gain = float(row.get("领涨股-涨跌幅", 0) or 0)
        except (ValueError, TypeError):
            pass
        net_inflow = 0.0
        try:
            net_inflow = float(row.get("净流入", 0) or 0)
        except (ValueError, TypeError):
            pass
        result.append({
            "name":          str(row.get("板块", "")),
            "gain_pct":      float(row.get("涨跌幅", 0) or 0),
            "net_inflow":    net_inflow,
            "leading_stock": leading,
            "leading_gain":  leading_gain,
        })
    _log(f"  [THS] 行业排行降级成功（{len(result)}条）")
    return result


def _fetch_ths_industry_bars(industry: str,
                              lookback_days: int = 180,
                              start_date: str = None):
    """
    同花顺行业 K 线（stock_board_industry_index_ths）。
    返回 List[RawBar]，失败返回空列表。
    """
    global _THS_CIRCUIT_OPEN
    if _THS_CIRCUIT_OPEN:
        return []

    import akshare as ak
    from datetime import datetime, timedelta
    from czsc import RawBar, Freq
    from signals.data.fetcher import _to_raw_bars
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    today = datetime.now()
    if start_date:
        s_date = start_date.replace("-", "")
    else:
        s_date = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    e_date = today.strftime("%Y%m%d")

    def _call():
        return ak.stock_board_industry_index_ths(
            symbol=industry, start_date=s_date, end_date=e_date
        )

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(_call)
            df = future.result(timeout=15)
    except (FutureTimeout, Exception) as e:
        _detail(f"  [THS] {industry} K线失败（{e.__class__.__name__}）")
        return []

    if df is None or df.empty:
        return []

    # 同花顺列名：日期, 开盘价, 最高价, 最低价, 收盘价, 成交量, 成交额
    col_map = {}
    for src, dst in [("日期", "dt"), ("开盘价", "open"), ("最高价", "high"),
                     ("最低价", "low"), ("收盘价", "close"), ("成交量", "vol"),
                     ("成交额", "amount")]:
        if src in df.columns:
            col_map[src] = dst
    df = df.rename(columns=col_map)
    if "amount" not in df.columns:
        df["amount"] = 0

    bars = _to_raw_bars(df, industry, Freq.D,
                        "dt", "open", "high", "low", "close", "vol", "amount")
    if bars:
        _detail(f"  [THS] {industry} K线降级成功（{len(bars)}根）")
    return bars


# ─────────────────────────────────────────────────────────
# 东财→pytdx 映射（成分股代码重叠匹配）
# ─────────────────────────────────────────────────────────
_EM_TDX_MAP: Optional[dict] = None   # {"白酒": "880437", ...}


def _get_em_tdx_map() -> dict:
    """获取东财行业名→pytdx 880xxx 代码映射（带磁盘缓存）。"""
    global _EM_TDX_MAP
    if _EM_TDX_MAP is not None:
        return _EM_TDX_MAP

    # 尝试磁盘缓存（7天有效）
    cached = _load_cache("em_tdx_map", max_age_hours=168)
    if cached:
        _EM_TDX_MAP = cached
        return _EM_TDX_MAP

    _EM_TDX_MAP = {}
    return _EM_TDX_MAP


def _build_em_tdx_mapping() -> dict:
    """
    构建东财行业名→pytdx 880xxx 代码映射。
    通过成分股代码重叠度匹配。调用耗时较长（需遍历行业），
    在 rank_industries() 中异步触发。
    """
    global _EM_TDX_MAP
    import akshare as ak

    try:
        from signals.data.pytdx_source import PytdxSource
        src = PytdxSource()
        tdx_blocks = src.get_board_stocks()
        src.disconnect()
    except Exception as e:
        _detail(f"  [pytdx] 板块数据获取失败（{e.__class__.__name__}），跳过映射构建")
        return {}

    if not tdx_blocks:
        return {}

    # 获取东财行业名称列表
    df = _fetch_board_industry_name_em()
    if df is None or df.empty or "板块名称" not in df.columns:
        return {}

    mapping = {}
    industry_names = df["板块名称"].tolist()[:30]  # 只匹配前30个热门行业减少耗时

    for name in industry_names:
        try:
            with _no_proxy():
                cons_df = ak.stock_board_industry_cons_em(symbol=name)
        except Exception:
            continue
        if cons_df is None or cons_df.empty:
            continue

        # 提取6位股票代码
        code_col = None
        for col in ["代码", "code", "股票代码"]:
            if col in cons_df.columns:
                code_col = col
                break
        if not code_col:
            continue

        em_codes = set(str(c).zfill(6) for c in cons_df[code_col].astype(str))

        # 找到重叠度最高的 pytdx 板块
        best_code, best_overlap = None, 0
        for tdx_code, tdx_codes in tdx_blocks.items():
            overlap = len(em_codes & tdx_codes)
            if overlap > best_overlap:
                best_overlap = overlap
                best_code = tdx_code
        if best_code and best_overlap >= 3:
            mapping[name] = best_code

    if mapping:
        _save_cache("em_tdx_map", mapping)
        _EM_TDX_MAP = mapping
        _detail(f"  [pytdx] 映射构建完成（{len(mapping)}个行业）")

    return mapping


def _fetch_pytdx_industry_bars(industry: str, count: int = 800):
    """
    pytdx 行业 K 线降级（通过映射表查 880xxx 代码）。
    返回 List[RawBar]，找不到映射或失败返回空列表。
    """
    tdx_map = _get_em_tdx_map()
    tdx_code = tdx_map.get(industry)
    if not tdx_code:
        return []

    try:
        from signals.data.pytdx_source import PytdxSource
        src = PytdxSource()
        bars = src.get_board_daily(tdx_code, count=count)
        src.disconnect()
        if bars:
            _detail(f"  [pytdx] {industry} K线降级成功（{tdx_code}, {len(bars)}根）")
        return bars
    except Exception as e:
        _detail(f"  [pytdx] {industry} K线失败（{e.__class__.__name__}）")
        return []


def _fetch_pytdx_industry_stocks(industry: str) -> List[str]:
    """
    pytdx 行业成分股降级。返回 Futu 格式代码列表。
    """
    tdx_map = _get_em_tdx_map()
    tdx_code = tdx_map.get(industry)
    if not tdx_code:
        return []

    try:
        from signals.data.pytdx_source import PytdxSource
        src = PytdxSource()
        all_blocks = src.get_board_stocks()
        src.disconnect()
    except Exception:
        return []

    codes = all_blocks.get(tdx_code, set())
    if not codes:
        return []

    futu_codes = []
    for code in codes:
        code = code.zfill(6)
        if code.startswith("6"):
            futu_codes.append(f"SH.{code}")
        elif code.startswith(("0", "3")):
            futu_codes.append(f"SZ.{code}")
        elif code.startswith(("8", "4")):
            futu_codes.append(f"BJ.{code}")
    if futu_codes:
        _detail(f"  [pytdx] {industry} 成分股降级成功（{len(futu_codes)}只）")
    return futu_codes


# ─────────────────────────────────────────────────────────
# 东财 API 熔断器 — SSLError 一次即熔断，后续调用直接走缓存
# ─────────────────────────────────────────────────────────
_EM_CIRCUIT_OPEN = False          # True = 东财不可用，跳过网络调用
_EM_NAME_DF_CACHE: Optional[pd.DataFrame] = None   # 首次成功结果缓存
_EM_HEALTH_CHECKED = False        # 启动时已做过健康预检


def _em_health_probe(timeout: float = 3.0) -> bool:
    """
    东财 API 快速健康探测（3s）。
    启动时调用一次，不可用则全局熔断，避免后续每个接口都等 10-25s 超时。
    """
    global _EM_CIRCUIT_OPEN, _EM_HEALTH_CHECKED
    if _EM_HEALTH_CHECKED:
        return not _EM_CIRCUIT_OPEN
    _EM_HEALTH_CHECKED = True

    # 预先检测 Clash 代理，避免所有东财调用超时
    from signals.data.fetcher import _try_fix_clash_global_mode
    _try_fix_clash_global_mode()

    import akshare as ak
    import time as _htime
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    # 最多尝试 3 次，每次独立超时
    max_attempts = 3
    for attempt in range(max_attempts):
        def _ping():
            with _no_proxy():
                return ak.stock_board_industry_name_em()

        _probe_pool = ThreadPoolExecutor(max_workers=1)
        _probe_future = _probe_pool.submit(_ping)
        try:
            df = _probe_future.result(timeout=timeout)
            if df is not None and not df.empty:
                global _EM_NAME_DF_CACHE
                _EM_NAME_DF_CACHE = df
                _log("  [✓] 东财 API 健康探测通过（复用数据）")
                return True
        except (FutureTimeout, Exception) as e:
            if attempt < max_attempts - 1:
                _log(f"  [⚡] 东财健康探测第{attempt+1}次失败（{e.__class__.__name__}），重试...")
                _htime.sleep(0.5 * (attempt + 1))
                continue
            _EM_CIRCUIT_OPEN = True
            _log(f"  [⚡] 东财 API 健康探测{max_attempts}次均失败（{e.__class__.__name__}），全局熔断")
            return False
        finally:
            _probe_pool.shutdown(wait=False, cancel_futures=True)
    return False


def _fetch_board_industry_name_em(timeout: float = 5.0) -> Optional[pd.DataFrame]:
    """
    带熔断+重试的 stock_board_industry_name_em 调用：
    - 整体超时 5s（从10s缩减）
    - 健康预检失败则直接跳过
    - 首次成功后缓存结果，后续直接返回
    - 两次失败后标记熔断，本次运行内不再尝试
    """
    global _EM_CIRCUIT_OPEN, _EM_NAME_DF_CACHE

    # 有缓存直接返回
    if _EM_NAME_DF_CACHE is not None:
        return _EM_NAME_DF_CACHE

    # 熔断打开 → 跳过
    if _EM_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        return ak.stock_board_industry_name_em()

    # 健康预检通过后才尝试（预检已缓存结果，直接返回）
    _em_pool = ThreadPoolExecutor(max_workers=1)
    _em_future = _em_pool.submit(_call)
    try:
        df = _em_future.result(timeout=timeout)
        if df is not None and not df.empty:
            _EM_NAME_DF_CACHE = df
            return df
        return None
    except FutureTimeout:
        _EM_CIRCUIT_OPEN = True
        _log(f"  [⚡] 东财行业接口超时（>{timeout}s），熔断")
        return None
    except Exception as e:
        _EM_CIRCUIT_OPEN = True
        _log(f"  [⚡] 东财行业接口熔断（{e.__class__.__name__}）")
        return None
    finally:
        _em_pool.shutdown(wait=False, cancel_futures=True)
    return None


# ─────────────────────────────────────────────────────────
# 同花顺 THS 行业排行（降级源 #1）
# ─────────────────────────────────────────────────────────
_THS_CIRCUIT_OPEN = False          # True = 同花顺不可用
_THS_RANKING_CACHE: Optional[pd.DataFrame] = None


def _fetch_industry_ranking_ths(timeout: float = 10.0) -> Optional[pd.DataFrame]:
    """
    同花顺行业排行 → 统一列名 DataFrame。
    stock_board_industry_summary_ths() 返回 90 行业，分类与东财一致。
    列映射：板块 → 板块名称，领涨股 → 领涨股票，领涨股-涨跌幅 → 领涨股票-涨跌幅
    """
    global _THS_CIRCUIT_OPEN, _THS_RANKING_CACHE

    if _THS_RANKING_CACHE is not None:
        return _THS_RANKING_CACHE
    if _THS_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        with _no_proxy():
            return ak.stock_board_industry_summary_ths()

    _pool = ThreadPoolExecutor(max_workers=1)
    _future = _pool.submit(_call)
    try:
        df = _future.result(timeout=timeout)
        if df is not None and not df.empty:
            # 统一列名，使下游代码无需感知数据来源
            rename_map = {
                "板块": "板块名称",
                "领涨股": "领涨股票",
                "领涨股-涨跌幅": "领涨股票-涨跌幅",
            }
            df = df.rename(columns=rename_map)
            _THS_RANKING_CACHE = df
            _detail(f"  [✓] 同花顺行业排行 {len(df)} 条")
            return df
        return None
    except FutureTimeout:
        _THS_CIRCUIT_OPEN = True
        _log(f"  [⚡] 同花顺行业排行超时（>{timeout}s），熔断")
        return None
    except Exception as e:
        _THS_CIRCUIT_OPEN = True
        _log(f"  [⚡] 同花顺行业排行失败（{e.__class__.__name__}），熔断")
        return None
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)


def _fetch_industry_ranking_with_fallback() -> Optional[pd.DataFrame]:
    """
    行业排行降级链：同花顺(4.5s) → 东财(5s) → None
    返回统一列名的 DataFrame（板块名称, 涨跌幅, 领涨股票, 领涨股票-涨跌幅）。
    """
    # 优先同花顺（更稳定）
    df = _fetch_industry_ranking_ths()
    if df is not None and not df.empty:
        return df
    # 降级到东财
    df = _fetch_board_industry_name_em()
    if df is not None and not df.empty:
        return df
    return None


# ─────────────────────────────────────────────────────────
# 基础接口（已有）
# ─────────────────────────────────────────────────────────

def get_industry_list() -> pd.DataFrame:
    """返回 A 股所有行业名称列表（同花顺 → 东财降级）。"""
    df = _fetch_industry_ranking_with_fallback()
    if df is not None:
        return df
    return pd.DataFrame()


def get_industry_stocks(industry: str) -> List[str]:
    """
    获取指定行业的成分股，返回 Futu 格式代码列表。

    降级链：东财 cons_em → pytdx block.dat → 磁盘缓存

    :param industry: 行业名称，如 "有色金属"、"半导体"
    :return: ["SH.600489", "SZ.002460", ...]
    """
    import akshare as ak
    _cache_key = f"stocks_{industry}"

    # ── 1. 东财 cons_em ──────────────────────────────
    try:
        df = _em_retry(ak.stock_board_industry_cons_em, symbol=industry, retries=2, delay=0.5)
    except Exception as e:
        _detail(f"  [!] {industry} 东财成分股失败（{e.__class__.__name__}）")

    # ── 2. pytdx block.dat 降级 ──────────────────────
    pytdx_codes = _fetch_pytdx_industry_stocks(industry)
    if pytdx_codes:
        _save_cache(_cache_key, pytdx_codes)
        return pytdx_codes

    # ── 3. 磁盘缓存兜底 ─────────────────────────────
    cached = _load_cache(_cache_key, max_age_hours=168)  # 成分股变动少，7天缓存
    if cached:
        _detail(f"  [cache] {industry} 成分股使用缓存（{len(cached)}只）")
        return cached
    return []


# ─────────────────────────────────────────────────────────
# IndustryScore 数据类
# ─────────────────────────────────────────────────────────

@dataclass
class IndustryScore:
    """行业强度评分结果"""
    name: str                          # 行业名称
    method: str = "unknown"            # "czsc" / "members" / "unavailable"
    avg_score: float = 0.0             # 平均评分
    buy_ratio: float = 0.0             # 成分股中有买信号的比例
    bullish_ratio: float = 0.0         # 上涨趋势成分股比例（czsc方法）
    bi_count: int = 0                  # 笔数（czsc方法）
    trend: str = "未知"                # 板块趋势（czsc方法）
    latest_signal: str = "无"          # 板块最新信号（czsc方法）
    top_stocks: List[str] = field(default_factory=list)  # 最强成分股代码列表
    error: str = ""                    # 异常信息

    @property
    def is_strong(self) -> bool:
        """是否为强势行业（综合判断）"""
        if self.method == "czsc":
            return self.trend == "上涨趋势" or "买" in self.latest_signal
        return self.buy_ratio > 0.3

    @property
    def summary(self) -> str:
        if self.method == "czsc":
            sig = f" | {self.latest_signal}" if self.latest_signal != "无" else ""
            return f"{self.name} [{self.trend}{sig}]（CZSC方法）"
        elif self.method == "members":
            top_str = ", ".join(self.top_stocks[:3]) if self.top_stocks else "无"
            return (f"{self.name} 平均分={self.avg_score:.1f} "
                    f"买信号占比={self.buy_ratio:.0%} "
                    f"强势股: {top_str}（成分股聚合）")
        return f"{self.name} 数据不可用"


# ─────────────────────────────────────────────────────────
# 新增数据类：多维度选股
# ─────────────────────────────────────────────────────────

@dataclass
class StockCandidate:
    """个股候选（来自某一维度）"""
    code: str          # Futu格式代码 (SH.600000)
    name: str          # 股票名称
    role: str          # "涨停" / "异动" / "领涨" / "强势" / "龙头"
    priority: int      # 优先级（1最高：涨停>异动>领涨>强势>龙头）
    detail: str = ""   # 附加信息（连板数、量比等）


@dataclass
class IndustryRanking:
    """行业排名结果（双榜 + 代表股）"""
    name: str                                          # 行业名称
    gain_rank: int = 0                                 # 涨幅榜排名（0=未上榜）
    composite_rank: int = 0                            # 综合榜排名（0=未上榜）
    gain_pct: float = 0.0                              # 今日涨幅%
    net_inflow: float = 0.0                            # 净流入（亿元）
    composite_score: float = 0.0                       # 综合评分（0-100）
    source: str = "gain"                               # "gain" / "composite" / "both"
    candidates: List[StockCandidate] = field(default_factory=list)
    zt_count: int = 0                                  # 涨停家数
    strong_count: int = 0                              # 强势股家数
    zbgc_count: int = 0                                # 昨涨停续板家数
    sector_type: str = "中性"                           # 防守/进攻/周期/中性
    concept_tags: List[str] = field(default_factory=list)  # 关联的热门概念名
    oversold_score: float = 0.0                           # 超跌评分(0-100)
    oversold_detail: str = ""                             # "距高点-18%"
    rotation_line: str = ""                                # 轮动线: 科技/顺周期/消费/新能源/主题/公用
    # P3-3: 板块节奏
    rhythm_phase: str = ""                                 # "启动"/"加速"/"高潮"/"衰竭"/"休整"
    rhythm_score: float = 0.0                              # 0-100
    rhythm_hint: str = ""                                  # "可加仓"/"持有"/"兑现"/"回避"

    @property
    def display_name(self) -> str:
        """行业名+属性标签+概念标签（括号化显示）"""
        _STYPE_ICON = {"防守": "🛡", "进攻": "⚔", "周期": "🔄", "中性": ""}
        icon = _STYPE_ICON.get(self.sector_type, "")
        tag = f"[{self.sector_type}{icon}]" if self.sector_type != "中性" else ""
        concept = f"({'、'.join(self.concept_tags[:3])})" if self.concept_tags else ""
        return f"{self.name}{tag}{concept}"

    @property
    def pool_codes(self) -> List[str]:
        """去重后的入池代码列表"""
        seen: set = set()
        codes: list = []
        for c in self.candidates:
            if c.code and c.code not in seen:
                seen.add(c.code)
                codes.append(c.code)
        return codes


# ─────────────────────────────────────────────────────────
# 行业 K 线磁盘缓存（RawBar 不可 pickle，用 JSON）
# ─────────────────────────────────────────────────────────

def _bars_cache_path(industry: str) -> Path:
    cache_dir = Path(__file__).resolve().parent.parent.parent / ".data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    safe_name = industry.replace("/", "_").replace("\\", "_")
    return cache_dir / f"bars_{safe_name}.json"


def _save_bars_cache(industry: str, bars, source: str = "em"):
    """保存行业 K 线到 JSON 磁盘缓存"""
    import json, time
    data = {
        "ts": time.time(),
        "source": source,
        "industry": industry,
        "bars": [
            {
                "dt": bar.dt.isoformat() if hasattr(bar.dt, 'isoformat') else str(bar.dt),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "vol": int(bar.vol),
                "amount": int(bar.amount) if hasattr(bar, 'amount') else 0,
            }
            for bar in bars
        ],
    }
    try:
        _bars_cache_path(industry).write_text(
            json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_bars_cache(industry: str, max_age: float = 86400):
    """从磁盘缓存加载行业 K 线（默认24h过期）。"""
    import json, time
    from czsc import RawBar, Freq

    path = _bars_cache_path(industry)
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > max_age:
            return []
        items = data.get("bars", [])
        if not items:
            return []
        bars = []
        for i, item in enumerate(items):
            bars.append(RawBar(
                symbol=industry,
                dt=pd.Timestamp(item["dt"]),
                id=i,
                freq=Freq.D,
                open=float(item["open"]),
                high=float(item["high"]),
                low=float(item["low"]),
                close=float(item["close"]),
                vol=int(item.get("vol", 0)),
                amount=int(item.get("amount", 0)),
            ))
        source = data.get("source", "unknown")
        _detail(f"  [缓存] {industry} K线 {len(bars)} 根（磁盘缓存, 来源:{source}）")
        return bars
    except Exception:
        return []


# ─────────────────────────────────────────────────────────
# 方法 A：行业板块 CZSC（多源降级 K 线）
# ─────────────────────────────────────────────────────────

def _get_industry_bars_em(industry: str, s_date: str, e_date: str):
    """东财行业 K 线（原始方法）"""
    if _EM_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    # ── 1. 东财 hist_em（现有，15s 超时）──────────────
    if not _EM_CIRCUIT_OPEN:
        def _call():
            return ak.stock_board_industry_hist_em(
                symbol=industry, period="daily",
                start_date=s_date, end_date=e_date,
                adjust="qfq"
            )

    _pool = ThreadPoolExecutor(max_workers=1)
    _future = _pool.submit(_call)
    try:
        df = _future.result(timeout=6)
        if df is not None and not df.empty:
            # 东财列名映射
            col_map = {}
            for src, dst in [("日期", "dt"), ("开盘", "open"), ("最高", "high"),
                             ("最低", "low"), ("收盘", "close"), ("成交量", "vol"),
                             ("成交额", "amount"), ("date", "dt"), ("volume", "vol")]:
                if src in df.columns:
                    col_map[src] = dst
            df = df.rename(columns=col_map)
            if "amount" not in df.columns:
                df["amount"] = 0
            return df
        return None
    except (FutureTimeout, Exception) as e:
        _log(f"  [!] {industry} 东财K线超时/失败（{e.__class__.__name__}）")
        return None
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)


def _get_industry_bars_ths(industry: str, s_date: str, e_date: str):
    """
    同花顺行业 K 线降级源。
    stock_board_industry_index_ths 列名: 日期, 开盘价, 最高价, 最低价, 收盘价, 成交量, 成交额
    """
    if _THS_CIRCUIT_OPEN:
        return None

    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    # THS 接口用 YYYY-MM-DD 格式
    s_fmt = f"{s_date[:4]}-{s_date[4:6]}-{s_date[6:]}"
    e_fmt = f"{e_date[:4]}-{e_date[4:6]}-{e_date[6:]}"

    def _call():
        with _no_proxy():
            return ak.stock_board_industry_index_ths(
                symbol=industry, start_date=s_fmt, end_date=e_fmt)

    _pool = ThreadPoolExecutor(max_workers=1)
    _future = _pool.submit(_call)
    try:
        df = _future.result(timeout=12)
        if df is not None and not df.empty:
            # THS 列名映射（与东财不同）
            col_map = {}
            for src, dst in [("日期", "dt"), ("开盘价", "open"), ("最高价", "high"),
                             ("最低价", "low"), ("收盘价", "close"), ("成交量", "vol"),
                             ("成交额", "amount")]:
                if src in df.columns:
                    col_map[src] = dst
            df = df.rename(columns=col_map)
            if "amount" not in df.columns:
                df["amount"] = 0
            _detail(f"  [✓] {industry} THS K线 {len(df)} 根")
            return df
        return None
    except (FutureTimeout, Exception) as e:
        _detail(f"  [!] {industry} THS K线失败（{e.__class__.__name__}）")
        return None
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)


def get_industry_bars(industry: str,
                      lookback_days: int = 180,
                      start_date: str = None):
    """
    获取行业板块日线 K 线。
    降级链：东财(6s) → 同花顺(12s) → 磁盘缓存(24h) → 空

    :param industry:     行业名称，如 "有色金属"
    :param lookback_days: 盘中模式：近 N 自然日（默认180）
    :param start_date:   盘后模式：固定起点，如 '2024-09-24'
    :return: List[RawBar]，失败返回空列表
    """
    from datetime import datetime, timedelta
    from czsc import Freq
    from signals.data.fetcher import _to_raw_bars

    today = datetime.now()
    if start_date:
        s_date = start_date.replace("-", "")
    else:
        s_date = (today - timedelta(days=lookback_days)).strftime("%Y%m%d")
    e_date = today.strftime("%Y%m%d")

    # 1. 东财
    df = _get_industry_bars_em(industry, s_date, e_date)
    if df is not None:
        bars = _to_raw_bars(df, industry, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        if bars:
            _save_bars_cache(industry, bars, source="em")
            return bars

    # 2. 同花顺降级
    df = _get_industry_bars_ths(industry, s_date, e_date)
    if df is not None:
        bars = _to_raw_bars(df, industry, Freq.D,
                            "dt", "open", "high", "low", "close", "vol", "amount")
        if bars:
            _save_bars_cache(industry, bars, source="ths")
            return bars

    # 3. 磁盘缓存
    cached = _load_bars_cache(industry)
    if cached:
        return cached

    return []


def score_industry_czsc(industry: str,
                        lookback_days: int = 180,
                        start_date: str = None) -> IndustryScore:
    """
    方法 A：对行业板块指数做 CZSC 分析。
    东财 SSL 超时时抛出异常，调用方降级到 score_industry_by_members()。
    """
    from .index_analyzer import IndexAnalyzer

    bars = get_industry_bars(industry, lookback_days=lookback_days,
                             start_date=start_date)
    if not bars:
        return IndustryScore(name=industry, method="unavailable",
                             error="东财数据为空")

    az = IndexAnalyzer(name=industry, symbol=industry,
                       daily_bars=bars)
    r = az.report()
    return IndustryScore(
        name=industry,
        method="czsc",
        trend=r.daily_trend,
        latest_signal=r.daily_latest_signal,
        bi_count=r.daily_bi_count,
        bullish_ratio=1.0 if r.is_bullish else 0.0,
        buy_ratio=1.0 if r.has_buy_signal else 0.0,
    )


# ─────────────────────────────────────────────────────────
# 方法 B：成分股聚合评分（降级方案，始终可用）
# ─────────────────────────────────────────────────────────

def score_industry_by_members(industry: str,
                               sample_size: int = 20,
                               freqs: list = None) -> IndustryScore:
    """
    方法 B：获取行业成分股 → 对每只跑 Layer 3 → 取平均分。

    :param industry:    行业名称
    :param sample_size: 最多分析前 N 只成分股（默认 20，控制耗时）
    :param freqs:       分析频率列表，默认 [Freq.F15, Freq.F30]
    :return: IndustryScore
    """
    from czsc import Freq as CFreq
    from .screener import IntraDayScreener

    if freqs is None:
        freqs = [CFreq.F15, CFreq.F30]

    stocks = get_industry_stocks(industry)
    if not stocks:
        return IndustryScore(name=industry, method="unavailable",
                             error="成分股获取失败")

    sample = stocks[:sample_size]
    screener = IntraDayScreener(symbols=sample, freqs=freqs)
    try:
        screener.initialize()
    except Exception as e:
        return IndustryScore(name=industry, method="unavailable",
                             error=f"成分股数据加载失败：{e}")

    results = screener.scan_once()
    if not results:
        return IndustryScore(name=industry, method="members",
                             avg_score=0.0, buy_ratio=0.0,
                             top_stocks=sample[:5])

    scores = [r.total_score for r in results]
    buy_count = sum(1 for r in results if r.total_score > 0)
    avg = sum(scores) / len(scores)
    top5 = [r.symbol for r in sorted(results, key=lambda x: -x.total_score)[:5]]

    return IndustryScore(
        name=industry,
        method="members",
        avg_score=avg,
        buy_ratio=buy_count / len(results),
        top_stocks=top5,
    )


# ─────────────────────────────────────────────────────────
# 统一入口：自动降级
# ─────────────────────────────────────────────────────────

def score_industry(industry: str,
                   lookback_days: int = 180,
                   start_date: str = None,
                   sample_size: int = 20) -> IndustryScore:
    """
    行业强度研判统一入口，自动两级降级：
    方法 A（东财 CZSC）→ 方法 B（成分股聚合）。

    :param industry:      行业名称
    :param lookback_days: 盘中模式窗口（默认180自然日）
    :param start_date:    盘后模式固定起点
    :param sample_size:   方法 B 成分股抽样数量
    :return: IndustryScore
    """
    # 方法 A：东财行业K线 CZSC
    try:
        result = score_industry_czsc(industry, lookback_days=lookback_days,
                                     start_date=start_date)
        if result.method == "czsc":
            return result
    except Exception as e:
        _detail(f"  [!] {industry} 东财接口失败（{e}），降级到成分股聚合")

    # 方法 B：成分股聚合评分
    try:
        return score_industry_by_members(industry, sample_size=sample_size)
    except Exception as e:
        return IndustryScore(name=industry, method="unavailable",
                             error=str(e))


# ─────────────────────────────────────────────────────────
# 行业涨幅排行（Layer 2a）
# ─────────────────────────────────────────────────────────

def get_top_industries_by_gain(top_n: int = 10, period: str = "今日") -> list:
    """
    获取行业涨幅排行前 top_n 名。

    数据源降级链：
    A. 东财 stock_board_industry_name_em（带熔断）
    B. 同花顺 stock_board_industry_summary_ths（降级）
    C. 东财 stock_board_change_em（全板块异动，再降级）
    D. 磁盘缓存兜底

    :param top_n:   取前N名行业，默认10
    :param period:  暂未使用（接口均为实时数据）
    :return: list of dict，每项含 name/gain_pct/net_inflow/leading_stock/leading_gain
    """
    import akshare as ak

    _cache_key = "industry_gain_ranking"

    # ── 方法 A：东财行业板块实时行情（带熔断）──────────────
    df = _fetch_board_industry_name_em()
    if df is not None and not df.empty and "涨跌幅" in df.columns and "板块名称" in df.columns:
        df = df.sort_values("涨跌幅", ascending=False).head(top_n)
        result = []
        for _, row in df.iterrows():
            leading = str(row.get("领涨股票", ""))
            leading_gain = 0.0
            try:
                leading_gain = float(row.get("领涨股票-涨跌幅", 0) or 0)
            except (ValueError, TypeError):
                pass
            result.append({
                "name":          str(row["板块名称"]),
                "gain_pct":      float(row.get("涨跌幅", 0) or 0),
                "net_inflow":    0.0,   # 该接口无资金流向
                "leading_stock": leading,
                "leading_gain":  leading_gain,
            })
        _save_cache(_cache_key, result)
        return result

    # ── 方法 B：同花顺行业排行（降级）─────────────────
    ths_result = _fetch_ths_industry_ranking(top_n)
    if ths_result:
        _save_cache(_cache_key, ths_result)
        return ths_result

    # ── 方法 C：东财全板块异动（再降级）────────────────
    try:
        df_all = ak.stock_board_change_em()
        if df_all is not None and not df_all.empty:
            industry_names = _get_known_industry_names()
            df_ind = df_all[df_all["板块名称"].isin(industry_names)] if industry_names else df_all
            df_ind = df_ind.sort_values("涨跌幅", ascending=False).head(top_n)
            result = []
            for _, row in df_ind.iterrows():
                leading_code = str(row.get("板块异动最频繁个股及所属类型-股票代码", ""))
                result.append({
                    "name":          str(row["板块名称"]),
                    "gain_pct":      float(row.get("涨跌幅", 0) or 0),
                    "net_inflow":    float(row.get("主力净流入", 0) or 0) / 1e8,
                    "leading_stock": leading_code,
                    "leading_gain":  0.0,
                })
            if result:
                _save_cache(_cache_key, result)
                return result
    except Exception as e:
        _detail(f"  [!] 全板块异动接口也失败（{e}）")

    # ── 方法 D：磁盘缓存兜底 ────────────────────────
    cached = _load_cache(_cache_key, max_age_hours=24)
    if cached:
        _log(f"  [cache] 行业排行使用缓存（{len(cached)}条）")
        return cached
    return []


# 静态行业名缓存（供方法B过滤，首次调用时尝试从接口加载，失败则用内置集合）
_KNOWN_INDUSTRY_NAMES: Optional[set] = None

def _get_known_industry_names() -> set:
    """返回已知东财行业名称集合，优先从接口加载，失败则返回内置集合。"""
    global _KNOWN_INDUSTRY_NAMES
    if _KNOWN_INDUSTRY_NAMES is not None:
        return _KNOWN_INDUSTRY_NAMES

    df = _fetch_board_industry_name_em()
    if df is not None and not df.empty and "板块名称" in df.columns:
        _KNOWN_INDUSTRY_NAMES = set(df["板块名称"].tolist())
        return _KNOWN_INDUSTRY_NAMES

    # 内置常用东财行业名（保底）
    _KNOWN_INDUSTRY_NAMES = {
        "煤炭", "石油石化", "电力", "燃气", "水务",
        "钢铁", "工业金属", "小金属", "金属新材料", "能源金属",
        "化学原料", "化学制品", "农化制品", "电子化学品",
        "造纸印刷", "橡胶", "塑料",
        "通用设备", "专用设备", "工程机械", "仪器仪表",
        "轨道交通装备", "航空航天装备", "军工装备", "军工电子", "船舶制造",
        "电网设备", "电机", "其他电源设备", "风电设备", "光伏设备",
        "汽车整车", "汽车零部件", "摩托车",
        "家用电器", "照明设备",
        "白酒", "啤酒", "饮料乳品", "食品加工制造",
        "种植业与林业", "养殖业", "水产",
        "化学制药", "生物制药", "中药", "医疗器械", "医疗服务", "医药商业",
        "银行", "证券", "保险", "多元金融",
        "房地产开发", "房地产服务", "建筑装饰", "建筑材料",
        "半导体", "消费电子", "元件", "印制电路板",
        "光学光电子", "其他电子", "IT设备", "计算机设备", "软件开发",
        "互联网服务", "通信设备", "通信服务",
        "环保设备", "环保服务",
        "旅游景区", "酒店餐饮", "航空运输", "航运港口",
        "教育", "游戏", "影视院线", "广告营销",
    }
    return _KNOWN_INDUSTRY_NAMES


# ─────────────────────────────────────────────────────────
# 概念板块排行
# ─────────────────────────────────────────────────────────

@dataclass
class ConceptRanking:
    """概念板块排名"""
    name: str
    code: str = ""                 # 板块代码 BK0xxx
    gain_pct: float = 0.0
    leading_stock: str = ""
    leading_gain: float = 0.0
    sector_type: str = "中性"      # 自动分类：防守/进攻/周期/中性
    tag: str = ""                  # 来源标记：""=实时, "static"=硬编码兜底
    # 丰富字段（东财概念板块数据）
    up_count: int = 0              # 上涨家数
    down_count: int = 0            # 下跌家数
    turnover_rate: float = 0.0     # 换手率
    composite_score: float = 0.0   # 综合评分（多维度加权）
    related_industries: List[str] = field(default_factory=list)  # 关联行业名


def _classify_concept(name: str) -> str:
    """根据概念名中的关键词自动分类。"""
    import config as _cfg
    keywords = getattr(_cfg, "CONCEPT_TYPE_KEYWORDS", {})
    for cat, kws in keywords.items():
        for kw in kws:
            if kw in name:
                return cat
    return "中性"


def _classify_industry(name: str) -> str:
    """根据 SECTOR_TYPE_MAP 分类行业。"""
    import config as _cfg
    return getattr(_cfg, "SECTOR_TYPE_MAP", {}).get(name, "中性")


def _is_noise_concept(name: str) -> bool:
    """判断是否为噪音概念（非主题性的统计/回溯类板块）。"""
    import config as _cfg
    patterns = getattr(_cfg, "CONCEPT_NOISE_PATTERNS", [])
    for p in patterns:
        if p in name:
            return True
    return False


def _compute_concept_score(gain_pct: float, up_count: int, down_count: int,
                           turnover_rate: float, leading_gain: float,
                           sector_type: str) -> float:
    """
    概念板块综合评分 (0-100)。
    维度: 涨跌幅30% + 上涨比例25% + 换手率20% + 领涨股涨幅15% + 属性10%
    """
    # 涨跌幅 (30%): [-10, +10] → [0, 100]
    gain_norm = min(max((gain_pct + 10) / 20 * 100, 0), 100)

    # 上涨比例 (25%): up/(up+down) → [0, 100]
    total = up_count + down_count
    up_ratio = (up_count / total * 100) if total > 0 else 50

    # 换手率 (20%): [0, 5%] → [0, 100]
    turn_norm = min(turnover_rate / 5 * 100, 100)

    # 领涨股涨幅 (15%): [0, 20%] → [0, 100]
    lead_norm = min(max(leading_gain / 20 * 100, 0), 100)

    # 属性加分 (10%): 进攻/周期 偏高
    type_score = {"进攻": 80, "周期": 60, "防守": 40, "中性": 50}.get(sector_type, 50)

    score = (gain_norm * 0.30 + up_ratio * 0.25 + turn_norm * 0.20
             + lead_norm * 0.15 + type_score * 0.10)
    return round(min(score, 100), 1)


_CONCEPT_INDUSTRY_HINTS = {
    "储能": ["电力", "光伏设备", "电网设备"],
    "电池": ["能源金属", "光伏设备"],
    "锂电": ["能源金属", "化学原料"],
    "锂矿": ["能源金属", "小金属"],
    "光伏": ["光伏设备", "电网设备"],
    "风电": ["风电设备", "电网设备"],
    "芯片": ["半导体", "消费电子"],
    "半导体": ["半导体", "元件"],
    "AI": ["计算机设备", "软件开发"],
    "算力": ["计算机设备", "通信设备"],
    "机器人": ["通用设备", "专用设备"],
    "汽车": ["汽车整车", "汽车零部件"],
    "军工": ["军工装备", "航空航天装备"],
    "医药": ["化学制药", "生物制药"],
    "白酒": ["白酒"],
    "化工": ["化学原料", "化学制品"],
    "钢铁": ["钢铁"],
    "银行": ["银行"],
    "券商": ["证券"],
    "地产": ["房地产开发"],
    "稀土": ["小金属", "工业金属"],
}


def _map_concept_to_industries(concept_name: str) -> List[str]:
    """概念名关键词匹配行业名。优先用提示表，再用 ROTATION_LINE_MAP。"""
    import config as _cfg

    # 先用提示表精确匹配
    for keyword, industries in _CONCEPT_INDUSTRY_HINTS.items():
        if keyword in concept_name:
            return industries[:3]

    # 回退到 ROTATION_LINE_MAP 子串匹配
    rot_map = getattr(_cfg, "ROTATION_LINE_MAP", {})
    matched = []
    kw = concept_name.replace("概念", "").replace("板块", "").strip()
    for ind_name in sorted(rot_map.keys(), key=len, reverse=True):
        if ind_name in concept_name or (kw and len(kw) >= 2 and kw in ind_name):
            matched.append(ind_name)
        if len(matched) >= 3:
            break
    return matched



def _concept_cache_path():
    """概念排行磁盘缓存路径"""
    from pathlib import Path
    cache_dir = Path(__file__).resolve().parent.parent.parent / ".data" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir / "concept_rankings.json"


def _save_concept_cache(results: List[ConceptRanking], source: str = "em"):
    """保存概念排行到磁盘缓存（标记来源 source: em/ths）"""
    import json, time
    data = {
        "ts": time.time(),
        "source": source,
        "items": [
            {"name": c.name, "code": c.code, "gain_pct": c.gain_pct,
             "leading_stock": c.leading_stock, "leading_gain": c.leading_gain,
             "sector_type": c.sector_type, "tag": c.tag,
             "up_count": c.up_count, "down_count": c.down_count,
             "turnover_rate": c.turnover_rate, "composite_score": c.composite_score,
             "related_industries": c.related_industries}
            for c in results
        ],
    }
    try:
        _concept_cache_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


def _load_concept_cache(max_age: float = 86400) -> List[ConceptRanking]:
    """从磁盘缓存加载概念排行（默认24h过期）。校验数据有效性。"""
    import json, time
    path = _concept_cache_path()
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if time.time() - data.get("ts", 0) > max_age:
            return []
        items = data.get("items", [])
        if not items:
            return []
        # 缓存有效性校验：如果 gain_pct 全为 0 且来源是 THS，视为弱数据
        source = data.get("source", "unknown")
        gains = [abs(item.get("gain_pct", 0)) for item in items]
        if source == "ths" and all(g < 0.01 for g in gains):
            _detail("  [缓存] THS 缓存数据 gain_pct 全为 0，视为无效")
            return []
        results = []
        for item in items:
            results.append(ConceptRanking(
                name=item["name"],
                code=item.get("code", ""),
                gain_pct=item.get("gain_pct", 0),
                leading_stock=item.get("leading_stock", ""),
                leading_gain=item.get("leading_gain", 0),
                sector_type=item.get("sector_type", ""),
                tag=item.get("tag", ""),
                up_count=item.get("up_count", 0),
                down_count=item.get("down_count", 0),
                turnover_rate=item.get("turnover_rate", 0),
                composite_score=item.get("composite_score", 0),
                related_industries=item.get("related_industries", []),
            ))
        _detail(f"  [缓存] 概念板块 {len(results)} 条（磁盘缓存, 来源:{source}）")
        return results
    except Exception:
        return []


# ── 概念板块硬编码兜底 ───────────────────────────────────
_FALLBACK_CONCEPTS = [
    ("AI算力", "进攻"), ("DeepSeek概念", "进攻"), ("机器人概念", "进攻"),
    ("半导体", "进攻"), ("消费电子", "进攻"), ("新能源车", "周期"),
    ("光伏", "周期"), ("军工", "主题"), ("中药", "防守"), ("白酒", "消费"),
]


def _fallback_concepts(top_n: int = 10) -> List[ConceptRanking]:
    """当东财+缓存都失败时的硬编码兜底概念列表"""
    _detail("  [兜底] 使用静态概念列表")
    results = []
    for name, stype in _FALLBACK_CONCEPTS[:top_n]:
        results.append(ConceptRanking(
            name=name,
            gain_pct=0.0,
            leading_stock="",
            leading_gain=0.0,
            sector_type=stype,
            tag="static",
        ))
    return results


def _get_concepts_ths(top_n: int = 10) -> List[ConceptRanking]:
    """
    同花顺概念板块降级源。
    获取概念名称列表 + 并行获取 K 线计算涨跌幅。
    """
    import akshare as ak
    from datetime import datetime, timedelta
    from concurrent.futures import ThreadPoolExecutor

    _detail("  [THS] 尝试同花顺概念板块...")
    df = ak.stock_board_concept_name_ths()
    if df is None or df.empty:
        raise ValueError("THS 概念列表为空")

    # 名称列: 尝试多种可能的列名
    name_col = None
    for col in ["概念名称", "name", "板块名称"]:
        if col in df.columns:
            name_col = col
            break
    if name_col is None and len(df.columns) > 0:
        name_col = df.columns[0]

    # THS 概念列表按拼音排序，不能只取前 N 个（会偏向 A/B 开头）
    # 策略：取全量名称 → 优先匹配已知热门关键词 → 补齐随机样本
    all_names = df[name_col].tolist()
    _HOT_KEYWORDS = ["AI", "算力", "机器人", "芯片", "半导体", "新能源", "光伏",
                     "储能", "电力", "军工", "低空", "白酒", "医药", "消费",
                     "汽车", "锂电", "光模块", "ChatGPT", "DeepSeek", "鸿蒙",
                     "华为", "特斯拉", "比亚迪", "CXO", "CPO"]
    # 优先选包含热门关键词的概念
    hot_names = [n for n in all_names if any(kw in n for kw in _HOT_KEYWORDS)]
    # 补齐：从剩余概念中均匀采样
    remaining = [n for n in all_names if n not in hot_names]
    import random
    sample_size = max(0, top_n * 3 - len(hot_names))
    if sample_size > 0 and remaining:
        step = max(1, len(remaining) // sample_size)
        sampled = remaining[::step][:sample_size]
    else:
        sampled = []
    concept_names = (hot_names + sampled)[:top_n * 2]  # 从3x降到2x，减少HTTP请求

    today = datetime.now()
    start_date = (today - timedelta(days=7)).strftime("%Y%m%d")
    end_date = today.strftime("%Y%m%d")

    def _fetch_gain(cname):
        try:
            kdf = ak.stock_board_concept_index_ths(
                symbol=cname, start_date=start_date, end_date=end_date)
            if kdf is not None and len(kdf) >= 2:
                close_col = None
                for c in ["收盘价", "收盘", "close"]:
                    if c in kdf.columns:
                        close_col = c
                        break
                if close_col:
                    gain = (float(kdf.iloc[-1][close_col]) /
                            float(kdf.iloc[-2][close_col]) - 1) * 100
                    return cname, round(gain, 2)
            return cname, 0.0
        except Exception:
            return cname, 0.0

    # 并行获取涨跌幅（8 workers 加速，6s 超时）
    results_raw = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(_fetch_gain, n): n for n in concept_names}
        for fut in futures:
            try:
                name, gain = fut.result(timeout=6)  # 从10s降到6s
                results_raw.append((name, gain))
            except Exception:
                results_raw.append((futures[fut], 0.0))

    # 按涨幅排序取 top_n
    results_raw.sort(key=lambda x: -x[1])
    results = []
    for name, gain in results_raw[:top_n]:
        results.append(ConceptRanking(
            name=name,
            gain_pct=gain,
            leading_stock="",
            leading_gain=0.0,
            sector_type=_classify_concept(name),
            tag="ths",
        ))

    _detail(f"  [✓] THS 概念板块 top {len(results)} 条")
    return results


def _get_concepts_sina(top_n: int = 10) -> List[ConceptRanking]:
    """
    新浪概念板块排行（2.4s, 175 概念）。
    stock_sector_spot("概念") 列: label, 板块, 公司家数, 平均价格, 涨跌额,
                                 涨跌幅, 总成交量, 总成交额, 领涨股票, 涨跌幅.1
    """
    import akshare as ak
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    def _call():
        with _no_proxy():
            return ak.stock_sector_spot(indicator="概念")

    _pool = ThreadPoolExecutor(max_workers=1)
    _future = _pool.submit(_call)
    try:
        df = _future.result(timeout=8)
    except (FutureTimeout, Exception) as e:
        _detail(f"  [!] 新浪概念板块失败（{e.__class__.__name__}）")
        return []
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)

    if df is None or df.empty or "涨跌幅" not in df.columns:
        return []

    # 全量处理：先过滤噪音，再评分排序
    results = []
    for _, row in df.iterrows():
        name = str(row.get("板块", "")).strip()
        if not name or _is_noise_concept(name):
            continue
        leading = str(row.get("领涨股票", ""))
        leading_gain = 0.0
        try:
            leading_gain = float(row.get("涨跌幅.1", 0) or 0)
        except (ValueError, TypeError):
            pass
        gain_pct = float(row.get("涨跌幅", 0) or 0)
        stype = _classify_concept(name)
        # 新浪无上涨家数/换手率，用简化评分
        score = _compute_concept_score(gain_pct, 0, 0, 0, leading_gain, stype)
        related = _map_concept_to_industries(name)
        results.append(ConceptRanking(
            name=name,
            gain_pct=gain_pct,
            leading_stock=leading,
            leading_gain=leading_gain,
            sector_type=stype,
            composite_score=score,
            related_industries=related,
        ))
    results.sort(key=lambda x: x.composite_score, reverse=True)
    results = results[:top_n]
    _detail(f"  [✓] 新浪概念板块 top {len(results)} 条（已过滤噪音）")
    return results


def get_concept_rankings(top_n: int = None) -> List[ConceptRanking]:
    """
    获取概念板块综合排行 top N（过滤噪音 + 多维评分）。
    降级链: 东财(8s,468概念,字段丰富) → 新浪(2.4s,175概念) → THS(25s) → 磁盘缓存 → 硬编码兜底
    """
    import akshare as ak
    import config as _cfg
    from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout

    if top_n is None:
        top_n = getattr(_cfg, "CONCEPT_TOP_N", 15)

    # ── 0. 东财优先（468概念, 上涨/下跌/换手率等丰富字段）──
    # 先尝试 social_fetcher 的内存/磁盘缓存（毫秒级），再尝试直接网络调用
    df = None
    try:
        from signals.data.social_fetcher import fetch_concept_list
        df = fetch_concept_list()
        if df is not None and not df.empty and "涨跌幅" in df.columns:
            _detail(f"  [✓] 东财概念板块（缓存） {len(df)} 条")
        else:
            df = None
    except Exception as e:
        _detail(f"  [!] 概念板块缓存读取失败: {e}")
        df = None

    if df is None:
        # 缓存未命中，尝试直接网络调用（带 SSL 重试，不受行业板块熔断控制）
        try:
            df = _em_retry(ak.stock_board_concept_name_em, retries=3, delay=1.0)
        except Exception as e:
            _detail(f"  [!] 东财概念板块接口失败（3次重试后，{e.__class__.__name__}: {e}）")
            df = None

    if df is not None and not df.empty and "涨跌幅" in df.columns:
        # 全量处理：先过滤噪音，再评分排序，最后截取 top_n
        results = []
        for _, row in df.iterrows():
            name = str(row.get("板块名称", "")).strip()
            if not name or _is_noise_concept(name):
                continue
            leading = str(row.get("领涨股票", ""))
            leading_gain = 0.0
            try:
                leading_gain = float(row.get("领涨股票-涨跌幅", 0) or 0)
            except (ValueError, TypeError):
                pass
            gain_pct = float(row.get("涨跌幅", 0) or 0)
            up_c = int(row.get("上涨家数", 0) or 0)
            down_c = int(row.get("下跌家数", 0) or 0)
            turn = float(row.get("换手率", 0) or 0)
            code = str(row.get("板块代码", ""))
            stype = _classify_concept(name)
            score = _compute_concept_score(
                gain_pct, up_c, down_c, turn, leading_gain, stype)
            related = _map_concept_to_industries(name)
            results.append(ConceptRanking(
                name=name, code=code,
                gain_pct=gain_pct,
                leading_stock=leading,
                leading_gain=leading_gain,
                sector_type=stype,
                up_count=up_c,
                down_count=down_c,
                turnover_rate=turn,
                composite_score=score,
                related_industries=related,
            ))
        # 按综合评分排序
        results.sort(key=lambda x: x.composite_score, reverse=True)
        results = results[:top_n]
        _detail(f"  [✓] 概念板块 top {len(results)} 条（已过滤噪音+综合评分排序）")
        _save_concept_cache(results, source="em")
        return results

    # ── 1. 新浪降级（2.4s, 175概念, 无上涨/下跌/换手率）──
    sina_results = _get_concepts_sina(top_n)
    if sina_results:
        _save_concept_cache(sina_results, source="sina")
        return sina_results

    # ── 2. 同花顺降级（全局超时 25s，防止 THS K线拉取太慢）──
    try:
        _ths_pool = ThreadPoolExecutor(max_workers=1)
        _ths_future = _ths_pool.submit(_get_concepts_ths, top_n)
        try:
            ths_results = _ths_future.result(timeout=25)
        finally:
            _ths_pool.shutdown(wait=False, cancel_futures=True)
        if ths_results:
            _save_concept_cache(ths_results, source="ths")
            return ths_results
    except (FutureTimeout, TimeoutError):
        _detail("  [!] THS 概念板块超时（>25s），跳过")
    except Exception as e:
        _detail(f"  [!] THS 概念板块也失败（{e.__class__.__name__}）")

    # ── 3. 磁盘缓存 ──
    cached = _load_concept_cache()
    if cached:
        return cached[:top_n]

    # ── 4. 硬编码兜底 ──
    return _fallback_concepts(top_n)


def _match_concepts_to_industries(
    concepts: List[ConceptRanking],
    industry_names: List[str],
    name_df: "pd.DataFrame | None",
) -> dict:
    """
    轻量关联：概念的领涨股如果同名出现在行业领涨股中，就把概念挂到该行业。
    返回 {行业名: [概念名, ...]}
    """
    if not concepts or name_df is None or name_df.empty:
        return {}

    # 构建行业→领涨股映射
    ind_leader_map: dict = {}  # {领涨股名: 行业名}
    for _, row in name_df.iterrows():
        ind = str(row.get("板块名称", "")).strip()
        leader = str(row.get("领涨股票", "")).strip()
        if ind and leader and ind in industry_names:
            ind_leader_map[leader] = ind

    result: dict = {}
    for c in concepts:
        if c.leading_stock and c.leading_stock in ind_leader_map:
            ind = ind_leader_map[c.leading_stock]
            result.setdefault(ind, []).append(c.name)

    return result


# ─────────────────────────────────────────────────────────
# 数据加载函数（涨停池、强势股、融资等）
# ─────────────────────────────────────────────────────────

def _load_zt_pool(date_str: str) -> dict:
    """
    加载涨停池，按行业归组。
    :param date_str: YYYYMMDD
    :return: {行业名: [(6位代码, 名称, 连板数, 首封时间), ...]}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if not ind:
                continue
            code = str(row.get("代码", "")).strip().zfill(6)
            name = str(row.get("名称", ""))
            lianban = 1
            try:
                lianban = int(row.get("连板数", 1) or 1)
            except (ValueError, TypeError):
                pass
            first_time = str(row.get("首次封板时间", ""))
            result.setdefault(ind, []).append((code, name, lianban, first_time))
        _detail(f"  [✓] 涨停池 {sum(len(v) for v in result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 涨停池加载失败（{e.__class__.__name__}）")
    return result


def _load_strong_pool(date_str: str) -> dict:
    """
    加载强势股池，按行业归组。
    :param date_str: YYYYMMDD
    :return: {行业名: [(6位代码, 名称, 量比, 入选理由), ...]}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_strong_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if not ind:
                continue
            code = str(row.get("代码", "")).strip().zfill(6)
            name = str(row.get("名称", ""))
            liangbi = 0.0
            try:
                liangbi = float(row.get("量比", 0) or 0)
            except (ValueError, TypeError):
                pass
            reason = str(row.get("入选理由", ""))
            result.setdefault(ind, []).append((code, name, liangbi, reason))
        _detail(f"  [✓] 强势股池 {sum(len(v) for v in result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 强势股池加载失败（{e.__class__.__name__}）")
    return result


def _load_margin_map(date_str: str) -> dict:
    """
    加载融资融券数据（上交所），返回 {6位代码: 融资买入额}。
    并行尝试当日+前3天（覆盖周末），取最先成功的。
    """
    import akshare as ak
    from datetime import datetime, timedelta
    from concurrent.futures import ThreadPoolExecutor, as_completed

    base = datetime.strptime(date_str, "%Y%m%d")
    dates = [(base - timedelta(days=delta)).strftime("%Y%m%d") for delta in range(4)]

    def _try_date(d):
        try:
            df = ak.stock_margin_detail_sse(date=d)
            if df is not None and not df.empty:
                code_col = None
                for c in ["股票代码", "标的证券代码", "代码"]:
                    if c in df.columns:
                        code_col = c
                        break
                buy_col = None
                for c in ["融资买入额", "融资买入额(元)", "融资买入"]:
                    if c in df.columns:
                        buy_col = c
                        break
                if code_col and buy_col:
                    result = {}
                    for _, row in df.iterrows():
                        code = str(row[code_col]).strip().zfill(6)
                        try:
                            amt = float(row[buy_col] or 0)
                        except (ValueError, TypeError):
                            amt = 0.0
                        if amt > 0:
                            result[code] = amt
                    return d, result
        except Exception:
            pass
        return d, None

    # 并行尝试 4 个日期，取第一个成功的
    with ThreadPoolExecutor(max_workers=4) as pool:
        for f in as_completed([pool.submit(_try_date, d) for d in dates]):
            d, result = f.result()
            if result:
                _detail(f"  [✓] 融资数据（{d}）{len(result)} 只")
                return result

    _detail("  [!] 融资数据加载失败（最近4天均无数据）")
    return {}


def _load_dt_pool(date_str: str) -> dict:
    """
    加载跌停股池，按行业计数。
    :return: {行业名: 跌停家数}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_dtgc_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if ind:
                result[ind] = result.get(ind, 0) + 1
        _detail(f"  [✓] 跌停池 {sum(result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 跌停池加载失败（{e.__class__.__name__}）")
    return result


def _load_zbgc_pool(date_str: str) -> dict:
    """
    加载昨日涨停今日表现（续板）池，按行业计数。
    :return: {行业名: 续板家数}
    """
    import akshare as ak
    result: dict = {}
    try:
        df = ak.stock_zt_pool_zbgc_em(date=date_str)
        if df is None or df.empty:
            return result
        for _, row in df.iterrows():
            ind = str(row.get("所属行业", "")).strip()
            if ind:
                result[ind] = result.get(ind, 0) + 1
        _detail(f"  [✓] 昨涨停跟踪 {sum(result.values())} 只，"
               f"覆盖 {len(result)} 个行业")
    except Exception as e:
        _detail(f"  [!] 昨涨停跟踪加载失败（{e.__class__.__name__}）")
    return result


# ─────────────────────────────────────────────────────────
# P3-3: 板块节奏检测（轻量补充）
# ─────────────────────────────────────────────────────────

def _enrich_rhythm(rankings: List["IndustryRanking"]):
    """为 Top-N 行业补充节奏检测（跳过已有 rhythm 的）"""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    # get_industry_bars 自带多源降级（东财→THS→pytdx→缓存），无需东财熔断守卫
    need_rhythm = [r for r in rankings if not r.rhythm_phase]
    if not need_rhythm:
        return

    def _compute(r):
        try:
            bars = get_industry_bars(r.name, lookback_days=30)
            if not bars:
                return
            from signals.core.sector_rhythm import compute_sector_rhythm
            rhythm = compute_sector_rhythm(r.name, bars)
            if rhythm:
                r.rhythm_phase = rhythm.phase
                r.rhythm_score = rhythm.rhythm_score
                r.rhythm_hint = rhythm.action_hint
        except Exception:
            pass

    with _no_proxy(), ThreadPoolExecutor(max_workers=4) as pool:
        futures = [pool.submit(_compute, r) for r in need_rhythm[:10]]
        for f in as_completed(futures):
            f.result()  # propagate exceptions silently


# 板块超跌检测
# ─────────────────────────────────────────────────────────

def compute_oversold_score(bars) -> tuple:
    """
    从行业板块日线K线计算超跌评分（满分100）。

    四维度加权：
    - 距20日高点回撤 (40分): >15%满分, 5~15%线性
    - 均线偏离度 (30分): 低于20日均线>10%满分, 3~10%线性
    - 连续下跌天数 (15分): 5天满分, 3天10分, 1~2天线性
    - RSI超卖 (15分): RSI14<30满分, 30~40线性

    :param bars: List[RawBar] 最近30日板块日线
    :return: (score, detail_str) — score=0表示不超跌
    """
    if not bars or len(bars) < 5:
        return 0.0, ""

    closes = [b.close for b in bars]
    latest = closes[-1]

    # 1. 距20日高点回撤 (40分)
    high_20 = max(closes[-20:]) if len(closes) >= 20 else max(closes)
    drawdown = (high_20 - latest) / high_20 * 100 if high_20 > 0 else 0
    if drawdown >= 15:
        dd_score = 40
    elif drawdown >= 5:
        dd_score = (drawdown - 5) / 10 * 40
    else:
        dd_score = 0

    # 2. 均线偏离度 (30分) — 低于20日均线的百分比
    ma20 = sum(closes[-20:]) / min(len(closes), 20) if closes else 0
    ma_dev = (ma20 - latest) / ma20 * 100 if ma20 > 0 else 0
    if ma_dev >= 10:
        ma_score = 30
    elif ma_dev >= 3:
        ma_score = (ma_dev - 3) / 7 * 30
    else:
        ma_score = 0

    # 3. 连续下跌天数 (15分)
    consec_down = 0
    for i in range(len(closes) - 1, 0, -1):
        if closes[i] < closes[i - 1]:
            consec_down += 1
        else:
            break
    if consec_down >= 5:
        cd_score = 15
    elif consec_down >= 3:
        cd_score = 10
    elif consec_down >= 1:
        cd_score = consec_down * 3
    else:
        cd_score = 0

    # 4. RSI14 超卖 (15分)
    rsi_score = 0
    if len(closes) >= 15:
        gains, losses = [], []
        for i in range(1, len(closes)):
            diff = closes[i] - closes[i - 1]
            gains.append(max(diff, 0))
            losses.append(max(-diff, 0))
        # 使用最近14日
        avg_gain = sum(gains[-14:]) / 14
        avg_loss = sum(losses[-14:]) / 14
        if avg_loss > 0:
            rs = avg_gain / avg_loss
            rsi = 100 - (100 / (1 + rs))
            if rsi < 30:
                rsi_score = 15
            elif rsi < 40:
                rsi_score = (40 - rsi) / 10 * 15

    total = round(dd_score + ma_score + cd_score + rsi_score, 1)
    detail = f"距高点-{drawdown:.1f}%"
    if consec_down >= 3:
        detail += f", 连跌{consec_down}日"

    return total, detail


def get_oversold_industries(name_df=None, top_n: int = 5) -> List[IndustryRanking]:
    """
    检测超跌行业（取当前跌幅最大的板块，加载K线计算超跌评分）。

    策略：从 name_df 中取涨幅最低的 15 个行业，并行加载30日K线，
    计算超跌评分，返回评分 >= 40 的 top N。

    :param name_df: stock_board_industry_name_em() 结果
    :param top_n: 返回前N个超跌行业
    :return: List[IndustryRanking]（带 oversold_score/oversold_detail）
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    # 东财+同花顺都熔断时跳过超跌检测（K线完全不可用）
    if _EM_CIRCUIT_OPEN and _THS_CIRCUIT_OPEN:
        _detail("  [⚡] 东财+同花顺均熔断，跳过超跌检测")
        return []

    if name_df is None or name_df.empty or "涨跌幅" not in name_df.columns:
        return []

    # 取跌幅最大的 15 个行业作为候选
    bottom = name_df.sort_values("涨跌幅", ascending=True).head(15)
    candidates = []
    for _, row in bottom.iterrows():
        ind = str(row.get("板块名称", "")).strip()
        gain = float(row.get("涨跌幅", 0) or 0)
        if ind:
            candidates.append((ind, gain))

    if not candidates:
        return []

    # 并行加载 30 日 K线
    def _compute_one(ind_name, gain_pct):
        try:
            bars = get_industry_bars(ind_name, lookback_days=30)
            score, detail = compute_oversold_score(bars)
            from signals.core.rotation import get_rotation_line
            # P3-3: 板块节奏检测
            r_phase, r_score, r_hint = "", 0.0, ""
            try:
                from signals.core.sector_rhythm import compute_sector_rhythm
                rhythm = compute_sector_rhythm(ind_name, bars)
                if rhythm:
                    r_phase, r_score, r_hint = rhythm.phase, rhythm.rhythm_score, rhythm.action_hint
            except Exception:
                pass
            return IndustryRanking(
                name=ind_name,
                gain_pct=gain_pct,
                sector_type=_classify_industry(ind_name),
                oversold_score=score,
                oversold_detail=detail,
                rotation_line=get_rotation_line(ind_name),
                rhythm_phase=r_phase,
                rhythm_score=r_score,
                rhythm_hint=r_hint,
            )
        except Exception:
            return None

    results = []
    with _no_proxy(), ThreadPoolExecutor(max_workers=6) as pool:
        futures = {pool.submit(_compute_one, n, g): n for n, g in candidates}
        for f in as_completed(futures):
            r = f.result()
            if r and r.oversold_score >= 40:
                results.append(r)

    results.sort(key=lambda x: -x.oversold_score)
    if results:
        _detail(f"  [✓] 超跌检测: {len(results)} 个行业评分>=40")
    return results[:top_n]


# ─────────────────────────────────────────────────────────
# 行业综合强度评分
# ─────────────────────────────────────────────────────────

def compute_industry_composite_scores(
    change_df: pd.DataFrame,
    zt_pool: dict,
    strong_pool: dict,
    dt_count: dict,
    zbgc_count: dict,
    weights: dict = None,
) -> List[dict]:
    """
    计算所有行业的综合强度评分（9维度加权，含2个领先指标）。

    维度说明:
      滞后型: gain, inflow, zt_density, lianban, strong_density, continue, dt_penalty
      领先型: inflow_momentum (资金先行度), startup_ratio (启动率)

    :param change_df:   stock_board_change_em() 结果
    :param zt_pool:     _load_zt_pool() 结果
    :param strong_pool: _load_strong_pool() 结果
    :param dt_count:    _load_dt_pool() 结果
    :param zbgc_count:  _load_zbgc_pool() 结果
    :param weights:     评分权重 dict
    :return: 按综合分降序排列的 list[dict]
    """
    import config as _cfg

    if weights is None:
        weights = getattr(_cfg, "RANK_COMPOSITE_WEIGHTS", {
            "gain": 15, "inflow": 15, "zt_density": 15,
            "lianban": 5, "strong_density": 10,
            "continue": 5, "dt_penalty": 10,
            "inflow_momentum": 15, "startup_ratio": 10,
        })

    if change_df is None or change_df.empty:
        return []

    # 过滤只保留行业板块
    industry_names = _get_known_industry_names()
    df = (change_df[change_df["板块名称"].isin(industry_names)].copy()
          if industry_names else change_df.copy())

    # 提取每行业的原始维度
    rows = []
    for _, r in df.iterrows():
        name = str(r["板块名称"])
        gain = float(r.get("涨跌幅", 0) or 0)
        inflow_raw = float(r.get("主力净流入", 0) or 0)
        inflow = inflow_raw / 1e8  # 转为亿
        zt_list = zt_pool.get(name, [])
        zt_cnt = len(zt_list)
        max_lianban = max((x[2] for x in zt_list), default=0)
        strong_cnt = len(strong_pool.get(name, []))
        zbgc = zbgc_count.get(name, 0)
        dt = dt_count.get(name, 0)
        # 领先指标 1: 资金动量 = 资金流入 / 涨幅绝对值
        # 资金大量流入但涨幅小 → 资金先行，值越大越领先
        inflow_momentum = inflow / max(abs(gain), 0.1)
        # 领先指标 2: 启动率 = (涨停+强势) 占比的代理指标
        # 涨停+强势数量多但涨幅低 → 个股开始启动但板块还没起来
        startup_ratio = (zt_cnt + strong_cnt)
        rows.append({
            "name": name, "gain": gain, "inflow": inflow,
            "zt_count": zt_cnt, "max_lianban": max_lianban,
            "strong_count": strong_cnt, "zbgc": zbgc, "dt": dt,
            "inflow_momentum": inflow_momentum,
            "startup_ratio": startup_ratio,
        })

    if not rows:
        return []

    # min-max 归一化
    def _norm(vals):
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(vals)
        return [(v - mn) / (mx - mn) for v in vals]

    gains      = _norm([r["gain"] for r in rows])
    inflows    = _norm([r["inflow"] for r in rows])
    zt_dens    = _norm([r["zt_count"] for r in rows])
    lianbans   = _norm([r["max_lianban"] for r in rows])
    strongs    = _norm([r["strong_count"] for r in rows])
    continues  = _norm([r["zbgc"] for r in rows])
    dt_pens    = _norm([r["dt"] for r in rows])
    inflow_moms = _norm([r["inflow_momentum"] for r in rows])
    startup_rs  = _norm([r["startup_ratio"] for r in rows])

    results = []
    for i, r in enumerate(rows):
        score = (
            gains[i]       * weights.get("gain", 15)
            + inflows[i]   * weights.get("inflow", 15)
            + zt_dens[i]   * weights.get("zt_density", 15)
            + lianbans[i]  * weights.get("lianban", 5)
            + strongs[i]   * weights.get("strong_density", 10)
            + continues[i] * weights.get("continue", 5)
            - dt_pens[i]   * weights.get("dt_penalty", 10)
            + inflow_moms[i] * weights.get("inflow_momentum", 15)
            + startup_rs[i]  * weights.get("startup_ratio", 10)
        )
        results.append({**r, "composite_score": round(score, 1)})

    results.sort(key=lambda x: -x["composite_score"])
    return results


def compute_historical_composite_scores(
    zt_pool: dict,
    strong_pool: dict,
    dt_count: dict,
    zbgc_count: dict,
    weights: dict = None,
) -> List[dict]:
    """
    盘后模式：仅用 pool 数据的 5 维综合评分（无 change_em）。
    以涨停池覆盖的行业为候选集。

    :return: 按综合分降序排列的 list[dict]
    """
    import config as _cfg

    if weights is None:
        weights = getattr(_cfg, "RANK_HISTORICAL_WEIGHTS", {
            "zt_density": 30, "lianban": 20, "strong_density": 25,
            "continue": 15, "dt_penalty": 10,
        })

    # 合并所有 pool 中出现过的行业名
    all_industries: set = set()
    all_industries.update(zt_pool.keys())
    all_industries.update(strong_pool.keys())
    all_industries.update(zbgc_count.keys())
    all_industries.update(dt_count.keys())

    if not all_industries:
        return []

    rows = []
    for name in sorted(all_industries):
        zt_list = zt_pool.get(name, [])
        zt_cnt = len(zt_list)
        max_lianban = max((x[2] for x in zt_list), default=0)
        strong_cnt = len(strong_pool.get(name, []))
        zbgc = zbgc_count.get(name, 0)
        dt = dt_count.get(name, 0)
        rows.append({
            "name": name, "gain": 0.0, "inflow": 0.0,
            "zt_count": zt_cnt, "max_lianban": max_lianban,
            "strong_count": strong_cnt, "zbgc": zbgc, "dt": dt,
        })

    if not rows:
        return []

    # min-max 归一化
    def _norm(vals):
        mn, mx = min(vals), max(vals)
        if mx == mn:
            return [0.5] * len(vals)
        return [(v - mn) / (mx - mn) for v in vals]

    zt_dens   = _norm([r["zt_count"] for r in rows])
    lianbans  = _norm([r["max_lianban"] for r in rows])
    strongs   = _norm([r["strong_count"] for r in rows])
    continues = _norm([r["zbgc"] for r in rows])
    dt_pens   = _norm([r["dt"] for r in rows])

    results = []
    for i, r in enumerate(rows):
        score = (
            zt_dens[i]   * weights.get("zt_density", 30)
            + lianbans[i] * weights.get("lianban", 20)
            + strongs[i]  * weights.get("strong_density", 25)
            + continues[i] * weights.get("continue", 15)
            - dt_pens[i]  * weights.get("dt_penalty", 10)
        )
        results.append({**r, "composite_score": round(score, 1)})

    results.sort(key=lambda x: -x["composite_score"])
    return results


# ─────────────────────────────────────────────────────────
# 双榜行业筛选 + 多维度个股入池（主函数）
# ─────────────────────────────────────────────────────────

def get_industry_representatives(top_n: int = None,
                                  date_str: str = None) -> Tuple[
        List[IndustryRanking], List[IndustryRanking],
        List[IndustryRanking], List[ConceptRanking],
        List[IndustryRanking]]:
    """
    双榜合并 + 每行业选出代表股 + 概念板块排行 + 超跌检测。

    流程：
    1. 加载全部数据源（change_em, name_em, 涨停池, 强势池, 融资, 跌停, 续板, 概念板块）
    2. 涨幅榜 top N（盘后降级为涨停密度排行）
    3. 综合强度榜 top N（盘后用5维评分）
    4. 对并集中每个行业，从 6 维度选出代表股
    5. 为每个行业设置 sector_type 和 concept_tags + 概念板块排行
    6. 超跌检测（盘中模式）

    :param top_n:    每个榜取前N名行业
    :param date_str: YYYYMMDD格式日期，None=今天（盘中模式）；
                     传入历史日期则进入盘后模式（跳过 change_em/name_em）
    :return: (gain_list, composite_list, merged_list, concepts, oversold_list)
             gain_list      — 涨幅榜/涨停密度榜 top N（带代表股+标签）
             composite_list — 综合榜 top N（带代表股+标签）
             merged_list    — 并集（去重，带代表股+标签）
             concepts       — 概念板块涨幅排行（带分类标签）
             oversold_list  — 超跌行业（评分>=40，盘后模式为空）
    """
    import akshare as ak
    import config as _cfg
    from datetime import datetime

    if top_n is None:
        top_n = getattr(_cfg, "RANK_TOP_N", 10)
    max_per_ind = getattr(_cfg, "RANK_MAX_STOCKS_PER_IND", 5)
    target_date = date_str or datetime.now().strftime("%Y%m%d")
    is_historical = date_str is not None  # 盘后模式标志

    # ── 0. 东财健康预检（3s 快速探测，失败则全局熔断跳过所有东财调用）──
    import time as _time
    _l2_start = _time.monotonic()

    _t0 = _time.monotonic()
    _em_health_probe(timeout=3.0)
    _detail(f"  [⏱] 东财健康预检 — {_time.monotonic() - _t0:.1f}s (熔断={_EM_CIRCUIT_OPEN})")

    # ── 1. 加载所有数据源 ────────────────────────────────
    _t0 = _time.monotonic()
    mode_label = f"历史（{target_date}）" if is_historical else "实时"
    _log(f"  加载多维数据源（{mode_label}）...")

    change_df = None
    name_df = None

    # 并行加载所有独立数据源（ThreadPool，IO密集型）
    # 盘中模式：change_em + name_em 也并入并行池
    from concurrent.futures import ThreadPoolExecutor, as_completed
    _pool_tasks = {
        "zt_pool":      lambda: _load_zt_pool(target_date),
        "strong_pool":  lambda: _load_strong_pool(target_date),
        "margin_map":   lambda: _load_margin_map(target_date),
        "dt_count":     lambda: _load_dt_pool(target_date),
        "zbgc_count":   lambda: _load_zbgc_pool(target_date),
        "name_to_code": lambda: _build_name_to_code_map(),
    }
    # 概念板块排行（盘中/盘后均可）
    _pool_tasks["concepts"] = lambda: get_concept_rankings()

    if not is_historical:
        def _load_change_em():
            try:
                df = ak.stock_board_change_em()
                if df is not None and not df.empty:
                    _detail(f"  [✓] 全板块异动 {len(df)} 条")
                    return df
            except Exception as e:
                _detail(f"  [!] 全板块异动接口失败（{e.__class__.__name__}）")
            return None
        _pool_tasks["change_df"] = _load_change_em
        _pool_tasks["name_df"] = lambda: _fetch_industry_ranking_with_fallback()
        _pool_tasks["concepts"] = lambda: get_concept_rankings()
    else:
        _detail("  [i] 盘后模式：跳过 change_em / name_em（仅实时接口）")
    _pool_results = {}
    with _no_proxy(), ThreadPoolExecutor(max_workers=8) as pool:
        futures = {pool.submit(fn): key for key, fn in _pool_tasks.items()}
        for f in as_completed(futures):
            key = futures[f]
            try:
                _pool_results[key] = f.result()
            except Exception as e:
                _detail(f"  [!] {key} 加载异常（{e.__class__.__name__}）")
                _pool_results[key] = {} if "map" in key or "count" in key else None
    zt_pool     = _pool_results.get("zt_pool", {})
    strong_pool = _pool_results.get("strong_pool", {})
    margin_map  = _pool_results.get("margin_map", {})
    dt_count    = _pool_results.get("dt_count", {})
    zbgc_count  = _pool_results.get("zbgc_count", {})
    name_to_code = _pool_results.get("name_to_code", {})
    change_df   = _pool_results.get("change_df")
    name_df     = _pool_results.get("name_df")
    concepts    = _pool_results.get("concepts", [])
    if name_df is not None and not isinstance(name_df, pd.DataFrame):
        name_df = None
    if name_df is not None and not name_df.empty:
        _detail(f"  [✓] 行业板块行情 {len(name_df)} 条")
    concepts: List[ConceptRanking] = _pool_results.get("concepts") or []
    _detail(f"  [⏱] 数据源并行加载 — {_time.monotonic() - _t0:.1f}s")

    # ── 2. 榜单 A：涨幅排行（盘中）/ 涨停密度排行（盘后）──
    _t0 = _time.monotonic()
    gain_industries: list = []   # [(行业名, {gain_pct, net_inflow, leading_name, leading_gain})]

    if is_historical:
        # 盘后模式：按涨停密度排行（替代涨幅排行）
        zt_density_rank = sorted(
            [(ind, len(stocks)) for ind, stocks in zt_pool.items()],
            key=lambda x: -x[1]
        )[:top_n]
        for ind_name, zt_cnt in zt_density_rank:
            gain_industries.append((ind_name, {
                "gain_pct": 0.0,
                "net_inflow": 0.0,
                "leading_name": "",
                "leading_gain": 0.0,
                "zt_count": zt_cnt,
            }))
    elif name_df is not None and not name_df.empty and "涨跌幅" in name_df.columns:
        sorted_df = name_df.sort_values("涨跌幅", ascending=False)
        for _, row in sorted_df.head(top_n).iterrows():
            ind_name = str(row.get("板块名称", "")).strip()
            if not ind_name:
                continue
            gain_industries.append((ind_name, {
                "gain_pct": float(row.get("涨跌幅", 0) or 0),
                "net_inflow": 0.0,
                "leading_name": str(row.get("领涨股票", "")),
                "leading_gain": float(row.get("领涨股票-涨跌幅", 0) or 0),
            }))
    elif change_df is not None and not change_df.empty:
        industry_names = _get_known_industry_names()
        df_ind = (change_df[change_df["板块名称"].isin(industry_names)]
                  if industry_names else change_df)
        df_ind = df_ind.sort_values("涨跌幅", ascending=False)
        for _, row in df_ind.head(top_n).iterrows():
            ind_name = str(row["板块名称"])
            gain_industries.append((ind_name, {
                "gain_pct": float(row.get("涨跌幅", 0) or 0),
                "net_inflow": float(row.get("主力净流入", 0) or 0) / 1e8,
                "leading_name": "",
                "leading_gain": 0.0,
            }))

    _detail(f"  [⏱] 涨幅/涨停榜 — {_time.monotonic() - _t0:.1f}s ({len(gain_industries)} 行业)")

    # ── 3. 榜单 B：综合强度排行 ──────────────────────────
    _t0 = _time.monotonic()
    if is_historical:
        all_scores = compute_historical_composite_scores(
            zt_pool, strong_pool, dt_count, zbgc_count
        )
    else:
        all_scores = compute_industry_composite_scores(
            change_df, zt_pool, strong_pool, dt_count, zbgc_count
        )
    composite_industries = all_scores[:top_n]  # list of dict

    _detail(f"  [⏱] 综合评分 — {_time.monotonic() - _t0:.1f}s ({len(composite_industries)} 行业)")

    # ── 4. 合并双榜 → 为每行业选代表股 ──────────────────
    _t0 = _time.monotonic()
    gain_names = {x[0] for x in gain_industries}
    comp_names = {x["name"] for x in composite_industries}
    all_ind_names = list(dict.fromkeys(
        [x[0] for x in gain_industries] + [x["name"] for x in composite_industries]
    ))

    # 辅助：从 change_df 获取行业信息
    change_map: dict = {}
    if change_df is not None and not change_df.empty:
        for _, row in change_df.iterrows():
            change_map[str(row["板块名称"])] = row

    # 辅助：gain_industries → dict
    gain_map = {x[0]: x[1] for x in gain_industries}

    # 辅助：composite → dict
    comp_map = {x["name"]: x for x in composite_industries}

    def _select_candidates(ind_name: str) -> List[StockCandidate]:
        """为单个行业从 6 个维度选出候选股。"""
        cands: list = []

        # 1. 涨停股（优先级1）
        for code6, sname, lianban, ftime in zt_pool.get(ind_name, []):
            futu = _code6_to_futu(code6)
            if futu:
                detail = f"{lianban}连板" if lianban > 1 else "涨停"
                if ftime:
                    detail += f",{ftime}"
                cands.append(StockCandidate(
                    code=futu, name=sname, role="涨停",
                    priority=1, detail=detail))
        # 涨停股按连板数降序，只保留最强的2只
        zt_cands = sorted(
            [c for c in cands if c.role == "涨停"],
            key=lambda c: -int(c.detail.split("连板")[0])
            if "连板" in c.detail else 0)
        cands = zt_cands[:2]

        # 2. 异动股（优先级2）
        if ind_name in change_map:
            row = change_map[ind_name]
            anomaly_code = str(
                row.get("板块异动最频繁个股及所属类型-股票代码", "")).strip()
            anomaly_name = str(
                row.get("板块异动最频繁个股及所属类型-股票名称", "")).strip()
            if anomaly_code and anomaly_code != "nan":
                futu = _code6_to_futu(anomaly_code)
                if futu:
                    inflow = float(row.get("主力净流入", 0) or 0) / 1e8
                    cands.append(StockCandidate(
                        code=futu, name=anomaly_name, role="异动",
                        priority=2, detail=f"净流入{inflow:.1f}亿"))

        # 3. 领涨股（优先级3，需名称→代码映射）
        g_info = gain_map.get(ind_name, {})
        leading_name = g_info.get("leading_name", "")
        if leading_name and name_to_code:
            code6 = name_to_code.get(leading_name, "")
            if code6:
                futu = _code6_to_futu(code6)
                if futu:
                    lg = g_info.get("leading_gain", 0)
                    cands.append(StockCandidate(
                        code=futu, name=leading_name, role="领涨",
                        priority=3,
                        detail=f"{'+' if lg >= 0 else ''}{lg:.1f}%"))

        # 4. 强势股（优先级4，取量比最高的1只）
        strong_list = strong_pool.get(ind_name, [])
        if strong_list:
            best = max(strong_list, key=lambda x: x[2])
            futu = _code6_to_futu(best[0])
            if futu:
                cands.append(StockCandidate(
                    code=futu, name=best[1], role="强势",
                    priority=4, detail=f"量比{best[2]:.1f}"))

        # 5. 行业龙头（优先级5，静态）
        if ind_name in _INDUSTRY_LEADERS:
            leader_code, leader_name = _INDUSTRY_LEADERS[ind_name]
            cands.append(StockCandidate(
                code=leader_code, name=leader_name, role="龙头",
                priority=5, detail="权重股"))

        # 去重（按代码），保留优先级最高的
        seen: set = set()
        unique: list = []
        for c in sorted(cands, key=lambda x: x.priority):
            if c.code not in seen:
                seen.add(c.code)
                unique.append(c)

        # 融资标注（补充信息，不影响入池）
        for c in unique:
            code6 = c.code.split(".")[-1] if "." in c.code else ""
            if code6 in margin_map:
                margin_amt = margin_map[code6]
                c.detail += f",融资{margin_amt / 1e8:.1f}亿"

        return unique[:max_per_ind]

    # ── 构建三个列表 ─────────────────────────────────────
    def _build_ranking(ind_name: str, candidates: List[StockCandidate]) -> IndustryRanking:
        g = gain_map.get(ind_name, {})
        c = comp_map.get(ind_name, {})
        source = "both" if (ind_name in gain_names and ind_name in comp_names) \
            else ("gain" if ind_name in gain_names else "composite")

        g_rank = 0
        if ind_name in gain_names:
            g_names_list = [x[0] for x in gain_industries]
            g_rank = g_names_list.index(ind_name) + 1 if ind_name in g_names_list else 0

        c_rank = 0
        if ind_name in comp_names:
            c_names_list = [x["name"] for x in composite_industries]
            c_rank = c_names_list.index(ind_name) + 1 if ind_name in c_names_list else 0

        # 板块属性标签
        sector_type_map = getattr(_cfg, "SECTOR_TYPE_MAP", {})
        stype = sector_type_map.get(ind_name, "中性")

        return IndustryRanking(
            name=ind_name,
            gain_rank=g_rank,
            composite_rank=c_rank,
            gain_pct=g.get("gain_pct", c.get("gain", 0.0)),
            net_inflow=g.get("net_inflow", 0.0) or (c.get("inflow", 0) / 1e8 if c else 0.0),
            composite_score=c.get("composite_score", 0.0),
            source=source,
            candidates=candidates,
            zt_count=c.get("zt_count", len(zt_pool.get(ind_name, []))),
            strong_count=c.get("strong_count", len(strong_pool.get(ind_name, []))),
            zbgc_count=c.get("zbgc", zbgc_count.get(ind_name, 0)),
            sector_type=stype,
            concept_tags=concept_ind_map.get(ind_name, []),
            rotation_line=_cfg.ROTATION_LINE_MAP.get(ind_name, ""),
        )

    # 概念→行业关联
    concept_ind_map = _match_concepts_to_industries(
        concepts, all_ind_names, name_df)

    # 选股（所有上榜行业）
    all_cands_map = {name: _select_candidates(name) for name in all_ind_names}

    # 涨幅榜列表
    gain_list = [_build_ranking(x[0], all_cands_map.get(x[0], []))
                 for x in gain_industries]

    # 综合榜列表
    composite_list = [_build_ranking(x["name"], all_cands_map.get(x["name"], []))
                      for x in composite_industries]

    # 合并列表（去重，保持顺序：先涨幅榜、再综合榜独有）
    merged_list: list = []
    seen_names: set = set()
    for r in gain_list:
        seen_names.add(r.name)
        merged_list.append(r)
    for r in composite_list:
        if r.name not in seen_names:
            seen_names.add(r.name)
            merged_list.append(r)

    _detail(f"  [⏱] 选股+构建列表 — {_time.monotonic() - _t0:.1f}s ({len(merged_list)} 合并)")

    # ── 5. 设置 sector_type 和 concept_tags ──
    concept_map = _match_concepts_to_industries(
        concepts, [r.name for r in merged_list], name_df)
    for r in gain_list + composite_list + merged_list:
        r.sector_type = _classify_industry(r.name)
        if r.name in concept_map:
            r.concept_tags = concept_map[r.name]

    # ── 5.5 P3-3: 板块节奏检测（所有上榜行业）──
    # gain_list 和 composite_list 是独立对象，需分别 enrich
    _t0 = _time.monotonic()
    _enrich_rhythm(gain_list)
    _enrich_rhythm(composite_list)
    _detail(f"  [⏱] 节奏检测 — {_time.monotonic() - _t0:.1f}s")

    # ── 6. 超跌检测（盘中模式有 name_df 时）──
    oversold_list: List[IndustryRanking] = []
    if not is_historical and name_df is not None:
        try:
            oversold_list = get_oversold_industries(name_df)
        except Exception as e:
            _detail(f"  [!] 超跌检测失败（{e.__class__.__name__}）")

    # ── 7. 情绪统计（供 MarketContext.update_sentiment 使用）──
    _zt_total = sum(len(v) for v in zt_pool.values())
    _dt_total = sum(v for v in dt_count.values())
    _lianban_max = max(
        (max((x[2] for x in v), default=0) for v in zt_pool.values()),
        default=0,
    ) if zt_pool else 0
    sentiment_stats = {
        "zt_total": _zt_total,
        "dt_total": _dt_total,
        "lianban_max": _lianban_max,
        "name_df": name_df,
    }

    _l2_elapsed = _time.monotonic() - _l2_start
    _log(f"  [⏱] L2 行业分析完成，耗时 {_l2_elapsed:.1f}s")

    return gain_list, composite_list, merged_list, concepts, oversold_list, sentiment_stats


# ─────────────────────────────────────────────────────────
# 抄底候选板块筛选（恐慌时触发）
# ─────────────────────────────────────────────────────────

def get_bottom_fishing_candidates(
    name_df,
    oversold_list: List[IndustryRanking],
    panic_score: float,
    themes: List[str] = None,
    top_n: int = 5,
) -> List[IndustryRanking]:
    """
    恐慌行情中筛选抄底候选板块。

    选择逻辑:
    1. 从 name_df 取今日跌幅最大的 15 个行业
    2. 与历史超跌 oversold_list 取交集 → "双重超跌"
    3. 评分加权:
       - 今日跌幅分 (35): 跌得越多分越高
       - 历史超跌分 (30): 复用 oversold_score
       - 轮动线加分 (20): 进攻/科技型板块恐慌后弹性最大
       - 主题命中   (15): 命中用户关注主题
    4. 排序输出 top N

    :param name_df: 东财行业板块 DataFrame (含板块名+涨跌幅)
    :param oversold_list: L2 历史超跌列表
    :param panic_score: 盘中恐慌评分 (0-100)
    :param themes: 用户关注主题列表
    :param top_n: 输出数量
    :return: IndustryRanking 列表（按抄底得分降序）
    """
    if name_df is None or name_df.empty:
        return []

    if panic_score < 40:
        return []

    import config as _cfg

    # 找列名
    name_col = None
    for col in ['板块名称', '板块', '名称', '行业']:
        if col in name_df.columns:
            name_col = col
            break
    change_col = None
    for col in ['涨跌幅', '涨跌幅(%)', '涨幅', '涨幅(%)', '最新涨跌幅']:
        if col in name_df.columns:
            change_col = col
            break
    if not name_col or not change_col:
        return []

    # 1. 取今日跌幅最大的 15 个行业
    df = name_df[[name_col, change_col]].copy()
    df[change_col] = pd.to_numeric(df[change_col], errors='coerce')
    df = df.dropna(subset=[change_col])
    df = df.sort_values(change_col, ascending=True).head(15)

    # 历史超跌名单
    oversold_map = {r.name: r.oversold_score for r in oversold_list}

    # 主题关键词
    theme_keywords = []
    if themes:
        from signals.core.theme_tracker import THEME_KEYWORD_MAP
        for t in themes:
            kws = THEME_KEYWORD_MAP.get(t.upper().strip()) or THEME_KEYWORD_MAP.get(t.strip())
            if kws:
                theme_keywords.extend(kws)
            else:
                theme_keywords.append(t.strip())

    # 板块属性和轮动线
    sector_type_map = getattr(_cfg, "SECTOR_TYPE_MAP", {})
    rotation_map = getattr(_cfg, "ROTATION_LINE_MAP", {})

    candidates = []
    for _, row in df.iterrows():
        ind_name = str(row[name_col])
        change = float(row[change_col])

        # 今日跌幅分 (满分35): 跌幅>3%满分, 1~3%线性
        drop = abs(change) if change < 0 else 0
        if drop >= 3.0:
            decline_pts = 35.0
        elif drop >= 1.0:
            decline_pts = 35.0 * (drop - 1.0) / 2.0
        else:
            decline_pts = 0.0

        # 历史超跌分 (满分30): 直接取 oversold_score 归一化
        hist_score = oversold_map.get(ind_name, 0.0)
        oversold_pts = min(30.0, hist_score * 0.3)  # oversold_score 0-100 → 0-30

        # 轮动线/板块属性加分 (满分20): 进攻型+科技线弹性最大
        stype = sector_type_map.get(ind_name, "中性")
        rot_line = rotation_map.get(ind_name, "")
        attr_pts = 0.0
        if stype == "进攻":
            attr_pts += 12.0
        elif stype == "周期":
            attr_pts += 6.0
        if rot_line == "科技":
            attr_pts += 8.0
        elif rot_line == "新能源":
            attr_pts += 5.0
        attr_pts = min(20.0, attr_pts)

        # 主题命中 (满分15)
        theme_pts = 0.0
        if theme_keywords and any(kw in ind_name for kw in theme_keywords):
            theme_pts = 15.0

        total = decline_pts + oversold_pts + attr_pts + theme_pts

        if total > 0:
            r = IndustryRanking(
                name=ind_name,
                gain_pct=change,
                composite_score=round(total, 1),
                sector_type=stype,
                rotation_line=rot_line,
                oversold_score=hist_score,
                oversold_detail=f"今日{change:+.1f}%",
            )
            candidates.append(r)

    candidates.sort(key=lambda x: x.composite_score, reverse=True)
    return candidates[:top_n]
