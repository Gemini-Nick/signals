# -*- coding: utf-8 -*-
"""
AutoResearch Agent — 策略参数自主研究循环控制器。

借鉴 Karpathy autoresearch 的 NEVER STOP 循环:
  SNAPSHOT → MUTATE → EVALUATE → DECIDE → LOG → REPEAT

每轮实验只需秒级（重算评分，不拉数据），一小时可跑数百轮。
"""
import subprocess
import time
from datetime import datetime
from typing import Optional

import config
from signals.core.backtest import SignalJournal
from .evaluator import compute_fitness, compute_fitness_detail
from .experiment_log import ExperimentLog
from .mutator import ParameterSpace, Mutation


def _git_hash() -> str:
    """获取当前 git short hash。"""
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "--short", "HEAD"],
            stderr=subprocess.DEVNULL,
        ).decode().strip()
    except Exception:
        return "unknown"


def _git_commit(message: str) -> bool:
    """提交 scorer.py + config.py 的变更。"""
    try:
        subprocess.run(
            ["git", "add",
             "signals/core/scorer.py",
             "config.py"],
            check=True, capture_output=True,
        )
        subprocess.run(
            ["git", "commit", "-m", message],
            check=True, capture_output=True,
        )
        return True
    except subprocess.CalledProcessError:
        return False


def _git_revert_files():
    """回退 scorer.py + config.py 到上次提交。"""
    try:
        subprocess.run(
            ["git", "checkout", "HEAD", "--",
             "signals/core/scorer.py",
             "config.py"],
            check=True, capture_output=True,
        )
    except subprocess.CalledProcessError:
        pass


