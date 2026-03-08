# AutoResearch Agent 行为规范

> 等价于 Karpathy autoresearch 的 `program.md`

## 目标

**最大化 Fitness 分数**（0~100），衡量信号系统的综合质量。

```
Fitness = 0.35 * norm(avg_SQS)
        + 0.25 * norm(overall_win_rate)
        + 0.20 * norm(profit_factor)
        + 0.20 * norm(expectancy)
```

## 可修改范围

仅以下参数可被变异：

| 组 | 文件 | 参数 |
|---|---|---|
| A | `scorer.py` | SIGNAL_WEIGHTS (18项信号基础分值) |
| B | `scorer.py` | FREQ_MULTIPLIER (7项级别系数) |
| C | `scorer.py` | _resonance_bonus (5档共振加分) |
| D | `scorer.py` | MA确认加分 (支撑/多头/逆势) |
| F | `config.py` | RANK_COMPOSITE_WEIGHTS (7项行业排名权重) |

## 不可修改

- `backtest.py` (评估逻辑 = 真相，不可篡改)
- `detectors.py` (信号检测逻辑)
- `prepare.py` 等价物 = 数据层
- 评估窗口、中性带、目标收益率等 BACKTEST_* 参数

## 约束

1. 买信号权重必须 > 0，卖信号权重必须 < 0
2. FREQ_MULTIPLIER 必须单调递减（周线 > 日线 > ... > 1分钟）
3. 共振加分必须保持排序（三级 > 日线+30M > 日线+15M > 30M+15M > 其他）
4. 单次变异幅度 ≤ 步长（不允许跳跃式变化）
5. 至少 20 条已评估信号才能开始实验

## 决策规则

- `fitness_after > fitness_before` → **KEEP**（保留变异，git commit）
- `fitness_after <= fitness_before` → **REVERT**（回退到快照）
- 约束违反 → **SKIP**（自动回退，不计入实验）

## 永不停止

一旦启动，循环运行直到被手动中断 (Ctrl+C)。
每 50 轮输出中间摘要。
