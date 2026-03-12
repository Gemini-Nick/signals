#!/usr/bin/env python3
"""
本地预缓存成分股数据 — 用于上传到 AutoDL（push2 被封的环境）

用法:
    python scripts/prebuild_cache.py

生成: .data/cache/stocks_*.json  (每个行业一个文件)
上传: scp -P <port> -r .data/cache/ root@<autodl_host>:/root/autodl-tmp/signals/.data/cache/
"""
import json
import time
import sys
import os

# 清除代理环境变量，避免 Clash TUN 干扰
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".data", "cache")
os.makedirs(CACHE_DIR, exist_ok=True)


def save(key, data):
    path = os.path.join(CACHE_DIR, f"{key}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    print(f"  ✅ {key}: {len(data)} 只")


def main():
    import akshare as ak

    # 1. 获取所有行业名称
    print(">>> 获取行业列表...")
    try:
        df = ak.stock_board_industry_name_em()
        industries = df["板块名称"].tolist()
        print(f"  共 {len(industries)} 个行业")
    except Exception as e:
        print(f"  ❌ 获取行业列表失败: {e}")
        return

    # 2. 逐个抓成分股
    success = 0
    fail = 0
    for i, name in enumerate(industries, 1):
        print(f"[{i}/{len(industries)}] {name}...", end=" ")
        try:
            df = ak.stock_board_industry_cons_em(symbol=name)
            if df is None or df.empty:
                print("空")
                fail += 1
                continue

            # 提取代码
            code_col = None
            for col in ["代码", "code", "股票代码"]:
                if col in df.columns:
                    code_col = col
                    break
            if not code_col:
                print(f"无代码列 (列: {list(df.columns)})")
                fail += 1
                continue

            codes = []
            for raw in df[code_col].astype(str):
                c = raw.strip()
                if len(c) == 6 and c.isdigit():
                    prefix = "SH." if c.startswith(("6", "5")) else "SZ."
                    codes.append(prefix + c)

            if codes:
                save(f"stocks_{name}", codes)
                success += 1
            else:
                print("无有效代码")
                fail += 1

        except Exception as e:
            print(f"❌ {e.__class__.__name__}")
            fail += 1

        # 防止频率限制
        time.sleep(0.3)

    print(f"\n>>> 完成！成功: {success}, 失败: {fail}")
    print(f"  缓存目录: {CACHE_DIR}")
    print(f"\n>>> 上传到 AutoDL:")
    print(f"  scp -P <port> -r .data/cache/ root@<host>:/root/autodl-tmp/signals/.data/cache/")


if __name__ == "__main__":
    main()
