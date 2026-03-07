# -*- coding: utf-8 -*-
"""
🐲 实时监控面板
用法：
    from signals.dashboard import Dashboard, get_dashboard

    # 创建（run.py 入口）
    dash = Dashboard(mode="intraday")

    # 各模块中获取
    dash = get_dashboard()
    if dash:
        dash.phase_start("L1.ak_load", total=7)
        dash.task_done("L1.ak_load", "上证50", "113根日线")
        dash.phase_end("L1.ak_load")
"""
import sys
import time
import threading
from typing import Optional

from .models import DashboardState, PhaseState, PHASE_DISPLAY
from .estimator import ETAEstimator

_dashboard: Optional["Dashboard"] = None


def get_dashboard() -> Optional["Dashboard"]:
    """获取全局 Dashboard 实例（未创建时返回 None）"""
    return _dashboard


class Dashboard:
    """
    线程安全的实时监控面板。

    - TTY 模式：rich.Live 底部固定面板 + 上方滚动日志
    - 管道模式：纯文本降级输出
    """

    def __init__(self, mode: str = "intraday"):
        global _dashboard

        self._state = DashboardState(mode=mode, global_start=time.time())
        self._lock = threading.Lock()
        self._estimator = ETAEstimator()
        self._paused = False

        is_tty = sys.stdout.isatty()
        if is_tty:
            try:
                from .renderer import RichRenderer
                self._renderer = RichRenderer(self._state)
                if not self._renderer.start():
                    raise RuntimeError("Rich start failed")
            except Exception as e:
                print(f"[Dashboard] Rich 面板启动失败，降级为文本模式: {e}",
                      file=sys.stderr, flush=True)
                from .fallback import PlainRenderer
                self._renderer = PlainRenderer(self._state)
                self._renderer.start()
        else:
            from .fallback import PlainRenderer
            self._renderer = PlainRenderer(self._state)
            self._renderer.start()

        _dashboard = self

    # ─────────────────────────────────────────
    # 阶段生命周期
    # ─────────────────────────────────────────

    def phase_start(self, phase: str, total: int = 0, display: str = ""):
        """标记阶段开始"""
        with self._lock:
            display_name = display or PHASE_DISPLAY.get(phase, phase)
            ps = PhaseState(
                name=phase,
                display_name=display_name,
                total=total,
                start_time=time.time(),
                status="running",
            )
            self._state.phases[phase] = ps
        self._refresh()

    def phase_end(self, phase: str, detail: str = ""):
        """标记阶段完成"""
        with self._lock:
            ps = self._state.phases.get(phase)
            if ps:
                ps.end_time = time.time()
                ps.status = "done"
                ps.active_task = ""
                if detail:
                    ps.detail = detail
                self._estimator.record(phase, ps.elapsed)
        self._refresh()

    def phase_skip(self, phase: str, reason: str = ""):
        """标记阶段跳过"""
        with self._lock:
            display_name = PHASE_DISPLAY.get(phase, phase)
            ps = PhaseState(
                name=phase,
                display_name=display_name,
                status="skipped",
                detail=reason,
            )
            self._state.phases[phase] = ps
        self._refresh()

    # ─────────────────────────────────────────
    # 任务级事件
    # ─────────────────────────────────────────

    def task_start(self, phase: str, task: str):
        """标记子任务开始（更新当前任务名）"""
        with self._lock:
            ps = self._state.phases.get(phase)
            if ps:
                ps.active_task = task
        self._refresh()

    def task_done(self, phase: str, task: str = "", detail: str = ""):
        """标记子任务完成"""
        with self._lock:
            ps = self._state.phases.get(phase)
            if ps:
                ps.done += 1
                ps.active_task = ""
        self._refresh()

    def task_error(self, phase: str, task: str, error: str):
        """记录子任务错误"""
        with self._lock:
            ps = self._state.phases.get(phase)
            if ps:
                ps.errors += 1
                ps.done += 1
                ps.active_task = ""
                short_err = f"[{task}] {error}"[:80]
                ps.error_details.append(short_err)
                ps.error_details = ps.error_details[-5:]
                self._state.error_log.append(short_err)
                self._state.error_log = self._state.error_log[-10:]
        self._refresh()

    def task_skip(self, phase: str, task: str, reason: str = ""):
        """标记子任务跳过"""
        with self._lock:
            ps = self._state.phases.get(phase)
            if ps:
                ps.skipped += 1
                ps.done += 1
                ps.active_task = ""
        self._refresh()

    # ─────────────────────────────────────────
    # 数据源降级
    # ─────────────────────────────────────────

    def degradation(self, source: str, target: str, reason: str = ""):
        """记录数据源降级事件"""
        msg = f"{source} → {target}"
        if reason:
            msg += f" ({reason})"
        with self._lock:
            self._state.degradations.append(msg)
        self._refresh()

    # ─────────────────────────────────────────
    # 全局状态
    # ─────────────────────────────────────────

    def set_context(self, direction: str = "", style: str = ""):
        """设置 MarketContext 摘要"""
        with self._lock:
            self._state.market_direction = direction
            self._state.market_style = style

    def set_l2_count(self, n: int):
        with self._lock:
            self._state.l2_count = n

    def set_l3_count(self, n: int):
        with self._lock:
            self._state.l3_count = n

    # ─────────────────────────────────────────
    # 日志输出（面板上方滚动）
    # ─────────────────────────────────────────

    def log(self, msg: str):
        """线程安全地在面板上方打印滚动日志（始终显示）"""
        self._renderer.log(msg)

    def detail(self, msg: str):
        """任务级详情 — Rich 模式静默（面板已展示进度），PlainRenderer 正常打印"""
        self._renderer.detail(msg)

    # ─────────────────────────────────────────
    # 暂停/恢复（用于 input() 交互）
    # ─────────────────────────────────────────

    def pause(self):
        """暂停面板渲染（用于 input() 交互时段）"""
        if not self._paused:
            self._renderer.stop()
            self._paused = True

    def resume(self):
        """恢复面板渲染"""
        if self._paused:
            self._renderer.start()
            self._paused = False

    # ─────────────────────────────────────────
    # 结束
    # ─────────────────────────────────────────

    def finish(self):
        """停止面板，输出最终汇总"""
        with self._lock:
            self._state.is_done = True
        if not self._paused:
            self._renderer.stop()
        self._estimator.save()
        self._renderer.print_summary(self._state, self._estimator)

    # ─────────────────────────────────────────
    # 内部
    # ─────────────────────────────────────────

    def _refresh(self):
        if not self._paused:
            self._renderer.refresh()
