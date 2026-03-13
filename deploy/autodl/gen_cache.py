#!/usr/bin/env python3
"""
隆小侠 LONG CLAW — 东财数据缓存预生成

在能访问东财 API 的环境（本地电脑）运行，生成缓存文件。
缓存通过 git 同步到 AutoDL 云端，避免云端直连东财超时。

用法:
    python deploy/autodl/gen_cache.py          # 生成所有缓存
    python deploy/autodl/gen_cache.py --push   # 生成 + git commit & push

缓存文件:
    .cache/name_to_code.json     — 股票名称→代码映射（~5000只）
    .cache/board_industry.csv    — 行业板块列表（~90个）
"""
import sys
import os
import json
import time

# 确保项目根目录在 path 中
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "../.."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

CACHE_DIR = os.path.join(ROOT, ".cache")


def gen_name_to_code():
    """生成股票名称→代码映射"""
    import akshare as ak

    print("  [1/2] 拉取股票名称映射 (stock_info_a_code_name)...", end=" ", flush=True)
    try:
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            print("❌ 返回空数据")
            return False
        mapping = {}
        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            code = str(row.get("code", "")).strip().zfill(6)
            if name and code:
                mapping[name] = code
        path = os.path.join(CACHE_DIR, "name_to_code.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(mapping, f, ensure_ascii=False, indent=None)
        print(f"✅ {len(mapping)} 只股票")
        return True
    except Exception as e:
        print(f"❌ {e.__class__.__name__}: {e}")
        return False


def gen_board_industry():
    """生成行业板块列表"""
    import akshare as ak

    print("  [2/2] 拉取行业板块列表 (stock_board_industry_name_em)...", end=" ", flush=True)
    try:
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            print("❌ 返回空数据")
            return False
        path = os.path.join(CACHE_DIR, "board_industry.csv")
        df.to_csv(path, index=False, encoding="utf-8")
        print(f"✅ {len(df)} 个行业")
        return True
    except Exception as e:
        print(f"❌ {e.__class__.__name__}: {e}")
        return False


def main():
    os.makedirs(CACHE_DIR, exist_ok=True)

    print("🐲 隆小侠 — 东财数据缓存预生成")
    print(f"  缓存目录: {CACHE_DIR}")
    print()

    ok1 = gen_name_to_code()
    time.sleep(1)  # 避免东财限流
    ok2 = gen_board_industry()

    print()
    if ok1 and ok2:
        print("✅ 所有缓存生成完成!")
    else:
        print("⚠️  部分缓存生成失败，请检查网络连接")

    # --push: 自动 commit & push
    if "--push" in sys.argv and (ok1 or ok2):
        print()
        print("📤 推送缓存到 Git...")
        os.system("git add .cache/name_to_code.json .cache/board_industry.csv")
        os.system('git commit -m "chore: update EM data cache (name mapping + industry boards)"')
        os.system("git push")
        print("✅ 已推送")


if __name__ == "__main__":
    main()
