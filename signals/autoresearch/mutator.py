# -*- coding: utf-8 -*-
"""
参数变异器 — 定义可变参数空间、施加受控变异、安全约束验证。

核心设计:
- ParameterSpace: 注册所有可变参数及其边界/约束
- 三种变异策略: 单参数(60%) / 组内(30%) / 跨组(10%)
- 通过直接修改模块级字典实现实时参数替换
- 支持快照/恢复实现 revert
"""
import copy
import random
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple


@dataclass
class ParamSpec:
    """单个参数的规格定义。"""
    group: str          # A/B/C/D/E/F
    name: str           # e.g. "SIGNAL_WEIGHTS.二买"
    getter: callable    # 读取当前值
    setter: callable    # 写入新值
    min_val: float
    max_val: float
    step: float         # 变异步长
    constraint: str = ""  # "positive" / "negative" / "monotonic" / ""


@dataclass
class Mutation:
    """一次变异操作的描述。"""
    mutation_type: str   # "single" / "group" / "cross"
    param_group: str
    param_name: str
    old_value: Any
    new_value: Any


class ParameterSpace:
    """
    可变参数空间管理器。

    注册所有可变参数并提供变异/快照/恢复能力。
    """

    def __init__(self):
        self._specs: List[ParamSpec] = []
        self._groups: Dict[str, List[ParamSpec]] = {}
        self._snapshot: Dict[str, Any] = {}
        self._register_all()

    def _register_all(self):
        """注册所有可变参数组。"""
        self._register_signal_weights()
        self._register_freq_multiplier()
        self._register_resonance_bonus()
        self._register_ma_confirmation()
        self._register_rank_weights()

    # ── A: 信号权重 ──────────────────────────────────────
    def _register_signal_weights(self):
        from signals.core.scorer import SIGNAL_WEIGHTS
        for key, val in SIGNAL_WEIGHTS.items():
            is_sell = val < 0
            spec = ParamSpec(
                group="A",
                name=f"SIGNAL_WEIGHTS.{key}",
                getter=lambda k=key: SIGNAL_WEIGHTS[k],
                setter=lambda v, k=key: SIGNAL_WEIGHTS.__setitem__(k, v),
                min_val=-70 if is_sell else 5,
                max_val=-5 if is_sell else 70,
                step=5,
                constraint="negative" if is_sell else "positive",
            )
            self._specs.append(spec)
            self._groups.setdefault("A", []).append(spec)

    # ── B: 级别系数 ──────────────────────────────────────
    def _register_freq_multiplier(self):
        from signals.core.scorer import FREQ_MULTIPLIER
        for key in FREQ_MULTIPLIER:
            spec = ParamSpec(
                group="B",
                name=f"FREQ_MULTIPLIER.{key}",
                getter=lambda k=key: FREQ_MULTIPLIER[k],
                setter=lambda v, k=key: FREQ_MULTIPLIER.__setitem__(k, v),
                min_val=0.1,
                max_val=2.5,
                step=0.1,
                constraint="positive",
            )
            self._specs.append(spec)
            self._groups.setdefault("B", []).append(spec)

    # ── C: 共振加分 ──────────────────────────────────────
    def _register_resonance_bonus(self):
        # 共振加分存储在函数闭包里，我们用一个可变容器来管理
        import signals.core.scorer as scorer_mod

        # 创建可变容器替换原函数
        if not hasattr(scorer_mod, '_resonance_values'):
            scorer_mod._resonance_values = {
                "三级共振": 25,
                "日线+30M": 20,
                "日线+15M": 15,
                "30M+15M": 12,
                "其他": 10,
            }
            # 替换 _resonance_bonus 函数以使用可变容器
            _original_bonus = scorer_mod._resonance_bonus

            def _patched_bonus(freqs: set) -> int:
                if len(freqs) < 2:
                    return 0
                rv = scorer_mod._resonance_values
                has_daily = bool(freqs & scorer_mod._DAILY_NAMES)
                has_30m = bool(freqs & scorer_mod._30M_NAMES)
                has_15m = bool(freqs & scorer_mod._15M_NAMES)
                if has_daily and has_30m and has_15m:
                    return rv["三级共振"]
                if has_daily and has_30m:
                    return rv["日线+30M"]
                if has_daily and has_15m:
                    return rv["日线+15M"]
                if has_30m and has_15m:
                    return rv["30M+15M"]
                return rv["其他"]

            scorer_mod._resonance_bonus = _patched_bonus

        rv = scorer_mod._resonance_values
        for key in rv:
            spec = ParamSpec(
                group="C",
                name=f"resonance.{key}",
                getter=lambda k=key: scorer_mod._resonance_values[k],
                setter=lambda v, k=key: scorer_mod._resonance_values.__setitem__(k, int(v)),
                min_val=5,
                max_val=35,
                step=2,
                constraint="positive",
            )
            self._specs.append(spec)
            self._groups.setdefault("C", []).append(spec)

    # ── D: MA确认加分 ──────────────────────────────────
    def _register_ma_confirmation(self):
        import signals.core.scorer as scorer_mod

        # 用可变容器管理 MA 加分值
        if not hasattr(scorer_mod, '_ma_bonus_values'):
            scorer_mod._ma_bonus_values = {
                "near_support": 15,
                "multi_head": 10,
                "anti_trend": -5,
            }

        mb = scorer_mod._ma_bonus_values
        specs = [
            ("near_support", 5, 25, 2, "positive"),
            ("multi_head", 5, 20, 2, "positive"),
            ("anti_trend", -15, -2, 1, "negative"),
        ]
        for key, lo, hi, step, constraint in specs:
            spec = ParamSpec(
                group="D",
                name=f"ma_bonus.{key}",
                getter=lambda k=key: scorer_mod._ma_bonus_values[k],
                setter=lambda v, k=key: scorer_mod._ma_bonus_values.__setitem__(k, int(v)),
                min_val=lo,
                max_val=hi,
                step=step,
                constraint=constraint,
            )
            self._specs.append(spec)
            self._groups.setdefault("D", []).append(spec)

    # ── F: 行业排名权重 ─────────────────────────────────
    def _register_rank_weights(self):
        import config as cfg
        for key in cfg.RANK_COMPOSITE_WEIGHTS:
            spec = ParamSpec(
                group="F",
                name=f"RANK_COMPOSITE.{key}",
                getter=lambda k=key: cfg.RANK_COMPOSITE_WEIGHTS[k],
                setter=lambda v, k=key: cfg.RANK_COMPOSITE_WEIGHTS.__setitem__(k, int(v)),
                min_val=5,
                max_val=40,
                step=5,
                constraint="positive",
            )
            self._specs.append(spec)
            self._groups.setdefault("F", []).append(spec)

    # ── 快照/恢复 ────────────────────────────────────────

    def snapshot(self) -> Dict[str, Any]:
        """保存当前所有参数的快照。"""
        snap = {}
        for spec in self._specs:
            snap[spec.name] = spec.getter()
        self._snapshot = snap
        return snap

    def revert(self):
        """恢复到快照。"""
        if not self._snapshot:
            return
        for spec in self._specs:
            if spec.name in self._snapshot:
                spec.setter(self._snapshot[spec.name])

    # ── 变异 ─────────────────────────────────────────────

    def mutate(self) -> Mutation:
        """
        随机选择变异策略并施加变异。

        概率: 单参数 60%, 组内 30%, 跨组 10%
        """
        roll = random.random()
        if roll < 0.6:
            return self._mutate_single()
        elif roll < 0.9:
            return self._mutate_group()
        else:
            return self._mutate_cross()

    def _mutate_single(self) -> Mutation:
        """随机选一个参数，施加 ±step 变异。"""
        spec = random.choice(self._specs)
        old_val = spec.getter()
        delta = random.choice([-1, 1]) * spec.step
        new_val = self._clamp(old_val + delta, spec)
        if new_val == old_val:
            # 如果到了边界，反向变异
            new_val = self._clamp(old_val - delta, spec)
        spec.setter(new_val)
        return Mutation("single", spec.group, spec.name, old_val, new_val)

    def _mutate_group(self) -> Mutation:
        """随机选一个参数组，对组内所有参数施加同向小变异。"""
        group_name = random.choice(list(self._groups.keys()))
        specs = self._groups[group_name]
        direction = random.choice([-1, 1])
        changes = []
        for spec in specs:
            old_val = spec.getter()
            delta = direction * spec.step * random.uniform(0.5, 1.0)
            new_val = self._clamp(old_val + delta, spec)
            spec.setter(new_val)
            changes.append(f"{spec.name}: {old_val}->{new_val}")
        return Mutation(
            "group", group_name,
            f"group_{group_name} ({len(specs)} params)",
            "...", f"direction={'+' if direction > 0 else '-'}"
        )

    def _mutate_cross(self) -> Mutation:
        """从两个不同参数组各选一个参数变异。"""
        groups = list(self._groups.keys())
        if len(groups) < 2:
            return self._mutate_single()
        g1, g2 = random.sample(groups, 2)
        s1 = random.choice(self._groups[g1])
        s2 = random.choice(self._groups[g2])
        changes = []
        for spec in [s1, s2]:
            old_val = spec.getter()
            delta = random.choice([-1, 1]) * spec.step
            new_val = self._clamp(old_val + delta, spec)
            spec.setter(new_val)
            changes.append(f"{spec.name}: {old_val}->{new_val}")
        return Mutation(
            "cross", f"{g1}+{g2}",
            f"{s1.name} + {s2.name}",
            "...", " | ".join(changes)
        )

    def _clamp(self, val: float, spec: ParamSpec) -> float:
        """将值约束在合法范围内。"""
        val = max(spec.min_val, min(spec.max_val, val))
        # 正负约束
        if spec.constraint == "positive" and val <= 0:
            val = spec.step
        elif spec.constraint == "negative" and val >= 0:
            val = -spec.step
        # 整数参数保持整数
        if isinstance(spec.getter(), int):
            val = int(round(val))
        else:
            # 浮点参数保留合理精度
            val = round(val, 2)
        return val

    # ── 验证 ─────────────────────────────────────────────

    def validate(self) -> Tuple[bool, str]:
        """
        验证当前参数是否满足全局约束。

        - FREQ_MULTIPLIER 单调递减
        - 共振加分保持排序
        - 信号权重正负号正确
        """
        # 检查 FREQ_MULTIPLIER 单调性
        from signals.core.scorer import FREQ_MULTIPLIER
        freq_order = ["周线", "日线", "60分钟", "30分钟", "15分钟", "5分钟", "1分钟"]
        prev = float("inf")
        for f in freq_order:
            v = FREQ_MULTIPLIER.get(f, 0)
            if v > prev:
                return False, f"FREQ_MULTIPLIER 非单调: {f}={v} > prev={prev}"
            prev = v

        # 检查共振加分排序
        import signals.core.scorer as scorer_mod
        if hasattr(scorer_mod, '_resonance_values'):
            rv = scorer_mod._resonance_values
            order = ["三级共振", "日线+30M", "日线+15M", "30M+15M", "其他"]
            prev = float("inf")
            for key in order:
                v = rv.get(key, 0)
                if v > prev:
                    return False, f"共振加分非单调: {key}={v} > prev={prev}"
                prev = v

        # 检查信号权重正负号
        from signals.core.scorer import SIGNAL_WEIGHTS
        for key, val in SIGNAL_WEIGHTS.items():
            if "卖" in key and val > 0:
                return False, f"卖信号权重为正: {key}={val}"
            if "买" in key and val < 0:
                return False, f"买信号权重为负: {key}={val}"
            # 形态信号检查: 顶/下降 应为负, 底/上升 应为正
            if "顶" in key and val > 0:
                return False, f"顶部形态权重为正: {key}={val}"
            if "底" in key and val < 0:
                return False, f"底部形态权重为负: {key}={val}"

        return True, "OK"

    # ── 信息查询 ─────────────────────────────────────────

    @property
    def param_count(self) -> int:
        return len(self._specs)

    @property
    def group_names(self) -> List[str]:
        return list(self._groups.keys())

    def current_values(self) -> Dict[str, Any]:
        """获取所有参数的当前值。"""
        return {spec.name: spec.getter() for spec in self._specs}
