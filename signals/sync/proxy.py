# -*- coding: utf-8 -*-
"""
隧道代理管理 — cheapproxy.net 隧道代理每次请求自动换 IP

用法:
    with em_proxy("http://user:pass@tunnel.cheapproxy.net:9020"):
        df = ak.stock_zh_a_hist(symbol="600519")

与 fetcher.py 中的 no_proxy() 互补：
    no_proxy()  — 本地开发，剥离代理直连国内数据源
    em_proxy()  — 云端部署，注入隧道代理绕东财 IP 封禁
"""
import os
from contextlib import contextmanager


@contextmanager
def em_proxy(proxy_url: str = None):
    """
    为东财 API 调用临时设置隧道代理。

    cheapproxy.net 隧道代理每次请求自动轮换 IP，
    无需手动维护 IP 池。设置 http_proxy/https_proxy 环境变量，
    AKShare 底层 requests 会自动使用。

    :param proxy_url: 代理地址，为空则不设置代理（透传）
    """
    if not proxy_url:
        yield
        return

    keys = ("http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY")
    saved = {k: os.environ.get(k) for k in keys}

    os.environ["http_proxy"] = proxy_url
    os.environ["https_proxy"] = proxy_url
    os.environ["HTTP_PROXY"] = proxy_url
    os.environ["HTTPS_PROXY"] = proxy_url
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
