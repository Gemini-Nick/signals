# -*- coding: utf-8 -*-
"""
ETA 预估器：自适应移动平均 + 持久化历史耗时
"""
import json
import time
from pathlib import Path
from typing import Dict, Optional

from .models import DashboardState, PHASE_ORDER

CACHE_PATH = Path(__file__).resolve().parent.parent.parent / ".cache" / "dashboard_timings.json"

# 默认阶段耗时估计（秒）——首次运行使用
DEFAULTS: Dict[str, float] = {
    "research":      3.0,
    "L1.ak_load":   12.0,
    "L1.futu":       8.0,
    "L1.us":        10.0,
    "L1.analyze":    5.0,
    "L2.ranking":   35.0,
    "L2.supplement":  5.0,
    "L3.init":      30.0,
    "L3.scan":       3.0,
    "feishu":        2.0,
}


class ETAEstimator:
    """基于指数移动平均的 ETA 预估器"""

    def __init__(self, alpha: float = 0.3):
        self._alpha = alpha
        self._history = self._load()

    def estimate_phase(self, phase: str) -> float:
        """单阶段预估耗时"""
        return self._history.get(phase, DEFAULTS.get(phase, 10.0))

    def estimate_remaining(self, state: DashboardState) -> float:
        """从当前状态预估总剩余时间"""
        remaining = 0.0
        for phase_id, _ in PHASE_ORDER:
            ps = state.phases.get(phase_id)
            if ps is None or ps.status == "pending":
                remaining += self.estimate_phase(phase_id)
            elif ps.status == "skipped" or ps.status == "done":
                continue
            elif ps.status == "running":
                elapsed = ps.elapsed
                if ps.total > 0 and ps.done > 0:
                    per_task = elapsed / ps.done
                    remaining += per_task * (ps.total - ps.done)
                else:
                    est = self.estimate_phase(phase_id)
                    remaining += max(0, est - elapsed)
        return remaining

    def record(self, phase: str, duration: float):
        """记录实际耗时，更新移动平均"""
        if phase in self._history:
            self._history[phase] = (
                self._alpha * duration + (1 - self._alpha) * self._history[phase]
            )
        else:
            self._history[phase] = duration

    def save(self):
        """持久化到磁盘"""
        try:
            CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CACHE_PATH.write_text(json.dumps(self._history, indent=2))
        except Exception:
            pass  # 写入失败不影响运行

    def _load(self) -> Dict[str, float]:
        try:
            if CACHE_PATH.exists():
                data = json.loads(CACHE_PATH.read_text())
                if isinstance(data, dict):
                    return {k: float(v) for k, v in data.items()}
        except Exception:
            pass
        return {}
