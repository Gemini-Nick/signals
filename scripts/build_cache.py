#!/usr/bin/env python3
"""
隆小侠 LONG CLAW — 本地缓存预生成

只缓存 AutoDL 云端网络受限 + 更新频率低（周级）的数据：
  1. 股票名称→代码映射 — 深交所 SSL 不稳定，周级变动
  2. 行业板块列表     — 东财 push2 偶发超时，基本不变
  3. 行业成分股       — 东财限流严重（90个请求），7天过期

不缓存（云端可实时拉）：行业K线、概念排行、指数日线

用法:
    python scripts/build_cache.py                          # 生成全部（mapping + board）
    python scripts/build_cache.py --only mapping           # 仅名称映射
    python scripts/build_cache.py --only board             # 仅行业列表
    python scripts/build_cache.py --only stocks 储能 光伏   # 按关键词筛选行业成分股
    python scripts/build_cache.py --only stocks all        # 全部行业成分股（慎用，497个）
    python scripts/build_cache.py --push                   # 生成 + git push

缓存目录:
    .cache/name_to_code.json       — 股票名称→代码映射 (~5000只)
    .cache/board_industry.csv      — 行业板块列表 (~90个)
    .data/cache/stocks_{name}.json — 行业成分股列表

上传到 AutoDL:
    scp -P <port> -r .cache/ .data/cache/ root@<host>:/root/autodl-tmp/signals/
"""
import json
import time
import sys
import os
import traceback
from datetime import datetime

# ── 环境设置 ──────────────────────────────────────────────
# 清除代理（Clash TUN 干扰）
for k in ["HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
          "http_proxy", "https_proxy", "all_proxy"]:
    os.environ.pop(k, None)

# SSL 降级 — 绕过 Clash TUN 模式下的 SSL EOF 错误
import ssl
import urllib3
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
# Monkey-patch requests 默认 session 不验证 SSL
import requests as _req
_original_get = _req.Session.get
_original_post = _req.Session.post
def _patched_get(self, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _original_get(self, url, **kwargs)
def _patched_post(self, url, **kwargs):
    kwargs.setdefault("verify", False)
    return _original_post(self, url, **kwargs)
_req.Session.get = _patched_get
_req.Session.post = _patched_post

# 降低 SSL 安全级别（兼容 TUN 代理的 MITM 证书）
try:
    ssl._create_default_https_context = ssl._create_unverified_context
except AttributeError:
    pass

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)
os.chdir(ROOT)

CACHE_DIR = os.path.join(ROOT, ".cache")
DATA_CACHE_DIR = os.path.join(ROOT, ".data", "cache")


# ── 日志工具 ──────────────────────────────────────────────
class Logger:
    """带时间戳、进度条、统计的日志"""

    def __init__(self):
        self.start_time = time.time()
        self.stats = {"success": 0, "fail": 0, "skip": 0}
        self._section_start = None

    def section(self, title: str):
        self._section_start = time.time()
        print(f"\n{'='*60}")
        print(f"  {title}")
        print(f"{'='*60}")

    def info(self, msg: str):
        print(f"  [{self._elapsed()}] {msg}")

    def progress(self, i: int, total: int, name: str, extra: str = ""):
        pct = i / total * 100
        bar = "█" * int(pct / 5) + "░" * (20 - int(pct / 5))
        suffix = f" — {extra}" if extra else ""
        print(f"  [{self._elapsed()}] [{bar}] {i}/{total} {name}{suffix}")

    def ok(self, msg: str):
        self.stats["success"] += 1
        print(f"         ✅ {msg}")

    def fail(self, msg: str, exc: bool = False):
        self.stats["fail"] += 1
        print(f"         ❌ {msg}")
        if exc:
            traceback.print_exc()

    def skip(self, msg: str):
        self.stats["skip"] += 1
        print(f"         ⏭️  {msg}")

    def section_done(self):
        if self._section_start:
            dt = time.time() - self._section_start
            print(f"  ⏱️  本节耗时: {dt:.1f}s")

    def summary(self):
        total = time.time() - self.start_time
        s = self.stats
        print(f"\n{'='*60}")
        print(f"  📊 汇总")
        print(f"{'='*60}")
        print(f"  ✅ 成功: {s['success']}")
        print(f"  ❌ 失败: {s['fail']}")
        print(f"  ⏭️  跳过: {s['skip']}")
        print(f"  ⏱️  总耗时: {total:.1f}s ({total/60:.1f}min)")
        if s["fail"] > 0:
            print(f"\n  ⚠️  有 {s['fail']} 项失败，请检查网络后重试")
        else:
            print(f"\n  🎉 全部成功!")

    def _elapsed(self) -> str:
        m, s = divmod(int(time.time() - self.start_time), 60)
        return f"{m:02d}:{s:02d}"


