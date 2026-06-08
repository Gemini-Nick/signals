# -*- coding: utf-8 -*-
"""Optional public fund-flow evidence for market replay.

These helpers fetch and parse source data.  They do not decide how to write the
replay, and they deliberately keep source口径 visible.
"""
from __future__ import annotations

from datetime import datetime
import time
from typing import Any
from zoneinfo import ZoneInfo

import requests


_EM_QUOTE_FIELDS = ",".join(
    [
        "f48",  # amount
        "f57",  # code
        "f58",  # name
        "f86",  # quote update timestamp
        "f135",
        "f136",
        "f137",
        "f138",
        "f139",
        "f140",
        "f141",
        "f142",
        "f143",
        "f144",
        "f145",
        "f146",
        "f148",
        "f149",
    ]
)
_EM_ULIST_FIELDS = ",".join(
    [
        "f6",  # amount
        "f62",
        "f184",
        "f64",
        "f65",
        "f66",
        "f69",
        "f70",
        "f71",
        "f72",
        "f75",
        "f76",
        "f77",
        "f78",
        "f81",
        "f82",
        "f83",
        "f84",
        "f87",
        "f124",
    ]
)
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 Chrome/124 Safari/537.36"
    ),
    "Accept": "application/json,text/plain,*/*",
    "Referer": "https://data.eastmoney.com/zjlx/detail.html",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _yi(value: Any) -> float | None:
    number = _float(value)
    if number is None:
        return None
    return round(number / 100000000, 2)


def _pure_code(symbol: str) -> str:
    value = _text(symbol).upper()
    for prefix in ("SZ.", "SH.", "BJ."):
        if value.startswith(prefix):
            return value[3:]
    return value


def _eastmoney_secid(symbol: str) -> str:
    code = _pure_code(symbol)
    market = 1 if code.startswith(("6", "9")) else 0
    return f"{market}.{code}"


def _quote_trade_date(value: Any) -> str:
    timestamp = _float(value)
    if timestamp is None:
        return ""
    try:
        return datetime.fromtimestamp(timestamp, ZoneInfo("Asia/Shanghai")).date().isoformat()
    except Exception:
        return ""


def _flow_pair(data: dict[str, Any], buy_key: str, sell_key: str, net_key: str) -> dict[str, Any]:
    buy_yi = _yi(data.get(buy_key))
    sell_yi = _yi(data.get(sell_key))
    net_yi = _yi(data.get(net_key))
    if buy_yi is None and sell_yi is None and net_yi is None:
        return {}
    return {
        "buy_yi": buy_yi,
        "sell_yi": sell_yi,
        "net_yi": net_yi,
        "raw_fields": {"buy": buy_key, "sell": sell_key, "net": net_key},
    }


def _merge_flow_pairs(first: dict[str, Any], second: dict[str, Any], *, label: str) -> dict[str, Any]:
    if not first and not second:
        return {}
    buy_yi = None
    sell_yi = None
    net_yi = None
    if first.get("buy_yi") is not None or second.get("buy_yi") is not None:
        buy_yi = round((first.get("buy_yi") or 0.0) + (second.get("buy_yi") or 0.0), 2)
    if first.get("sell_yi") is not None or second.get("sell_yi") is not None:
        sell_yi = round((first.get("sell_yi") or 0.0) + (second.get("sell_yi") or 0.0), 2)
    if first.get("net_yi") is not None or second.get("net_yi") is not None:
        net_yi = round((first.get("net_yi") or 0.0) + (second.get("net_yi") or 0.0), 2)
    return {
        "buy_yi": buy_yi,
        "sell_yi": sell_yi,
        "net_yi": net_yi,
        "basis": label,
        "components": [item for item in (first, second) if item],
    }


