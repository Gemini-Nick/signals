# -*- coding: utf-8 -*-
"""
数据同步引擎 — 调度器 + CLI 入口

借鉴 Akshare-Sync 的 ProcessPoolExecutor 模式，
使用 ThreadPoolExecutor（I/O 密集无需多进程）。

用法:
    # 一次性执行所有模块
    python -m signals.sync.engine --once

    # 只执行指定模块
    python -m signals.sync.engine --once --module index_daily

    # 常驻调度模式（cron 外的备选方案）
    python -m signals.sync.engine --daemon
"""
import argparse
import atexit
import logging
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime

from .db import get_db, close as close_db

logger = logging.getLogger("signals.sync")


class SyncEngine:
    """
    数据同步调度器。

    管理 6 个同步模块的执行：
    - stock_daily:   ~5000 A股日线（增量）
    - index_daily:   11 只指数日线（全量）
    - stock_minute:  活跃标的 30M/15M（增量）
    - index_minute:  11 只指数 30M/15M
    - board_ranking: 行业排行快照
    - board_cons:    行业成分股（周日全量）
    """

    def __init__(self, mongo_url: str = None, proxy_url: str = None,
                 max_workers: int = 4):
        self.db = get_db(mongo_url)
        self.proxy_url = proxy_url
        self.max_workers = max_workers
        atexit.register(close_db)

        # 延迟导入避免循环
        from .modules import ALL_MODULES
        self.modules = ALL_MODULES

    def run_module(self, name: str, module_fn) -> dict:
        """
        执行单个同步模块，返回执行结果。
        """
        sync_log = self.db["sync_log"]
        start_time = datetime.now()

        # 标记开始
        sync_log.update_one(
            {"_id": f"{name}:_meta"},
            {"$set": {
                "module": name,
                "status": "running",
                "last_run": start_time,
            }},
            upsert=True,
        )

        try:
            result = module_fn(self.db, proxy_url=self.proxy_url)
            elapsed = (datetime.now() - start_time).total_seconds()

            # 标记完成
            sync_log.update_one(
                {"_id": f"{name}:_meta"},
                {"$set": {
                    "status": "ok",
                    "last_run": datetime.now(),
                    "elapsed_seconds": elapsed,
                    "error_msg": None,
                }},
            )

            logger.info(f"✓ {name} 完成 ({elapsed:.1f}s)")
            return {"module": name, "status": "ok", "elapsed": elapsed,
                    "result": result}

        except Exception as e:
            elapsed = (datetime.now() - start_time).total_seconds()

            sync_log.update_one(
                {"_id": f"{name}:_meta"},
                {"$set": {
                    "status": "error",
                    "last_run": datetime.now(),
                    "elapsed_seconds": elapsed,
                    "error_msg": str(e)[:500],
                }},
            )

            logger.error(f"✗ {name} 失败 ({elapsed:.1f}s): {e}")
            return {"module": name, "status": "error", "elapsed": elapsed,
                    "error": str(e)}

    def run_all(self) -> list:
        """
        一次性执行所有模块（--once 模式）。

        使用 ThreadPoolExecutor 并行执行，max_workers 控制并发。
        """
        logger.info(f"🔄 开始全量同步 ({len(self.modules)} 模块, "
                     f"{self.max_workers} 并发)")
        results = []

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            futures = {
                executor.submit(self.run_module, name, fn): name
                for name, fn, _ in self.modules
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    result = future.result()
                    results.append(result)
                except Exception as e:
                    logger.error(f"✗ {name} 未预期错误: {e}")
                    results.append({
                        "module": name, "status": "error", "error": str(e)
                    })

        ok = sum(1 for r in results if r["status"] == "ok")
        logger.info(f"🏁 同步完成: {ok}/{len(results)} 成功")
        return results

    def run_one(self, module_name: str) -> dict:
        """执行指定模块"""
        for name, fn, _ in self.modules:
            if name == module_name:
                return self.run_module(name, fn)
        raise ValueError(f"未知模块: {module_name}，"
                         f"可选: {[m[0] for m in self.modules]}")

    def run_daemon(self, check_interval: int = 60):
        """
        常驻调度模式（--daemon）。

        每分钟检查一次，在预设时间点触发对应模块。
        生产环境建议用 cron 替代。
        """
        logger.info("🐲 同步守护进程启动")
        last_run_date = {}

        while True:
            now = datetime.now()
            today = now.strftime("%Y-%m-%d")
            weekday = now.weekday()  # 0=Monday, 6=Sunday
            hhmm = now.strftime("%H:%M")

            for name, fn, schedule in self.modules:
                run_key = f"{name}:{today}"
                if run_key in last_run_date:
                    continue

                should_run = False

                if "weekday" in schedule and weekday < 5:
                    sched_time = schedule.split()[0]
                    if hhmm == sched_time:
                        should_run = True

                if "Sunday" in schedule and weekday == 6:
                    sched_time = schedule.split()[-1]
                    if hhmm == sched_time:
                        should_run = True

                if should_run:
                    logger.info(f"⏰ 触发 {name} (schedule: {schedule})")
                    self.run_module(name, fn)
                    last_run_date[run_key] = now

            time.sleep(check_interval)


def _setup_logging(level: str = "INFO"):
    """配置日志"""
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def main():
    parser = argparse.ArgumentParser(description="隆小侠数据同步引擎")
    parser.add_argument("--once", action="store_true",
                        help="一次性执行所有模块")
    parser.add_argument("--daemon", action="store_true",
                        help="常驻调度模式")
    parser.add_argument("--module", type=str, default=None,
                        help="只执行指定模块（配合 --once）")
    parser.add_argument("--workers", type=int, default=4,
                        help="并行工人数（默认 4）")
    parser.add_argument("--log-level", type=str, default="INFO",
                        help="日志级别")
    args = parser.parse_args()

    _setup_logging(args.log_level)

    import config
    if not config.MONGO_URL:
        logger.error("MONGO_URL 未配置，请设置环境变量 MONGO_URL")
        sys.exit(1)

    engine = SyncEngine(
        mongo_url=config.MONGO_URL,
        proxy_url=config.EM_PROXY_URL if config.EM_PROXY_ENABLED else None,
        max_workers=args.workers,
    )

    if args.once:
        if args.module:
            result = engine.run_one(args.module)
            print(f"\n{result}")
        else:
            results = engine.run_all()
            for r in results:
                status = "✓" if r["status"] == "ok" else "✗"
                print(f"  {status} {r['module']}: {r.get('elapsed', 0):.1f}s")
    elif args.daemon:
        engine.run_daemon()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