log = Logger()


# ── 缓存写入工具 ──────────────────────────────────────────
def save_json(directory: str, filename: str, data) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    size_kb = os.path.getsize(path) / 1024
    return f"{size_kb:.1f}KB"


def save_csv(directory: str, filename: str, df) -> str:
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, filename)
    df.to_csv(path, index=False, encoding="utf-8")
    size_kb = os.path.getsize(path) / 1024
    return f"{size_kb:.1f}KB"


# ── Task 1: 名称→代码映射 ────────────────────────────────
def task_name_mapping():
    import akshare as ak

    log.section("Task 1/3: 股票名称→代码映射")
    log.info("接口: akshare.stock_info_a_code_name()")

    try:
        df = ak.stock_info_a_code_name()
        if df is None or df.empty:
            log.fail("返回空数据")
            return

        mapping = {}
        for _, row in df.iterrows():
            name = str(row.get("name", "")).strip()
            code = str(row.get("code", "")).strip().zfill(6)
            if name and code and len(code) == 6:
                mapping[name] = code

        size = save_json(CACHE_DIR, "name_to_code.json", mapping)
        log.ok(f"{len(mapping)} 只股票 ({size})")
    except Exception as e:
        log.fail(f"{e.__class__.__name__}: {e}", exc=True)

    log.section_done()


# ── Task 2: 行业板块列表 ─────────────────────────────────
def task_board_industry():
    import akshare as ak

    log.section("Task 2/3: 行业板块列表")
    log.info("接口: akshare.stock_board_industry_name_em()")

    try:
        df = ak.stock_board_industry_name_em()
        if df is None or df.empty:
            log.fail("返回空数据")
            return None

        size = save_csv(CACHE_DIR, "board_industry.csv", df)
        log.ok(f"{len(df)} 个行业 ({size})")
        log.section_done()
        return df["板块名称"].tolist()
    except Exception as e:
        log.fail(f"{e.__class__.__name__}: {e}", exc=True)
        log.section_done()
        return None


# ── Task 3: 行业成分股 ───────────────────────────────────
def task_industry_stocks(industries: list):
    import akshare as ak

    log.section("Task 3/3: 行业成分股")
    log.info(f"匹配到 {len(industries)} 个行业")

    delay = 0.8  # 初始间隔
    consecutive_fails = 0

    for i, name in enumerate(industries, 1):
        log.progress(i, len(industries), name)

        # 检查已有缓存（7天内跳过）
        cache_path = os.path.join(DATA_CACHE_DIR, f"stocks_{name}.json")
        if os.path.exists(cache_path):
            age_days = (time.time() - os.path.getmtime(cache_path)) / 86400
            if age_days < 7:
                log.skip(f"缓存有效 ({age_days:.1f}天前)")
                continue

        # 带重试的请求（最多3次）
        df = None
        for attempt in range(3):
            try:
                df = ak.stock_board_industry_cons_em(symbol=name)
                consecutive_fails = 0
                delay = max(0.8, delay - 0.2)  # 成功后逐步恢复速度
                break
            except Exception as e:
                if attempt < 2:
                    wait = (attempt + 1) * 3  # 3s, 6s
                    log.info(f"  重试 {attempt+1}/2 (等待{wait}s): {e.__class__.__name__}")
                    time.sleep(wait)
                else:
                    consecutive_fails += 1
                    log.fail(f"{e.__class__.__name__}: {str(e)[:80]}", exc=True)

        if df is None or df.empty:
            if df is not None:
                log.skip("空数据")
            # 连续失败 >= 5 次，加大间隔
            if consecutive_fails >= 5:
                delay = min(5.0, delay + 1.0)
                log.info(f"⚡ 连续失败{consecutive_fails}次，间隔加至{delay:.1f}s")
            time.sleep(delay)
            continue

        # 找代码列
        code_col = None
        for col in ["代码", "code", "股票代码"]:
            if col in df.columns:
                code_col = col
                break
        if not code_col:
            log.fail(f"无代码列 (列: {list(df.columns)[:5]})")
            time.sleep(delay)
            continue

        # 转换为 SH./SZ. 格式
        codes = []
        for raw in df[code_col].astype(str):
            c = raw.strip()
            if len(c) == 6 and c.isdigit():
                prefix = "SH." if c.startswith(("6", "5")) else "SZ."
                codes.append(prefix + c)

        if codes:
            size = save_json(DATA_CACHE_DIR, f"stocks_{name}.json", codes)
            log.ok(f"{len(codes)} 只 ({size})")
        else:
            log.skip("无有效代码")

        time.sleep(delay)

    log.section_done()


