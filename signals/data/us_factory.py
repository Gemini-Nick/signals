# -*- coding: utf-8 -*-
"""
美股数据源工厂 — 根据运行模式组装降级链

盘中 (intraday): [IBSource, FutuSource?] → YFinanceSource
盘后 (review):   [AlpacaSource]          → YFinanceSource

未配置的数据源自动跳过，降级到下一个。
全部未配置时行为与重构前一致（yfinance 兜底）。
"""

from typing import Optional

from .fetcher import USDataSource, FutuSource


def _log(msg: str):
    """Dashboard-aware logging: detail when panel active, print otherwise."""
    from signals.dashboard import get_dashboard
    dash = get_dashboard()
    if dash:
        dash.detail(msg)
    else:
        print(msg, flush=True)


def create_us_source(mode: str = "intraday",
                     futu_source: Optional[FutuSource] = None) -> USDataSource:
    """
    构建 USDataSource，根据 mode 组装正确的数据源优先级链。

    Parameters
    ----------
    mode : str
        "intraday" — 盘中模式（IB → Futu → yfinance）
        "review"   — 盘后模式（Alpaca → yfinance）
    futu_source : FutuSource, optional
        已连接的 Futu 实例（仅盘中模式使用，作为 IB 的备选）

    Returns
    -------
    USDataSource
        已装配好降级链的美股数据路由器
    """
    import config

    providers = []

    if mode == "intraday":
        # ── 1. IB（盘中优先）──────────────────────────────
        try:
            from .ib_source import IBSource
            ib = IBSource(config.IB_HOST, config.IB_PORT, config.IB_CLIENT_ID)
            ib.connect()
            providers.append(ib)
            _log(f"  [IBSource] 连接成功 ({config.IB_HOST}:{config.IB_PORT})")
        except ImportError:
            _log("  [IBSource] 跳过: ib_async 未安装")
        except ConnectionRefusedError:
            _log(f"  [IBSource] 跳过: IB Gateway 未运行")
        except Exception as e:
            _log(f"  [IBSource] 跳过: {type(e).__name__}")

        # ── 2. Futu（盘中备选）────────────────────────────
        if futu_source:
            providers.append(futu_source)
            _log("  [FutuSource] 已加入降级链")

    elif mode == "review":
        # ── 1. Alpaca（盘后优先）──────────────────────────
        alpaca_key = getattr(config, "ALPACA_API_KEY", "")
        alpaca_secret = getattr(config, "ALPACA_SECRET_KEY", "")

        if not alpaca_key:
            _log("  [AlpacaSource] 跳过: ALPACA_API_KEY 未配置")
        else:
            try:
                from .alpaca_source import AlpacaSource
                alp = AlpacaSource(alpaca_key, alpaca_secret)
                providers.append(alp)
                _log("  [AlpacaSource] 初始化成功")
            except ImportError:
                _log("  [AlpacaSource] 跳过: alpaca-py 未安装")
            except Exception as e:
                _log(f"  [AlpacaSource] 跳过: {type(e).__name__}")

    else:
        _log(f"  [USDataSource] 未知模式 '{mode}'，使用 yfinance 兜底")

    # 打印最终降级链
    chain_names = [src.__class__.__name__ for src in providers]
    chain_names.append("YFinanceSource(兜底)")
    _log(f"  [USDataSource] 降级链: {' → '.join(chain_names)}")

    return USDataSource(providers=providers)
