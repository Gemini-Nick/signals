"""
创业板指三段历史对比：K线 + MACD（纵向排列）
- 2022年6-8月（7月8日见顶 2888）
- 2024年10-12月（11月12日见顶 2448）
- 2025年12月至今（当前走势）
"""

import akshare as ak
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots

# ── 数据获取 ──────────────────────────────────────────────
df_all = ak.stock_zh_index_daily(symbol="sz399006")
df_all["date"] = pd.to_datetime(df_all["date"])
df_all = df_all.sort_values("date").reset_index(drop=True)

segments = [
    ("① 2022年6-8月 — 反弹见顶2888后回落", "2022-04-01", "2022-08-31", "2022-06-01",
     [("2022-07-08", 2888, "7/8 高点2888")]),
    ("② 2024年10-12月 — 924行情后二次探底", "2024-08-01", "2024-12-31", "2024-10-08",
     [("2024-10-08", 2576, "10/8 高开2576"), ("2024-11-12", 2448, "11/12 反弹高2448")]),
    ("③ 2025年12月至今 — 当前走势", "2025-10-01", "2026-12-31", "2025-12-01", []),
]


def calc_macd(close: pd.Series, fast=12, slow=26, signal=9):
    ema_fast = close.ewm(span=fast, adjust=False).mean()
    ema_slow = close.ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd_hist = 2 * (dif - dea)
    return dif, dea, macd_hist


# ── 6行布局：每段 K线+MACD 各占一行 ──────────────────────
titles = []
for s in segments:
    titles.append(s[0])
    titles.append("MACD")

fig = make_subplots(
    rows=6, cols=1,
    shared_xaxes=False,
    vertical_spacing=0.035,
    subplot_titles=titles,
    row_heights=[0.20, 0.10, 0.20, 0.10, 0.20, 0.10],
)

colors_up = "#ef4444"
colors_down = "#22c55e"

for seg_idx, (title, warmup_start, end, display_start, annotations) in enumerate(segments):
    k_row = seg_idx * 2 + 1
    m_row = seg_idx * 2 + 2

    mask_warmup = (df_all["date"] >= warmup_start) & (df_all["date"] <= end)
    df_seg = df_all[mask_warmup].copy().reset_index(drop=True)
    df_seg["dif"], df_seg["dea"], df_seg["macd_hist"] = calc_macd(df_seg["close"])

    mask_display = df_seg["date"] >= display_start
    df_plot = df_seg[mask_display].copy()
    # 带年份的日期标签
    df_plot["date_str"] = df_plot["date"].dt.strftime("%y/%m/%d")

    # K线
    fig.add_trace(
        go.Candlestick(
            x=df_plot["date_str"],
            open=df_plot["open"], high=df_plot["high"],
            low=df_plot["low"], close=df_plot["close"],
            increasing_line_color=colors_up, increasing_fillcolor=colors_up,
            decreasing_line_color=colors_down, decreasing_fillcolor=colors_down,
            showlegend=False,
        ),
        row=k_row, col=1,
    )

    # 高点标注
    for ann_date, ann_price, ann_text in annotations:
        ann_str = pd.to_datetime(ann_date).strftime("%y/%m/%d")
        fig.add_annotation(
            x=ann_str, y=ann_price, text=ann_text,
            showarrow=True, arrowhead=2, arrowcolor="#fbbf24",
            font=dict(size=11, color="#fbbf24"), ax=0, ay=-30,
            row=k_row, col=1,
        )

    # 第三段：自动标注高点和最新收盘
    if seg_idx == 2:
        idx_high = df_plot["high"].idxmax()
        high_row = df_plot.loc[idx_high]
        fig.add_annotation(
            x=high_row["date_str"], y=high_row["high"],
            text=f'{high_row["date"].strftime("%m/%d")} 高点 {high_row["high"]:.0f}',
            showarrow=True, arrowhead=2, arrowcolor="#fbbf24",
            font=dict(size=11, color="#fbbf24"), ax=-40, ay=-30,
            row=k_row, col=1,
        )
        last = df_plot.iloc[-1]
        fig.add_annotation(
            x=last["date_str"], y=last["close"],
            text=f'最新 {last["close"]:.0f}',
            showarrow=True, arrowhead=2, arrowcolor="#60a5fa",
            font=dict(size=11, color="#60a5fa"), ax=30, ay=-20,
            row=k_row, col=1,
        )

    # MACD 柱
    bar_colors = [colors_up if v >= 0 else colors_down for v in df_plot["macd_hist"]]
    fig.add_trace(
        go.Bar(x=df_plot["date_str"], y=df_plot["macd_hist"],
               marker_color=bar_colors, showlegend=False),
        row=m_row, col=1,
    )
    # DIF
    fig.add_trace(
        go.Scatter(x=df_plot["date_str"], y=df_plot["dif"],
                   line=dict(color="#3b82f6", width=1.2),
                   name="DIF", showlegend=(seg_idx == 0)),
        row=m_row, col=1,
    )
    # DEA
    fig.add_trace(
        go.Scatter(x=df_plot["date_str"], y=df_plot["dea"],
                   line=dict(color="#f59e0b", width=1.2),
                   name="DEA", showlegend=(seg_idx == 0)),
        row=m_row, col=1,
    )
    fig.add_hline(y=0, line_dash="dot", line_color="gray", line_width=0.5, row=m_row, col=1)

    # 金叉/死叉
    dif_v = df_plot["dif"].values
    dea_v = df_plot["dea"].values
    ds = df_plot["date_str"].values
    for i in range(1, len(dif_v)):
        if dif_v[i - 1] > dea_v[i - 1] and dif_v[i] <= dea_v[i]:
            fig.add_annotation(x=ds[i], y=dif_v[i], text="死叉", showarrow=False,
                               font=dict(size=9, color="#22c55e"), yshift=12, row=m_row, col=1)
        elif dif_v[i - 1] < dea_v[i - 1] and dif_v[i] >= dea_v[i]:
            fig.add_annotation(x=ds[i], y=dif_v[i], text="金叉", showarrow=False,
                               font=dict(size=9, color="#ef4444"), yshift=-12, row=m_row, col=1)

# ── 布局 ──────────────────────────────────────────────────
fig.update_layout(
    title=dict(text="创业板指 — 三次「新高后回落」K线 + MACD 对比", font=dict(size=16)),
    height=1600, width=1000,
    template="plotly_dark",
    legend=dict(orientation="h", yanchor="bottom", y=1.01, xanchor="right", x=1),
    margin=dict(l=60, r=60, t=80, b=40),
)

for r in range(1, 7):
    fig.update_xaxes(rangeslider_visible=False, row=r, col=1)
    fig.update_xaxes(type="category", nticks=12, tickangle=-45, tickfont=dict(size=9), row=r, col=1)

output_path = "/Users/zhangqilong/Desktop/Signals/chinext_compare.html"
fig.write_html(output_path)
print(f"图表已保存: {output_path}")
