# -*- coding: utf-8 -*-
"""
纯文本渲染器（管道输出降级）
当输出重定向到文件/日志时使用
"""
import time
from datetime import datetime

from .models import DashboardState, PHASE_ORDER


def _fmt_sec(sec: float) -> str:
    if sec < 0.01:
        return "--"
    if sec < 0.1:
        return "<0.1s"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m{int(sec % 60)}s"


class PlainRenderer:
    """管道输出降级渲染器：只在状态变更时打印单行"""

    def __init__(self, state: DashboardState):
        self._state = state
        self._last_phase = ""
        self._last_done = 0

    def start(self):
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")
        print(f"[{now_str}] 🐲 隆小侠 — {self._state.mode} 模式", flush=True)

    def stop(self):
        pass

    def refresh(self):
        """只在阶段切换或进度变化时打印"""
        for phase_id, display in PHASE_ORDER:
            ps = self._state.phases.get(phase_id)
            if ps is None:
                continue
            if ps.status == "running":
                if phase_id != self._last_phase:
                    self._last_phase = phase_id
                    self._last_done = 0
                    ts = datetime.now().strftime("%H:%M:%S")
                    total_str = f"/{ps.total}" if ps.total > 0 else ""
                    print(f"[{ts}] >>> {display}{total_str} ...", flush=True)
                elif ps.done > self._last_done:
                    self._last_done = ps.done
                    if ps.total > 0 and (ps.done % 10 == 0 or ps.done == ps.total):
                        ts = datetime.now().strftime("%H:%M:%S")
                        print(f"[{ts}]   {display} {ps.done}/{ps.total}"
                              f" ({ps.progress_pct}%)", flush=True)
            elif ps.status == "done" and phase_id == self._last_phase:
                ts = datetime.now().strftime("%H:%M:%S")
                detail = f"  {ps.detail}" if ps.detail else ""
                print(f"[{ts}] ✅ {display} {_fmt_sec(ps.elapsed)}{detail}", flush=True)
                self._last_phase = ""

    def log(self, msg: str):
        print(msg, flush=True)

    def detail(self, msg: str):
        print(msg, flush=True)

    def print_summary(self, state: DashboardState, eta_estimator=None):
        """纯文本汇总"""
        total = state.total_elapsed
        print(f"\n{'=' * 50}", flush=True)
        print(f"  运行汇总  ({_fmt_sec(total)})", flush=True)
        print(f"{'─' * 50}", flush=True)
        for phase_id, display in PHASE_ORDER:
            ps = state.phases.get(phase_id)
            if ps is None:
                continue
            if ps.status == "done":
                print(f"  {display:<18s} {_fmt_sec(ps.elapsed):>8s}"
                      f"  {ps.detail}", flush=True)
            elif ps.status == "skipped":
                print(f"  {display:<18s} {'跳过':>8s}", flush=True)
        print(f"  {'总计':<18s} {_fmt_sec(total):>8s}", flush=True)
        if state.total_errors:
            print(f"  错误: {state.total_errors} 个", flush=True)
        print(f"{'=' * 50}\n", flush=True)
