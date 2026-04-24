# -*- coding: utf-8 -*-
"""
东财直连上下文

用法:
    with em_proxy():
        df = ak.stock_zh_a_hist(symbol="600519")

与 fetcher.py 中的 no_proxy() 互补：
    no_proxy()  — 剥离代理直连国内数据源
    em_proxy()  — sync 模块里的东财专用直连上下文
"""
import os
from contextlib import contextmanager


@contextmanager
def em_proxy(proxy_url: str = None):
    """
    为东财 API 调用临时清除代理环境变量，强制直连。

    当前本机使用虚拟网卡 + 规则路由，东财接口应直接出站；如果继承
    HTTP_PROXY/HTTPS_PROXY/ALL_PROXY，AKShare 底层 requests 可能走错链路。

    :param proxy_url: 兼容旧调用签名；传入也会被忽略。
    """
    keys = ("http_proxy", "https_proxy", "all_proxy",
            "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")
    saved = {k: os.environ.pop(k) for k in keys if k in os.environ}
    try:
        yield
    finally:
        for k, v in saved.items():
            os.environ[k] = v