def parse_eastmoney_ulist_fund_flow(data: dict[str, Any], *, requested_trade_date: str = "") -> dict[str, Any]:
    """Parse Eastmoney ``ulist.np/get`` fund-flow fields.

    This is the endpoint used by Eastmoney's own zjlx detail page for the
    realtime numeric panel.  It exposes buy/sell/net for super-large, large,
    medium, and small order-size buckets.
    """
    if not isinstance(data, dict) or not data:
        return {}
    super_large = _flow_pair(data, "f64", "f65", "f66")
    large = _flow_pair(data, "f70", "f71", "f72")
    medium = _flow_pair(data, "f76", "f77", "f78")
    small = _flow_pair(data, "f82", "f83", "f84")
    main_order = _merge_flow_pairs(super_large, large, label="super_large_order + large_order")
    if main_order and data.get("f62") is not None:
        main_order["net_yi"] = _yi(data.get("f62"))
    amount_yi = _yi(data.get("f6"))
    coverage_gap_yi = None
    if amount_yi is not None and main_order and medium:
        covered = (
            (main_order.get("buy_yi") or 0.0)
            + (medium.get("buy_yi") or 0.0)
            + (small.get("buy_yi") or 0.0)
        )
        coverage_gap_yi = round(amount_yi - covered, 2)
    return {
        "source": "eastmoney_ulist_np",
        "requested_trade_date": requested_trade_date,
        "observed_trade_date": _quote_trade_date(data.get("f124")),
        "amount_yi": amount_yi,
        "main_order": main_order,
        "super_large_order": super_large,
        "large_order": large,
        "medium_order": medium,
        "small_order": small,
        "retail_proxy": {
            **_merge_flow_pairs(medium, small, label="medium_order + small_order"),
            "basis": "medium_order + small_order; order-size proxy, not account-level retail",
        }
        if medium or small
        else {},
        "amount_coverage_gap_yi": coverage_gap_yi,
        "order_size_buy_sell_available": bool(main_order and main_order.get("buy_yi") is not None),
        "participant_flow_available": False,
        "note": (
            "Eastmoney ulist fields provide buy/sell/net for order-size buckets. "
            "They are useful for acceptance analysis but not account-level 主力/散户 proof."
        ),
    }


def parse_eastmoney_quote_fund_flow(data: dict[str, Any], *, requested_trade_date: str = "") -> dict[str, Any]:
    """Parse Eastmoney quote fund-flow fields from ``/api/qt/stock/get``.

    Field mapping is source-specific:
    f135/f136/f137 = main-order buy/sell/net.
    f138-f143 split main into super-large and large.
    f144/f145/f146 = medium-order buy/sell/net.

    This is order-size flow, not a verified account-level main/retail split.
    """
    if not isinstance(data, dict) or not data:
        return {}
    trade_date = _quote_trade_date(data.get("f86"))
    main_order = _flow_pair(data, "f135", "f136", "f137")
    medium_order = _flow_pair(data, "f144", "f145", "f146")
    amount_yi = _yi(data.get("f48"))
    coverage_gap_yi = None
    if amount_yi is not None and main_order and medium_order:
        covered = (main_order.get("buy_yi") or 0.0) + (medium_order.get("buy_yi") or 0.0)
        coverage_gap_yi = round(amount_yi - covered, 2)
    return {
        "source": "eastmoney_quote_stock_get",
        "requested_trade_date": requested_trade_date,
        "observed_trade_date": trade_date,
        "code": _text(data.get("f57")),
        "name": _text(data.get("f58")),
        "amount_yi": amount_yi,
        "main_order": main_order,
        "super_large_order": _flow_pair(data, "f138", "f139", "f140"),
        "large_order": _flow_pair(data, "f141", "f142", "f143"),
        "medium_order": medium_order,
        "retail_proxy": {
            **medium_order,
            "basis": "medium_order_flow; small-order buy/sell is not exposed by this quote endpoint",
        }
        if medium_order
        else {},
        "small_order_net_yi": _yi(data.get("f149")),
        "amount_coverage_gap_yi": coverage_gap_yi,
        "order_size_buy_sell_available": bool(main_order and main_order.get("buy_yi") is not None),
        "participant_flow_available": False,
        "note": (
            "Eastmoney quote fields provide order-size buy/sell and net-flow evidence. "
            "They do not prove an account-level 主力/散户 split, and historical endpoints expose net fields only."
        ),
    }


def _get_with_retries(
    session: requests.Session,
    url: str,
    *,
    params: dict[str, Any],
    timeout: float,
) -> requests.Response:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(url, params=params, headers=_HEADERS, timeout=timeout)
            response.raise_for_status()
            return response
        except Exception as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.3)
    raise last_exc or RuntimeError(f"request failed: {url}")


def _get_first_available(
    session: requests.Session,
    urls: list[str],
    *,
    params: dict[str, Any],
    timeout: float,
) -> requests.Response:
    errors: list[str] = []
    for url in urls:
        try:
            return _get_with_retries(session, url, params=params, timeout=timeout)
        except Exception as exc:
            errors.append(f"{url}:{type(exc).__name__}:{exc}")
    raise RuntimeError("; ".join(errors))


def fetch_eastmoney_ulist_fund_flow(symbol: str, *, trade_date: str = "", timeout: float = 8.0) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    try:
        response = _get_first_available(
            session,
            [
                "https://push2delay.eastmoney.com/api/qt/ulist.np/get",
                "http://push2delay.eastmoney.com/api/qt/ulist.np/get",
                "https://push2.eastmoney.com/api/qt/ulist.np/get",
                "http://push2.eastmoney.com/api/qt/ulist.np/get",
            ],
            params={
                "fltt": "2",
                "secids": _eastmoney_secid(symbol),
                "fields": _EM_ULIST_FIELDS,
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "_": int(time.time() * 1000),
            },
            timeout=timeout,
        )
        payload = response.json()
    finally:
        session.close()
    rows = (payload.get("data") or {}).get("diff") or []
    return parse_eastmoney_ulist_fund_flow(rows[0] if rows else {}, requested_trade_date=trade_date)


