/**
 * 隆小侠 LONG CLAW — K线图表 + CZSC 缠论叠加 + MACD
 * 使用 TradingView Lightweight Charts v4
 */

// ── 图表状态 ─────────────────────────────────────────
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let biSeries = null;         // 笔线 (LineSeries)
let maSeries = [];           // MA 均线 (LineSeries[])
let macdDifSeries = null;
let macdDeaSeries = null;
let macdBarSeries = null;
let currentSymbol = '';
let currentFreq = 'daily';

// ── 主题颜色获取 ─────────────────────────────────────
function chartColors() {
  return {
    bg: cssVar('--chart-bg') || '#131722',
    grid: cssVar('--chart-grid') || '#1e222d',
    text: cssVar('--text-secondary') || '#787b86',
    crosshair: cssVar('--chart-crosshair') || '#787b86',
    upColor: cssVar('--color-up') || '#f23645',
    downColor: cssVar('--color-down') || '#26a69a',
    biUp: cssVar('--bi-up') || '#f23645',
    biDown: cssVar('--bi-down') || '#26a69a',
    signalBuy: cssVar('--signal-buy') || '#f7931a',
    signalSell: cssVar('--signal-sell') || '#9c27b0',
    volUp: cssVar('--vol-up') || 'rgba(242,54,69,0.5)',
    volDown: cssVar('--vol-down') || 'rgba(38,166,154,0.5)',
    zhongshuStroke: cssVar('--zhongshu-stroke') || 'rgba(41,98,255,0.6)',
    macdDif: cssVar('--macd-dif') || '#f7931a',
    macdDea: cssVar('--macd-dea') || '#2962ff',
    macdBarUp: cssVar('--macd-bar-up') || 'rgba(242,54,69,0.7)',
    macdBarDown: cssVar('--macd-bar-down') || 'rgba(38,166,154,0.7)',
  };
}

// ── 创建/重建图表 ────────────────────────────────────
function createChart() {
  const container = document.getElementById('chart-container');
  const c = chartColors();

  // 销毁旧图表
  if (chart) {
    chart.remove();
    chart = null;
  }

  chart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 620,
    layout: {
      background: { type: 'solid', color: c.bg },
      textColor: c.text,
      fontFamily: cssVar('--font-family') || 'sans-serif',
    },
    grid: {
      vertLines: { color: c.grid },
      horzLines: { color: c.grid },
    },
    crosshair: {
      vertLine: { color: c.crosshair, width: 1, style: 2 },
      horzLine: { color: c.crosshair, width: 1, style: 2 },
    },
    timeScale: {
      borderColor: c.grid,
      timeVisible: true,
      secondsVisible: false,
    },
    rightPriceScale: {
      borderColor: c.grid,
    },
  });

  // K 线
  candleSeries = chart.addCandlestickSeries({
    upColor: c.upColor,
    downColor: c.downColor,
    borderUpColor: c.upColor,
    borderDownColor: c.downColor,
    wickUpColor: c.upColor,
    wickDownColor: c.downColor,
    priceScaleId: 'right',
  });

  // 成交量 (middle band)
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  chart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.72, bottom: 0.18 },
  });

  // 旧 chart 已 remove，series 引用全部失效
  biSeries = null;
  maSeries = [];
  macdDifSeries = null;
  macdDeaSeries = null;
  macdBarSeries = null;

  // 响应式
  const ro = new ResizeObserver(() => {
    chart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);

  return chart;
}

