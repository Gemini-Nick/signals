#!/usr/bin/env python3
"""测试东财接口连通性（独立脚本，无项目依赖）"""
import os
for k in ["HTTP_PROXY","HTTPS_PROXY","ALL_PROXY","http_proxy","https_proxy","all_proxy"]:
    os.environ.pop(k, None)

import urllib.request
import json
import ssl

# 忽略 SSL 验证（绕过 Clash TUN 证书问题）
ctx = ssl.create_default_context()
ctx.check_hostname = False
ctx.verify_mode = ssl.CERT_NONE

tests = [
    ("行业列表(push2)", "https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=5&fs=m:90+t:2&fields=f12,f14"),
    ("行业列表(非push2)", "https://data.eastmoney.com/bkzj/hy.html"),
    ("新浪概念", "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/Market_Center.getHQNodeStockCount?node=new_gn"),
    ("同花顺", "https://q.10jqka.com.cn/"),
]

for name, url in tests:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        resp = urllib.request.urlopen(req, timeout=5, context=ctx)
        data = resp.read(200).decode("utf-8", errors="replace")
        print(f"✅ {name}: HTTP {resp.status}, {len(data)} bytes")
    except Exception as e:
        print(f"❌ {name}: {type(e).__name__}: {e}")
