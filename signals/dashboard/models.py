# -*- coding: utf-8 -*-
"""
Dashboard 数据模型：阶段状态、全局状态、阶段定义
"""
from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
import time


# ─────────────────────────────────────────────
# 阶段顺序定义（id, 显示名称）
# ─────────────────────────────────────────────

PHASE_ORDER: List[Tuple[str, str]] = [
    ("research",      "研报加载"),
    ("L1.ak_load",    "L1 A股指数"),
    ("L1.futu",       "L1 港股指数"),
    ("L1.us",         "L1 美股指数"),
    ("L1.analyze",    "L1 CZSC分析"),
    ("L2.ranking",    "L2 行业筛选"),
    ("L2.supplement", "L2 补充行业"),
    ("L3.init",       "L3 数据加载"),
    ("L3.scan",       "L3 信号检测"),
    ("feishu",        "飞书推送"),
]

# 阶段 id → 显示名称 快速查找
PHASE_DISPLAY: Dict[str, str] = dict(PHASE_ORDER)


@dataclass
class PhaseState:
    """单个阶段的运行状态"""
    name: str
    display_name: str
    total: int = 0
    done: int = 0
    errors: int = 0
    skipped: int = 0
    start_time: float = 0.0
    end_time: float = 0.0
    status: str = "pending"       # pending / running / done / skipped
    detail: str = ""              # 阶段完成时的摘要
    active_task: str = ""         # 当前正在执行的子任务描述
    error_details: List[str] = field(default_factory=list)  # 最近错误

    @property
    def elapsed(self) -> float:
        if self.start_time == 0:
            return 0.0
        end = self.end_time if self.end_time > 0 else time.time()
        return end - self.start_time

    @property
    def progress_pct(self) -> int:
        if self.total <= 0:
            return 0
        return min(100, int(self.done * 100 / self.total))


@dataclass
class DashboardState:
    """全局面板状态"""
    mode: str = "intraday"
    global_start: float = 0.0
    phases: Dict[str, PhaseState] = field(default_factory=dict)
    degradations: List[str] = field(default_factory=list)
    error_log: List[str] = field(default_factory=list)
    is_done: bool = False
    # MarketContext 摘要（L1完成后填充）
    market_direction: str = ""
    market_style: str = ""
    l2_count: int = 0
    l3_count: int = 0

    @property
    def total_elapsed(self) -> float:
        if self.global_start == 0:
            return 0.0
        return time.time() - self.global_start

    @property
    def total_errors(self) -> int:
        return sum(p.errors for p in self.phases.values())