// ── 绘制笔线 ────────────────────────────────────────
function drawBiLines(biList) {
  const c = chartColors();
  if (!biList || biList.length === 0) return;

  const points = [];
  biList.forEach((bi) => {
    if (bi.direction === 'up') {
      points.push({ time: bi.sdt, value: bi.low });
      points.push({ time: bi.edt, value: bi.high });
    } else {
      points.push({ time: bi.sdt, value: bi.high });
      points.push({ time: bi.edt, value: bi.low });
    }
  });

  // 去重 + 排序
  const seen = new Set();
  const uniquePoints = [];
  points.forEach(p => {
    const key = p.time + '_' + p.value;
    if (!seen.has(key)) {
      seen.add(key);
      uniquePoints.push(p);
    }
  });
  uniquePoints.sort((a, b) => a.time - b.time);

  // 合并相同时间戳
  const merged = [];
  uniquePoints.forEach(p => {
    if (merged.length > 0 && merged[merged.length - 1].time === p.time) {
      merged[merged.length - 1] = p;
    } else {
      merged.push(p);
    }
  });

  if (merged.length < 2) return;

  biSeries = chart.addLineSeries({
    color: c.biUp,
    lineWidth: 2,
    lineStyle: 0,
    crosshairMarkerVisible: false,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  biSeries.setData(merged);
}

// ── 绘制中枢 (矩形框: 上下左右4条实线) ─────────────
function drawZhongshu(zhongshuList) {
  if (!zhongshuList || zhongshuList.length === 0) return;

  const c = chartColors();
  zhongshuList.forEach(zs => {
    // 上沿线
    const upperSeries = chart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    upperSeries.setData([
      { time: zs.start_dt, value: zs.zg },
      { time: zs.end_dt, value: zs.zg },
    ]);

    // 下沿线
    const lowerSeries = chart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    lowerSeries.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zd },
    ]);

    // 左竖线
    const leftSeries = chart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    leftSeries.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.start_dt, value: zs.zg },
    ]);

    // 右竖线
    const rightSeries = chart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    rightSeries.setData([
      { time: zs.end_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zg },
    ]);

    // 中枢标记
    upperSeries.setMarkers([{
      time: Math.floor((zs.start_dt + zs.end_dt) / 2),
      position: 'aboveBar',
      color: c.zhongshuStroke,
      shape: 'square',
      text: `ZS ${zs.bi_count}B`,
    }]);
  });
}

// ── MA 图例 overlay ─────────────────────────────────
let maLegendData = []; // [{label, color, series}]

function createMALegend() {
  const container = document.getElementById('chart-container');
  let legend = document.getElementById('ma-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.id = 'ma-legend';
    legend.className = 'ma-legend';
    container.appendChild(legend);
  }
  legend.innerHTML = '';
  return legend;
}

function updateMALegend(param) {
  const legend = document.getElementById('ma-legend');
  if (!legend || maLegendData.length === 0) return;

  const parts = maLegendData.map(item => {
    let val = '—';
    if (param && param.seriesData) {
      const d = param.seriesData.get(item.series);
      if (d && d.value !== undefined) val = d.value.toFixed(0);
    } else {
      // 无 crosshair 时显示最新值
      const lastData = item.lastValue;
      if (lastData != null) val = lastData.toFixed(0);
    }
    return `<span class="ma-legend-item" style="color:${item.color}">${item.label}: ${val}</span>`;
  });
  legend.innerHTML = parts.join(' <span class="ma-legend-sep">|</span> ');
}

// ── 绘制MA均线 ──────────────────────────────────────
function drawMALines(maLines) {
  if (!maLines || maLines.length === 0) return;
  maLegendData = [];

  maLines.forEach(ma => {
    if (!ma.data || ma.data.length < 2) return;
    const series = chart.addLineSeries({
      color: ma.color,
      lineWidth: 1,
      lineStyle: 0,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      title: '',
      priceScaleId: '',
    });
    series.setData(ma.data);
    maSeries.push(series);
    maLegendData.push({
      label: ma.label,
      color: ma.color,
      series: series,
      lastValue: ma.data[ma.data.length - 1].value,
    });
  });

  // 创建图例并显示最新值
  createMALegend();
  updateMALegend(null);

  // crosshair 联动
  chart.subscribeCrosshairMove(updateMALegend);
}

// ── 绘制 MACD 子图 ──────────────────────────────────
function drawMACD(macdData) {
  if (!macdData || macdData.length < 2) return;

  const c = chartColors();

  // MACD 柱状图
  macdBarSeries = chart.addHistogramSeries({
    priceScaleId: 'macd',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    lastValueVisible: false,
  });
  chart.priceScale('macd').applyOptions({
    scaleMargins: { top: 0.85, bottom: 0 },
  });
  macdBarSeries.setData(macdData.map(d => ({
    time: d.time,
    value: d.bar,
    color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown,
  })));

  // DIF 线
  macdDifSeries = chart.addLineSeries({
    color: c.macdDif, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DIF',
  });
  macdDifSeries.setData(macdData.map(d => ({ time: d.time, value: d.dif })));

  // DEA 线
  macdDeaSeries = chart.addLineSeries({
    color: c.macdDea, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DEA',
  });
  macdDeaSeries.setData(macdData.map(d => ({ time: d.time, value: d.dea })));
}