class AutoResearchAgent:
    """
    自主研究代理。

    使用:
        agent = AutoResearchAgent()
        agent.run_n(10)            # 跑10轮
        agent.run_forever()        # 永不停止
    """

    def __init__(self, dry_run: bool = False, min_samples: int = 0):
        self._dry_run = dry_run
        self._min_samples = min_samples or getattr(
            config, 'AUTORESEARCH_MIN_SAMPLES', 20)
        log_path = getattr(
            config, 'AUTORESEARCH_LOG',
            '.data/autoresearch/experiments.tsv')
        self._log = ExperimentLog(log_path)
        self._space = ParameterSpace()
        self._journal = SignalJournal()
        self._experiment_count = 0
        self._kept_count = 0

    def run_n(self, n: int):
        """运行 N 轮实验。"""
        self._print_header()
        baseline = self._compute_baseline()
        if baseline is None:
            return

        for i in range(n):
            self._run_one_experiment(i + 1, n)

        self._print_summary()

    def run_forever(self):
        """永不停止循环（autoresearch 模式）。"""
        self._print_header()
        baseline = self._compute_baseline()
        if baseline is None:
            return

        i = 0
        try:
            while True:
                i += 1
                self._run_one_experiment(i)
                # 每 50 轮输出中间摘要
                if i % 50 == 0:
                    self._print_summary()
        except KeyboardInterrupt:
            print(f"\n  [中断] 收到 Ctrl+C，已完成 {i} 轮实验")
            self._print_summary()

    def _compute_baseline(self) -> Optional[float]:
        """计算并输出基线 fitness。"""
        detail = compute_fitness_detail(
            self._journal, min_samples=self._min_samples)
        if detail is None:
            summary = self._journal.summary()
            print(f"\n  [错误] 样本不足，无法计算 fitness。")
            print(f"  数据库: 总计 {summary['total']} 条 | "
                  f"已评估 {summary['evaluated']} | "
                  f"最少需要 {self._min_samples} 条已评估信号")
            print(f"  请先运行 `python run.py --mode review` + "
                  f"`python run.py --mode backtest` 积累数据。")
            return None

        print(f"\n  基线 Fitness: {detail['fitness']:.2f}")
        print(f"    加权SQS: {detail['weighted_sqs']:.1f}  "
              f"平均SQS: {detail['avg_sqs']:.1f}  "
              f"胜率: {detail['win_rate']:.1f}%  "
              f"PF: {detail['profit_factor']:.2f}  "
              f"期望值: {detail['expectancy']:+.2f}%  "
              f"样本: {detail['sample_count']}")
        print(f"  参数空间: {self._space.param_count} 个可变参数，"
              f"{len(self._space.group_names)} 组 ({', '.join(self._space.group_names)})")
        if self._dry_run:
            print(f"  [DRY RUN] 预览模式，不会实际修改参数")
        print(f"{'─' * 70}")
        return detail['fitness']

    def _run_one_experiment(self, idx: int, total: Optional[int] = None):
        """运行单轮实验。"""
        t0 = time.time()
        self._experiment_count += 1

        # 1. 快照
        self._space.snapshot()
        fitness_before = compute_fitness(
            self._journal, min_samples=self._min_samples)

        # 2. 变异
        mutation = self._space.mutate()

        # 3. 验证约束
        valid, reason = self._space.validate()
        if not valid:
            # 约束违反，直接回退
            self._space.revert()
            total_str = f"/{total}" if total else ""
            print(f"  [{idx}{total_str}] SKIP  {mutation.param_name}  "
                  f"约束违反: {reason}")
            return

        # 4. 评估
        fitness_after = compute_fitness(
            self._journal, min_samples=self._min_samples)

        # 5. 决策
        if fitness_before is None or fitness_after is None:
            decision = "SKIP"
            self._space.revert()
        elif fitness_after > fitness_before:
            decision = "KEEP"
            self._kept_count += 1
            if not self._dry_run:
                _git_commit(
                    f"autoresearch: {mutation.param_name} "
                    f"fitness {fitness_before:.2f} -> {fitness_after:.2f}")
        else:
            decision = "REVERT"
            self._space.revert()

        # 6. 日志
        exp_id = self._log.next_id()
        self._log.append(
            experiment_id=exp_id,
            mutation_type=mutation.mutation_type,
            param_group=mutation.param_group,
            param_name=mutation.param_name,
            old_value=str(mutation.old_value),
            new_value=str(mutation.new_value),
            fitness_before=fitness_before,
            fitness_after=fitness_after,
            decision=decision,
            git_hash=_git_hash() if decision == "KEEP" else "",
        )

        # 7. 输出
        elapsed = time.time() - t0
        total_str = f"/{total}" if total else ""
        delta = ""
        if fitness_before is not None and fitness_after is not None:
            d = fitness_after - fitness_before
            delta = f"  delta={d:+.4f}"
        tag = {"KEEP": "+++", "REVERT": "---", "SKIP": "~~~"}.get(decision, "???")
        print(f"  [{idx}{total_str}] {tag}  {mutation.param_name}  "
              f"{mutation.old_value} -> {mutation.new_value}{delta}  "
              f"({elapsed:.1f}s)")

    def _print_header(self):
        """输出运行头部信息。"""
        print(f"\n{'═' * 70}")
        print(f"  🐲 AutoResearch — 策略参数自主研究引擎")
        print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{'═' * 70}")

    def _print_summary(self):
        """输出实验摘要。"""
        log_summary = self._log.summary()
        print(f"\n{'─' * 70}")
        print(f"  实验摘要:")
        print(f"    本次运行: {self._experiment_count} 轮  "
              f"保留: {self._kept_count}  "
              f"回退: {self._experiment_count - self._kept_count}")
        print(f"    历史总计: {log_summary['total']} 轮  "
              f"保留: {log_summary['kept']}  "
              f"回退: {log_summary['reverted']}  "
              f"最佳提升: {log_summary['best_delta']}")

        # 当前 fitness
        detail = compute_fitness_detail(
            self._journal, min_samples=self._min_samples)
        if detail:
            print(f"    当前 Fitness: {detail['fitness']:.2f}  "
                  f"(wSQS={detail['weighted_sqs']:.1f}  "
                  f"WR={detail['win_rate']:.1f}%  "
                  f"PF={detail['profit_factor']:.2f}  "
                  f"Exp={detail['expectancy']:+.2f}%)")
        print(f"{'─' * 70}")

    def close(self):
        """清理资源。"""
        self._journal.close()
