# -*- coding: utf-8 -*-
"""
实验日志 — MongoDB 优先 + TSV 降级

MongoDB: 跨设备共享实验历史，支持自进化系统跨 session 知识积累
TSV:     无 MongoDB 时降级到本地 TSV 文件
"""
import logging
import os
from datetime import datetime
from typing import List, Optional

_log = logging.getLogger("signals.autoresearch.experiment_log")

_HEADER = (
    "id\ttimestamp\tmutation_type\tparam_group\tparam_name\t"
    "old_value\tnew_value\tfitness_before\tfitness_after\t"
    "delta\tdecision\tgit_hash\n"
)


def _try_mongo_experiments():
    """尝试获取 MongoDB experiments collection。"""
    try:
        import config
        if not getattr(config, "DB_ENABLED", False):
            return None
        from signals.sync.db import get_db
        db = get_db()
        db.command("ping")
        return db["experiments"]
    except Exception:
        return None


class ExperimentLog:
    """追加式实验日志（MongoDB 优先，TSV 降级）。"""

    def __init__(self, log_path: str):
        # 尝试 MongoDB
        self._mongo = _try_mongo_experiments()
        self._use_mongo = self._mongo is not None

        # TSV 降级（总是初始化）
        self._path = log_path
        os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
        if not os.path.exists(log_path):
            with open(log_path, "w") as f:
                f.write(_HEADER)

        backend = "MongoDB" if self._use_mongo else "TSV"
        _log.info(f"ExperimentLog 后端: {backend}")

    def next_id(self) -> int:
        """下一个实验 ID。"""
        if self._use_mongo:
            return self._mongo.count_documents({}) + 1
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

        if fitness_before is not None and fitness_after is not None:
            delta = fitness_after - fitness_before
        else:
            delta = None

        if self._use_mongo:
            doc = {
                "experiment_id": experiment_id,
                "timestamp": ts,
                "mutation_type": mutation_type,
                "param_group": param_group,
                "param_name": param_name,
                "old_value": old_value,
                "new_value": new_value,
                "fitness_before": fitness_before,
                "fitness_after": fitness_after,
                "delta": delta,
                "decision": decision,
                "git_hash": git_hash,
            }
            try:
                self._mongo.insert_one(doc)
                return
            except Exception as e:
                _log.warning(f"MongoDB 写入失败，降级 TSV: {e}")

        # TSV 降级
        fb = f"{fitness_before:.2f}" if fitness_before is not None else "N/A"
        fa = f"{fitness_after:.2f}" if fitness_after is not None else "N/A"
        delta_str = f"{delta:+.2f}" if delta is not None else "N/A"

        row = (
            f"{experiment_id}\t{ts}\t{mutation_type}\t{param_group}\t{param_name}\t"
            f"{old_value}\t{new_value}\t{fb}\t{fa}\t"
            f"{delta_str}\t{decision}\t{git_hash}\n"
        )
        with open(self._path, "a") as f:
            f.write(row)

    def load_history(self) -> List[dict]:
        """读取所有历史实验记录。"""
        if self._use_mongo:
            return list(self._mongo.find({}, {"_id": 0}).sort("timestamp", 1))

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
                d = r.get("delta", "0")
                deltas.append(float(d) if d != "N/A" else 0)
            except (ValueError, TypeError):
                pass
        best_delta = f"{max(deltas):+.2f}" if deltas else "N/A"
        return {
            "total": len(history),
            "kept": kept,
            "reverted": reverted,
            "best_delta": best_delta,
        }