// ── 绘制买卖点标记 ──────────────────────────────────
function drawSignalMarkers(signals) {
  if (!candleSeries || !signals || signals.length === 0) return;

  const c = chartColors();
  const markers = signals.map(s => {
    const isBuy = s.type.includes('买');
    const isDivergence = s.type.includes('背驰');
    let color, shape;
    if (isDivergence && isBuy) {
      color = '#00e676'; shape = 'arrowUp';
    } else if (isDivergence && !isBuy) {
      color = '#ff1744'; shape = 'arrowDown';
    } else {
      color = isBuy ? c.signalBuy : c.signalSell;
      shape = isBuy ? 'arrowUp' : 'arrowDown';
    }
    return {
      time: s.dt,
      position: isBuy ? 'belowBar' : 'aboveBar',
      color: color,
      shape: shape,
      text: s.type,
    };
  });

  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
}

// ── 信号详情面板 (增强版) ────────────────────────────
function renderSignalDetails(data) {
  const body = document.getElementById('signal-details-body');
  const { signals, report, meta, zhongshu, report_signals } = data;

  let html = '';

  // 操作结论（最重要，置顶）
  if (report && report.conclusion) {
    html += `<div class="detail-conclusion">${report.conclusion}</div>`;
  }

  // 多级别趋势摘要
  if (report && report.daily_trend) {
    const trendCls = (t) => t === '上涨趋势' ? 'up' : t === '下跌趋势' ? 'down' : 'flat';
    html += `<div class="detail-section">
      <div class="detail-section-title">多级别趋势</div>
      <div class="detail-trends">
        <span class="trend-chip ${trendCls(report.daily_trend)}">日线: ${report.daily_trend}</span>
        <span class="trend-chip ${trendCls(report.f30_trend)}">30M: ${report.f30_trend}</span>
        <span class="trend-chip ${trendCls(report.f15_trend)}">15M: ${report.f15_trend}</span>
      </div>
      ${report.three_level_aligned ? '<div class="detail-highlight">三级共振</div>' : ''}
    </div>`;
  }

  // 均线排列
  if (report && report.ma_trend) {
    const maTrendCls = report.ma_trend === '多头排列' ? 'up' : report.ma_trend === '空头排列' ? 'down' : 'flat';
    html += `<div class="detail-section">
      <div class="detail-section-title">均线状态</div>
      <span class="trend-chip ${maTrendCls}">${report.ma_trend}</span>`;
    if (report.key_levels && report.key_levels.length > 0) {
      html += '<div class="detail-key-levels">';
      report.key_levels.forEach(lv => {
        const arrow = lv.position === '上方' ? '\u25B2' : lv.position === '下方' ? '\u25BC' : '\u25C6';
        const cls = lv.position === '上方' ? 'resistance' : 'support';
        const lvVal = lv.value != null ? lv.value.toFixed(0) : '—';
        const distPct = lv.distance_pct != null ? `${lv.distance_pct > 0 ? '+' : ''}${lv.distance_pct.toFixed(1)}%` : '';
        html += `<span class="key-level ${cls}">${arrow}${lv.name} ${lvVal} (${distPct})</span>`;
      });
      html += '</div>';
    }
    html += '</div>';
  }

  // 中枢信息
  if (zhongshu && zhongshu.length > 0) {
    html += `<div class="detail-section">
      <div class="detail-section-title">中枢 (${zhongshu.length}个)</div>`;
    zhongshu.forEach((zs, i) => {
      html += `<div class="detail-zs">ZS${i + 1}: ${zs.zd.toFixed(2)} ~ ${zs.zg.toFixed(2)} (${zs.bi_count}笔)</div>`;
    });
    html += '</div>';
  }

  // 跨级别信号摘要
  if (report_signals && report_signals.length > 0) {
    html += `<div class="detail-section">
      <div class="detail-section-title">全级别信号快照</div>
      <div class="detail-tf-signals">`;
    report_signals.forEach(rs => {
      const isBuy = rs.type.includes('买');
      html += `<span class="tf-signal-chip ${isBuy ? 'buy' : 'sell'}">${rs.freq}: ${rs.type}</span>`;
    });
    html += '</div></div>';
  }

  // 当前周期信号列表
  if (signals && signals.length > 0) {
    html += `<div class="detail-section">
      <div class="detail-section-title">当前周期信号 (${signals.length})</div>`;
    signals.forEach(s => {
      const isBuy = s.type.includes('买');
      html += `<div class="detail-item">
        <span class="detail-freq">[${s.freq}]</span>
        <span class="detail-type ${isBuy ? 'buy' : 'sell'}">${s.type}</span>
        <span class="detail-conf">conf ${s.confidence != null ? (s.confidence * 100).toFixed(0) : '—'}%</span>
        <span class="detail-price">@ ${s.price != null ? s.price.toFixed(2) : '—'}</span>
        ${s.details ? `<div class="detail-desc">${s.details}</div>` : ''}
      </div>`;
    });
    html += '</div>';
  }

  // IndexReport summary
  if (report && report.summary) {
    html += `<div class="detail-section">
      <div class="detail-section-title">综合研判</div>
      <div class="detail-summary">${report.summary}</div>
    </div>`;
  }

  if (!html) {
    html = '<div class="detail-item" style="color:var(--text-muted);">无信号</div>';
  }

  body.innerHTML = html;
  body.classList.add('open');
  document.getElementById('signal-details-arrow').innerHTML = '&#9650;';
}