# ── 主流程 ────────────────────────────────────────────────
def main():
    print()
    print("🐲 隆小侠 LONG CLAW — 本地缓存预生成")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"  缓存1: {CACHE_DIR}")
    print(f"  缓存2: {DATA_CACHE_DIR}")

    # 解析 --only 参数和关键词
    args = sys.argv[1:]
    # 过滤掉 --push
    args = [a for a in args if a != "--push"]

    only = None
    keywords = []
    if "--only" in args:
        idx = args.index("--only")
        remaining = args[idx + 1:]
        if remaining:
            only = remaining[0]
            keywords = remaining[1:]  # stocks 后面的都是关键词

    valid_only = {"mapping", "board", "stocks"}
    if only and only not in valid_only:
        print(f"\n  ❌ 未知任务: {only}")
        print(f"  可选: {', '.join(sorted(valid_only))}")
        sys.exit(1)

    os.makedirs(CACHE_DIR, exist_ok=True)
    os.makedirs(DATA_CACHE_DIR, exist_ok=True)

    # Task 1: 名称映射
    if not only or only == "mapping":
        task_name_mapping()
        time.sleep(1)

    # Task 2: 行业列表（Task 3 依赖）
    industries = None
    if not only or only in ("board", "stocks"):
        industries = task_board_industry()
        time.sleep(1)

    # Task 3: 行业成分股（需要关键词或 all）
    if not only or only == "stocks":
        if only == "stocks" and not keywords:
            print(f"\n  ❌ 请指定关键词，例如:")
            print(f"     python scripts/build_cache.py --only stocks 储能 光伏")
            print(f"     python scripts/build_cache.py --only stocks all  # 全部（慎用）")
            sys.exit(1)

        # 加载行业列表
        if industries is None:
            csv_path = os.path.join(CACHE_DIR, "board_industry.csv")
            if os.path.exists(csv_path):
                import pandas as pd
                df = pd.read_csv(csv_path)
                industries = df["板块名称"].tolist()
                log.info(f"从缓存加载 {len(industries)} 个行业")
            else:
                log.fail("无行业列表，请先运行 --only board")

        if industries:
            # 关键词筛选
            if keywords and keywords != ["all"]:
                matched = [name for name in industries
                           if any(kw in name for kw in keywords)]
                if not matched:
                    print(f"\n  ❌ 没有匹配「{'、'.join(keywords)}」的行业")
                    print(f"  💡 试试更短的关键词，或用以下命令查看所有行业:")
                    print(f"     head -20 .cache/board_industry.csv")
                    sys.exit(1)
                log.info(f"关键词「{'、'.join(keywords)}」匹配到 {len(matched)} 个行业:")
                for name in matched:
                    log.info(f"  → {name}")
                task_industry_stocks(matched)
            elif keywords == ["all"]:
                log.info(f"⚠️  全量模式: {len(industries)} 个行业")
                task_industry_stocks(industries)
            # 非 --only stocks 模式（全量 build）不跑成分股
            # 全量 build 只跑 mapping + board

    # 汇总
    log.summary()

    # --push: 自动 git commit & push
    if "--push" in sys.argv:
        print(f"\n📤 推送缓存到 Git...")
        os.system("git add .cache/ .data/cache/stocks_*.json")
        os.system('git commit -m "chore: update local cache (build_cache.py)"')
        os.system("git push")
        print("✅ 已推送")

    # 上传提示
    print(f"\n📦 上传到 AutoDL:")
    print(f"  scp -P <port> -r .cache/ .data/cache/ root@<host>:/root/autodl-tmp/signals/")


if __name__ == "__main__":
    main()
