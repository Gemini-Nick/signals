# -*- coding: utf-8 -*-
"""
实验日志 — TSV 格式记录每轮实验结果。

类似 autoresearch 的 results.tsv，但包含更丰富的参数变异信息。
"""
import os
from datetime import datetime
from typing import List, Optional


_HEADER = (
    "id\ttimestamp\tmutation_type\tparam_group\tparam_name\t"
    "old_value\tnew_value\tfitness_before\tfitness_after\t"
    "delta\tdecision\tgit_hash\n"
)


class ExperimentLog:
    """追加式 TSV 实验日志。"""

    def __init__(self, log_path: str):
        self._path = log_path
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        if not os.path.exists(log_path):
            with open(log_path, "w") as f:
                f.write(_HEADER)

    def next_id(self) -> int:
        """下一个实验 ID（基于已有行数）。"""
        try:
            with open(self._path) as f:
                return sum(1 for _ in f)  # 含 header 行，所以 id 从 1 开始
        except FileNotFoundError:
            return 1

    def append(
        self,
        experiment_id: int,
        mutation_type: str,
        param_group: str,
        param_name: str,
        old_value: str,
        new_value: str,
        fitness_before: Optional[float],
        fitness_after: Optional[float],
        decision: str,
        git_hash: str = "",
    ):
        """追加一行实验记录。"""
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fb = f"{fitness_before:.2f}" if fitness_before is not None else "N/A"
        fa = f"{fitness_after:.2f}" if fitness_after is not None else "N/A"
        if fitness_before is not None and fitness_after is not None:
            delta = f"{fitness_after - fitness_before:+.2f}"
        else:
            delta = "N/A"

        row = (
            f"{experiment_id}\t{ts}\t{mutation_type}\t{param_group}\t{param_name}\t"
            f"{old_value}\t{new_value}\t{fb}\t{fa}\t"
            f"{delta}\t{decision}\t{git_hash}\n"
        )
        with open(self._path, "a") as f:
            f.write(row)

    def load_history(self) -> List[dict]:
        """读取所有历史实验记录。"""
        if not os.path.exists(self._path):
            return []
        rows = []
        with open(self._path) as f:
            header = f.readline().strip().split("\t")
            for line in f:
                parts = line.strip().split("\t")
                if len(parts) == len(header):
                    rows.append(dict(zip(header, parts)))
        return rows

    def summary(self) -> dict:
        """快速统计摘要。"""
        history = self.load_history()
        if not history:
            return {"total": 0, "kept": 0, "reverted": 0, "best_delta": "N/A"}
        kept = sum(1 for r in history if r.get("decision") == "KEEP")
        reverted = sum(1 for r in history if r.get("decision") == "REVERT")
        deltas = []
        for r in history:
            try:
                deltas.append(float(r.get("delta", "0")))
            except ValueError:
                pass
        best_delta = f"{max(deltas):+.2f}" if deltas else "N/A"
        return {
            "total": len(history),
            "kept": kept,
            "reverted": reverted,
            "best_delta": best_delta,
        }