// ── 摘要栏 ─────────────────────────────────────────
function renderChartSummary(data) {
  const summary = document.getElementById('chart-summary');
  const { meta, signals, report } = data;

  const buyCount = signals.filter(s => s.type.includes('买')).length;
  const sellCount = signals.filter(s => s.type.includes('卖')).length;

  let direction = '中性';
  let dirCls = '';
  if (report && report.is_bullish !== undefined) {
    direction = report.is_bullish ? '偏多' : '偏空';
    dirCls = report.is_bullish ? 'up' : 'down';
  } else if (buyCount > sellCount) {
    direction = '偏多'; dirCls = 'up';
  } else if (sellCount > buyCount) {
    direction = '偏空'; dirCls = 'down';
  }

  let items = `
    <span class="summary-item">方向: <span class="summary-value ${dirCls}">${direction}</span></span>
    <span class="summary-item">买: <span class="summary-value">${buyCount}</span></span>
    <span class="summary-item">卖: <span class="summary-value">${sellCount}</span></span>
    <span class="summary-item">周期: <span class="summary-value">${meta.freq}</span></span>`;

  if (report && report.daily_trend) {
    items += `<span class="summary-item">日线: <span class="summary-value">${report.daily_trend}</span></span>`;
  }
  if (report && report.ma_trend) {
    items += `<span class="summary-item">MA: <span class="summary-value">${report.ma_trend}</span></span>`;
  }

  // 跨级别信号
  if (data.report_signals && data.report_signals.length > 0) {
    const sigStr = data.report_signals.map(rs => {
      const cls = rs.type.includes('买') ? 'up' : 'down';
      return `<span class="${cls}">${rs.freq}:${rs.type}</span>`;
    }).join(' ');
    items += `<span class="summary-item">${sigStr}</span>`;
  }

  summary.innerHTML = items;
}

// ── 加载图表数据 ─────────────────────────────────────
async function loadChart(indexName, freq) {
  currentSymbol = indexName;
  currentFreq = freq || 'daily';

  document.getElementById('chart-title').textContent = indexName;

  document.querySelectorAll('.freq-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.freq === currentFreq);
  });

  try {
    const data = await apiFetch(`/api/chart/${encodeURIComponent(indexName)}?freq=${currentFreq}`);

    createChart();

    // K线数据
    if (data.ohlcv && data.ohlcv.length > 0) {
      candleSeries.setData(data.ohlcv);

      const c = chartColors();
      const volData = data.ohlcv.map(bar => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? c.volUp : c.volDown,
      }));
      volumeSeries.setData(volData);
    }

    // CZSC 叠加
    drawBiLines(data.bi_list);
    drawZhongshu(data.zhongshu);
    drawSignalMarkers(data.signals);

    // MA 均线叠加
    drawMALines(data.ma_lines);

    // MACD 子图
    drawMACD(data.macd);

    // 信号详情 & 摘要
    renderSignalDetails(data);
    renderChartSummary(data);

    // 自适应可见区域
    chart.timeScale().fitContent();

  } catch (err) {
    console.error('Chart load failed:', err);
    document.getElementById('chart-title').textContent =
      indexName + ' - 加载失败: ' + err.message;
  }
}

// ── 主题切换时重建图表 ───────────────────────────────
window.chartInstance = {
  applyTheme: () => {
    if (currentSymbol) {
      loadChart(currentSymbol, currentFreq);
    }
  }
};

// ── 周期切换 ─────────────────────────────────────────
document.querySelectorAll('.freq-btn').forEach(btn => {
  btn.addEventListener('click', () => {
    if (currentSymbol) {
      loadChart(currentSymbol, btn.dataset.freq);
    }
  });
});

window.loadChart = loadChart;

// ── 行业图表模式 ──────────────────────────────────────
let isIndustryMode = false;

