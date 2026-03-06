# -*- coding: utf-8 -*-
"""
Rich 实时面板渲染器
- 底部固定面板（Live），上方滚动日志
- 类似 docker build 效果
"""
import time
from typing import Optional

from rich.console import Console
from rich.live import Live
from rich.markup import escape
from rich.table import Table
from rich.text import Text
from rich.panel import Panel

from .models import DashboardState, PhaseState, PHASE_ORDER


def _fmt_sec(sec: float) -> str:
    """格式化秒数"""
    if sec < 0.01:
        return "--"
    if sec < 0.1:
        return "<0.1s"
    if sec < 60:
        return f"{sec:.1f}s"
    return f"{int(sec // 60)}m{int(sec % 60)}s"


def _status_icon(status: str) -> str:
    if status == "done":
        return "[green]✅[/green]"
    if status == "running":
        return "[cyan]🔄[/cyan]"
    if status == "skipped":
        return "[dim]⏭️[/dim]"
    return "[dim]⏳[/dim]"


def _progress_bar(pct: int, width: int = 12) -> str:
    """纯文本进度条"""
    filled = int(width * pct / 100)
    empty = width - filled
    return f"[cyan]{'━' * filled}[/cyan][dim]{'─' * empty}[/dim]"


class RichRenderer:
    """基于 rich.Live 的底部固定面板 + 上方滚动日志"""

    def __init__(self, state: DashboardState):
        self._state = state
        self._console = Console()
        self._live: Optional[Live] = None
        self._started = False

    def start(self) -> bool:
        if self._started:
            return True
        try:
            self._live = Live(
                self._build(),
                console=self._console,
                refresh_per_second=4,
                transient=True,
            )
            self._live.start()
            self._started = True
            return True
        except Exception:
            self._started = False
            return False

    def stop(self):
        if self._live and self._started:
            self._live.stop()
            self._started = False

    def refresh(self):
        if self._live and self._started:
            try:
                self._live.update(self._build())
            except Exception:
                pass

    def log(self, msg: str):
        """在面板上方打印滚动日志（重要消息）"""
        if self._live and self._started:
            self._live.console.print(msg, markup=False, highlight=False)
        else:
            print(msg, flush=True)

    def detail(self, msg: str):
        """任务级详情 — Rich 面板已展示进度，静默"""
        pass

    def print_summary(self, state: DashboardState, eta_estimator=None):
        """面板结束后打印最终汇总"""
        total = state.total_elapsed
        print(f"\n{'━' * 56}", flush=True)
        print(f"  📋 运行汇总  ({_fmt_sec(total)})", flush=True)
        print(f"{'─' * 56}", flush=True)

        if state.market_direction:
            print(f"  大势: {state.market_direction}  |  风格: {state.market_style}", flush=True)
        if state.l2_count:
            print(f"  L2 入池: {state.l2_count} 只", flush=True)
        if state.l3_count:
            print(f"  L3 筛选: {state.l3_count} 只", flush=True)

        print(f"{'─' * 56}", flush=True)
        for phase_id, display in PHASE_ORDER:
            ps = state.phases.get(phase_id)
            if ps is None:
                continue
            if ps.status == "skipped":
                print(f"  {display:<20s} {'跳过':>8s}", flush=True)
            elif ps.status == "done":
                elapsed_str = _fmt_sec(ps.elapsed)
                detail = f"  {ps.detail}" if ps.detail else ""
                err = f"  ({ps.errors}错误)" if ps.errors else ""
                print(f"  {display:<20s} {elapsed_str:>8s}{detail}{err}", flush=True)
        print(f"  {'总计':<20s} {_fmt_sec(total):>8s}", flush=True)

        if state.total_errors:
            print(f"{'─' * 56}", flush=True)
            print(f"  错误汇总 ({state.total_errors} 个):", flush=True)
            for err in state.error_log[-5:]:
                print(f"    {err}", flush=True)

        if state.degradations:
            print(f"{'─' * 56}", flush=True)
            print(f"  数据源降级:", flush=True)
            for d in state.degradations:
                print(f"    {d}", flush=True)

        print(f"{'━' * 56}\n", flush=True)

    def _build(self) -> Panel:
        """构建面板内容"""
        state = self._state
        from datetime import datetime
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M")

        from .estimator import ETAEstimator
        eta_est = ETAEstimator()
        remaining = eta_est.estimate_remaining(state)
        eta_str = f"预估剩余: ~{_fmt_sec(remaining)}" if remaining > 0.5 else "即将完成"

        # 构建表格
        table = Table(
            show_header=False, show_edge=False, show_lines=False,
            padding=(0, 1), expand=True,
        )
        table.add_column("icon", width=3, no_wrap=True)
        table.add_column("phase", min_width=14, no_wrap=True)
        table.add_column("bar", min_width=14, no_wrap=True)
        table.add_column("time", min_width=8, justify="right", no_wrap=True)
        table.add_column("info", no_wrap=True)

        for phase_id, display in PHASE_ORDER:
            ps = state.phases.get(phase_id)
            if ps is None:
                # 阶段未注册 — 显示为待执行
                table.add_row(
                    _status_icon("pending"),
                    f"[dim]{display}[/dim]",
                    _progress_bar(0),
                    "[dim]--[/dim]",
                    "[dim]待执行[/dim]",
                )
                continue

            icon = _status_icon(ps.status)
            elapsed_str = _fmt_sec(ps.elapsed)

            if ps.status == "pending":
                table.add_row(
                    icon,
                    f"[dim]{display}[/dim]",
                    _progress_bar(0),
                    "[dim]--[/dim]",
                    "[dim]待执行[/dim]",
                )
            elif ps.status == "skipped":
                table.add_row(
                    icon,
                    f"[dim]{display}[/dim]",
                    "",
                    "[dim]--[/dim]",
                    "[dim]跳过[/dim]",
                )
            elif ps.status == "running":
                if ps.total > 0:
                    info = f"{ps.done}/{ps.total}"
                    bar = _progress_bar(ps.progress_pct)
                    if ps.errors:
                        info += f" [red]({ps.errors}err)[/red]"
                else:
                    info = "..."
                    bar = "[cyan]━━━[/cyan][dim]─────────[/dim]"
                table.add_row(
                    icon, f"[bold]{display}[/bold]", bar, elapsed_str, info,
                )
                # 当前子任务
                if ps.active_task:
                    # 卡住检测：超过预估 2x 标红
                    task_text = escape(ps.active_task)
                    est = eta_est.estimate_phase(phase_id)
                    if ps.elapsed > est * 2 and est > 1:
                        task_label = f"   [bold red]↳ {task_text}[/bold red]"
                    elif ps.elapsed > est * 1.5 and est > 1:
                        task_label = f"   [yellow]↳ {task_text}[/yellow]"
                    else:
                        task_label = f"   [dim]↳ {task_text}[/dim]"
                    table.add_row(
                        "", "", task_label, "", "",
                    )
            elif ps.status == "done":
                detail = escape(ps.detail) if ps.detail else ""
                if ps.total > 0:
                    detail = f"{ps.done}/{ps.total}" + (f"  {escape(ps.detail)}" if ps.detail else "")
                err_str = f" [red]({ps.errors}err)[/red]" if ps.errors else ""
                table.add_row(
                    icon,
                    f"[green]{display}[/green]",
                    _progress_bar(100),
                    f"[green]{elapsed_str}[/green]",
                    f"{detail}{err_str}",
                )

        # 底部状态行
        footer_parts = []
        if state.degradations:
            footer_parts.append(
                f"[yellow]降级({len(state.degradations)})[/yellow]"
            )
        if state.total_errors:
            last_err = state.error_log[-1] if state.error_log else ""
            footer_parts.append(
                f"[red]错误({state.total_errors})[/red]: {escape(last_err[:50])}"
            )

        footer = "  ".join(footer_parts) if footer_parts else "[dim]运行中...[/dim]"

        header = (
            f"[bold]🐲 隆小侠[/bold]  {state.mode}  {now_str}"
            f"  |  总耗时: {_fmt_sec(state.total_elapsed)}  |  {eta_str}"
        )

        content = table
        # 如果有错误/降级，加在表格下方
        if footer_parts:
            from rich.console import Group
            footer_text = Text.from_markup(f"\n{footer}")
            content = Group(table, footer_text)

        return Panel(
            content,
            title=header,
            border_style="cyan",
            padding=(0, 1),
        )
