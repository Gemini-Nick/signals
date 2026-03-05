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
            print(f"  [IBSource] 连接成功 "
                  f"({config.IB_HOST}:{config.IB_PORT})", flush=True)
        except ImportError:
            print("  [IBSource] 跳过: ib_async 未安装 "
                  "(pip install ib_async)", flush=True)
        except ConnectionRefusedError:
            print(f"  [IBSource] 跳过: IB Gateway 未运行 "
                  f"({config.IB_HOST}:{config.IB_PORT})", flush=True)
        except Exception as e:
            print(f"  [IBSource] 跳过: 连接失败 "
                  f"({type(e).__name__}: {e})", flush=True)

        # ── 2. Futu（盘中备选）────────────────────────────
        if futu_source:
            providers.append(futu_source)
            print("  [FutuSource] 已加入降级链（盘中备选）", flush=True)

    elif mode == "review":
        # ── 1. Alpaca（盘后优先）──────────────────────────
        alpaca_key = getattr(config, "ALPACA_API_KEY", "")
        alpaca_secret = getattr(config, "ALPACA_SECRET_KEY", "")

        if not alpaca_key:
            print("  [AlpacaSource] 跳过: ALPACA_API_KEY 未配置", flush=True)
        else:
            try:
                from .alpaca_source import AlpacaSource
                alp = AlpacaSource(alpaca_key, alpaca_secret)
                providers.append(alp)
                print("  [AlpacaSource] 初始化成功", flush=True)
            except ImportError:
                print("  [AlpacaSource] 跳过: alpaca-py 未安装 "
                      "(pip install alpaca-py)", flush=True)
            except Exception as e:
                print(f"  [AlpacaSource] 跳过: 初始化失败 "
                      f"({type(e).__name__}: {e})", flush=True)

    else:
        print(f"  [USDataSource] 未知模式 '{mode}'，使用 yfinance 兜底",
              flush=True)

    # 打印最终降级链
    chain_names = [src.__class__.__name__ for src in providers]
    chain_names.append("YFinanceSource(兜底)")
    print(f"  [USDataSource] 降级链: {' → '.join(chain_names)}", flush=True)

    return USDataSource(providers=providers)