function renderIndustryInfo(data) {
  const panel = document.getElementById('industry-info-panel');
  const related = document.getElementById('industry-related');
  const detail = document.getElementById('industry-detail');
  if (!panel || !detail) return;

  detail.style.display = 'block';

  const info = data.industry_info || {};
  const report = data.report || {};

  let html = '';

  // 行业属性
  const rotLine = info.rotation_line || '—';
  const sectorType = info.sector_type || '中性';
  const phase = info.phase || '—';
  const phaseHint = info.phase_hint || '';

  html += `<div class="ind-info-row">
    <span class="ind-tag rotation">${rotLine}</span>
    <span class="ind-tag sector">${sectorType}</span>
    ${phase !== '—' ? `<span class="ind-tag phase">${phase}</span>` : ''}
  </div>`;

  // CZSC 报告
  const trend = report.daily_trend || '未知';
  const trendCls = trend === '上涨趋势' ? 'up' : trend === '下跌趋势' ? 'down' : 'flat';
  const signal = report.daily_latest_signal || '无';
  const biCount = report.daily_bi_count || 0;

  html += `<div class="ind-info-row">
    <span class="trend-chip ${trendCls}">趋势: ${trend}</span>
    <span class="ind-stat">笔数: ${biCount}</span>
    ${signal !== '无' ? `<span class="ind-stat signal">信号: ${signal}</span>` : ''}
  </div>`;

  // 涨跌幅 + 综合分
  if (info.gain_pct !== undefined) {
    const gainCls = info.gain_pct >= 0 ? 'up' : 'down';
    html += `<div class="ind-info-row">
      <span class="ind-stat">今日: <span class="${gainCls}">${info.gain_pct >= 0 ? '+' : ''}${info.gain_pct}%</span></span>
      ${info.composite_score ? `<span class="ind-stat">综合分: ${info.composite_score}</span>` : ''}
      ${info.zt_count ? `<span class="ind-stat">涨停: ${info.zt_count}只</span>` : ''}
    </div>`;
  }

  if (phaseHint) {
    html += `<div class="ind-phase-hint">${phaseHint}</div>`;
  }

  panel.innerHTML = html;

  // 关联行业
  if (data.related_industries && data.related_industries.length > 0) {
    related.innerHTML = '<span class="ind-related-label">同线行业: </span>' +
      data.related_industries.map(name =>
        `<span class="ind-related-chip" onclick="navigateToIndustryChart('${name}')">${name}</span>`
      ).join('');
  } else {
    related.innerHTML = '';
  }
}

async function loadIndustryChart(industryName) {
  isIndustryMode = true;
  currentSymbol = industryName;
  currentFreq = 'daily';

  // 设置行业模式 UI
  document.getElementById('chart-title').textContent = industryName + ' (行业)';

  // 隐藏周期切换（行业只有日线）
  const freqGroup = document.querySelector('.freq-group');
  if (freqGroup) freqGroup.style.display = 'none';

  // 设置频率按钮状态
  document.querySelectorAll('.freq-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.freq === 'daily');
  });

  try {
    const data = await apiFetch(`/api/industry/detail/${encodeURIComponent(industryName)}`);

    createChart();

    // K线数据
    if (data.ohlcv && data.ohlcv.length > 0) {
      candleSeries.setData(data.ohlcv);

      const c = chartColors();
      const volData = data.ohlcv.map(bar => ({
        time: bar.time,
        value: bar.volume,
        color: bar.close >= bar.open ? c.volUp : c.volDown,
      }));
      volumeSeries.setData(volData);
    }

    // CZSC 叠加
    drawBiLines(data.bi_list);
    drawZhongshu(data.zhongshu);
    drawSignalMarkers(data.signals);

    // MA 均线叠加
    drawMALines(data.ma_lines);

    // MACD 子图
    drawMACD(data.macd);

    // 行业信息面板
    renderIndustryInfo(data);

    // 信号详情
    renderSignalDetails(data);

    // 摘要栏（复用指数摘要逻辑）
    renderChartSummary(data);

    chart.timeScale().fitContent();

  } catch (err) {
    console.error('Industry chart load failed:', err);
    document.getElementById('chart-title').textContent =
      industryName + ' - 加载失败: ' + err.message;
  }
}

// 返回 dashboard 时重置行业模式
const origChartBack = document.getElementById('chart-back');
if (origChartBack) {
  origChartBack.addEventListener('click', () => {
    if (isIndustryMode) {
      isIndustryMode = false;
      const freqGroup = document.querySelector('.freq-group');
      if (freqGroup) freqGroup.style.display = '';
      const detail = document.getElementById('industry-detail');
      if (detail) detail.style.display = 'none';
    }
  });
}

window.loadIndustryChart = loadIndustryChart;