def fetch_eastmoney_quote_fund_flow(symbol: str, *, trade_date: str = "", timeout: float = 8.0) -> dict[str, Any]:
    session = requests.Session()
    session.trust_env = False
    try:
        response = _get_first_available(
            session,
            [
                "https://push2.eastmoney.com/api/qt/stock/get",
                "http://push2.eastmoney.com/api/qt/stock/get",
            ],
            params={
                "secid": _eastmoney_secid(symbol),
                "fields": _EM_QUOTE_FIELDS,
                "ut": "b2884a393a59ad64002292a3e90d46a5",
                "_": int(time.time() * 1000),
            },
            timeout=timeout,
        )
        payload = response.json()
    finally:
        session.close()
    return parse_eastmoney_quote_fund_flow(payload.get("data") or {}, requested_trade_date=trade_date)


def parse_ths_real_funds(payload: dict[str, Any], *, requested_trade_date: str = "") -> dict[str, Any]:
    flash = payload.get("flash") if isinstance(payload.get("flash"), list) else []
    buckets: dict[str, dict[str, float]] = {}
    for row in flash:
        if not isinstance(row, dict):
            continue
        name = _text(row.get("name"))
        amount = _yi((_float(row.get("sr")) or 0.0) * 10000)
        if amount is None:
            continue
        bucket = "unknown"
        if "大单" in name:
            bucket = "big_order"
        elif "中单" in name:
            bucket = "medium_order"
        elif "小单" in name:
            bucket = "small_order"
        side = "in_yi" if "流入" in name else "out_yi" if "流出" in name else "unknown_yi"
        buckets.setdefault(bucket, {})[side] = amount
    for values in buckets.values():
        if "in_yi" in values or "out_yi" in values:
            values["net_yi"] = round(values.get("in_yi", 0.0) - values.get("out_yi", 0.0), 2)
    title = payload.get("title") if isinstance(payload.get("title"), dict) else {}
    return {
        "source": "ths_real_funds",
        "requested_trade_date": requested_trade_date,
        "total_in_yi": _yi((_float(title.get("zlr")) or 0.0) * 10000),
        "total_out_yi": _yi((_float(title.get("zlc")) or 0.0) * 10000),
        "net_yi": _yi((_float(title.get("je")) or 0.0) * 10000),
        "buckets": buckets,
        "order_size_buy_sell_available": bool(buckets),
        "participant_flow_available": False,
        "note": "THS realFunds exposes big/medium/small order distribution for the current quote day.",
    }


def fetch_ths_real_funds(symbol: str, *, trade_date: str = "", timeout: float = 8.0) -> dict[str, Any]:
    code = _pure_code(symbol)
    session = requests.Session()
    session.trust_env = False
    try:
        response = session.get(
            f"https://stockpage.10jqka.com.cn/spService/{code}/Funds/realFunds",
            headers={**_HEADERS, "Referer": f"https://f10.10jqka.com.cn/{code}/funds/"},
            timeout=timeout,
        )
        response.raise_for_status()
        payload = response.json()
    finally:
        session.close()
    return parse_ths_real_funds(payload, requested_trade_date=trade_date)


def fetch_stock_fund_flow_evidence(symbol: str, *, trade_date: str = "", timeout: float = 8.0) -> dict[str, Any]:
    """Fetch best-effort public flow evidence for one stock.

    Network failures return a structured error so callers can keep the replay
    deterministic and preserve the data boundary.
    """
    result: dict[str, Any] = {"symbol": symbol, "code": _pure_code(symbol), "trade_date": trade_date}
    errors: list[str] = []
    try:
        result["eastmoney_quote"] = fetch_eastmoney_ulist_fund_flow(symbol, trade_date=trade_date, timeout=timeout)
    except Exception as exc:
        errors.append(f"eastmoney_ulist:{type(exc).__name__}:{exc}")
        try:
            result["eastmoney_quote"] = fetch_eastmoney_quote_fund_flow(symbol, trade_date=trade_date, timeout=timeout)
        except Exception as quote_exc:
            errors.append(f"eastmoney_quote:{type(quote_exc).__name__}:{quote_exc}")
    try:
        result["ths_real_funds"] = fetch_ths_real_funds(symbol, trade_date=trade_date, timeout=timeout)
    except Exception as exc:
        errors.append(f"ths_real_funds:{type(exc).__name__}:{exc}")
    result["errors"] = errors
    result["order_size_buy_sell_available"] = bool(
        result.get("eastmoney_quote", {}).get("order_size_buy_sell_available")
        or result.get("ths_real_funds", {}).get("order_size_buy_sell_available")
    )
    result["participant_flow_available"] = False
    return result
