/* ═══════════════════════════════════════════════════
   🐲 K线图表 + CZSC 缠论叠加 + MACD — chart.js (web2)
   所有全局变量加 _chart 前缀避免与 backtest.js 冲突
   ═══════════════════════════════════════════════════ */

let _chartInst = null;
let _chartCandle = null;
let _chartVolume = null;
let _chartBi = null;
let _chartMaSeries = [];
let _chartMacdDif = null;
let _chartMacdDea = null;
let _chartMacdBar = null;
let _chartSymbol = '';
let _chartFreq = 'daily';
let _chartLoaded = false;
let _chartMaLegendData = [];

// ── 页面注册 ──────────────────────────────────────
onPageLoad('chart', () => {
  if (!_chartLoaded) {
    _chartLoaded = true;
    _initChartEvents();
  }
});

function _initChartEvents() {
  // 周期切换
  document.querySelectorAll('.chart-freq-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (_chartSymbol) _loadChart(_chartSymbol, btn.dataset.freq);
    });
  });

  // 指数选择
  document.querySelectorAll('.chart-index-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      _loadChart(btn.dataset.name, _chartFreq);
    });
  });
}

// ── 主题颜色 ──────────────────────────────────────
function _chartColors() {
  return {
    bg: cssVar('--chart-bg') || '#0c0e18',
    grid: cssVar('--chart-grid') || '#141824',
    text: cssVar('--text-secondary') || '#7a8098',
    crosshair: cssVar('--chart-crosshair') || '#464d64',
    upColor: cssVar('--color-up') || '#e8384f',
    downColor: cssVar('--color-down') || '#2d8a6e',
    biUp: cssVar('--bi-up') || '#e8384f',
    biDown: cssVar('--bi-down') || '#2d8a6e',
    signalBuy: cssVar('--signal-buy') || '#e8a33e',
    signalSell: cssVar('--signal-sell') || '#8e44ad',
    volUp: cssVar('--vol-up') || 'rgba(232,56,79,0.45)',
    volDown: cssVar('--vol-down') || 'rgba(45,138,110,0.45)',
    zhongshuStroke: cssVar('--zhongshu-stroke') || 'rgba(59,125,255,0.5)',
    macdDif: cssVar('--macd-dif') || '#e8a33e',
    macdDea: cssVar('--macd-dea') || '#3b7dff',
    macdBarUp: cssVar('--macd-bar-up') || 'rgba(232,56,79,0.65)',
    macdBarDown: cssVar('--macd-bar-down') || 'rgba(45,138,110,0.65)',
  };
}

// ── 创建图表 ──────────────────────────────────────
function _createChartInst() {
  const container = document.getElementById('chart-main-container');
  const c = _chartColors();

  if (_chartInst) { _chartInst.remove(); _chartInst = null; }
  container.innerHTML = '';
  _chartBi = null;
  _chartMaSeries = [];
  _chartMacdDif = null;
  _chartMacdDea = null;
  _chartMacdBar = null;
  _chartMaLegendData = [];

  _chartInst = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 580,
    layout: {
      background: { type: 'solid', color: c.bg },
      textColor: c.text,
      fontFamily: cssVar('--font-mono') || 'monospace',
    },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    crosshair: {
      vertLine: { color: c.crosshair, width: 1, style: 2 },
      horzLine: { color: c.crosshair, width: 1, style: 2 },
    },
    timeScale: { borderColor: c.grid, timeVisible: true, secondsVisible: false },
    rightPriceScale: { borderColor: c.grid },
  });

  _chartCandle = _chartInst.addCandlestickSeries({
    upColor: c.upColor, downColor: c.downColor,
    borderUpColor: c.upColor, borderDownColor: c.downColor,
    wickUpColor: c.upColor, wickDownColor: c.downColor,
    priceScaleId: 'right',
  });

  _chartVolume = _chartInst.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  _chartInst.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.72, bottom: 0.18 },
  });

  const ro = new ResizeObserver(() => {
    if (_chartInst) _chartInst.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}

// ── 绘制笔线 ──────────────────────────────────────
function _drawChartBi(biList) {
  if (!biList?.length) return;
  const c = _chartColors();
  const points = [];
  biList.forEach(bi => {
    if (bi.direction === 'up') {
      points.push({ time: bi.sdt, value: bi.low });
      points.push({ time: bi.edt, value: bi.high });
    } else {
      points.push({ time: bi.sdt, value: bi.high });
      points.push({ time: bi.edt, value: bi.low });
    }
  });
  const seen = new Set();
  const unique = [];
  points.forEach(p => {
    const key = p.time + '_' + p.value;
    if (!seen.has(key)) { seen.add(key); unique.push(p); }
  });
  unique.sort((a, b) => a.time - b.time);
  const merged = [];
  unique.forEach(p => {
    if (merged.length > 0 && merged[merged.length - 1].time === p.time) {
      merged[merged.length - 1] = p;
    } else { merged.push(p); }
  });
  if (merged.length < 2) return;
  _chartBi = _chartInst.addLineSeries({
    color: c.biUp, lineWidth: 2, lineStyle: 0,
    crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
  });
  _chartBi.setData(merged);
}

// ── 绘制中枢 ──────────────────────────────────────
function _drawChartZhongshu(zsList) {
  if (!zsList?.length) return;
  const c = _chartColors();
  zsList.forEach(zs => {
    const opts = {
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    };
    const upper = _chartInst.addLineSeries(opts);
    upper.setData([{ time: zs.start_dt, value: zs.zg }, { time: zs.end_dt, value: zs.zg }]);
    const lower = _chartInst.addLineSeries(opts);
    lower.setData([{ time: zs.start_dt, value: zs.zd }, { time: zs.end_dt, value: zs.zd }]);
    const left = _chartInst.addLineSeries(opts);
    left.setData([{ time: zs.start_dt, value: zs.zd }, { time: zs.start_dt, value: zs.zg }]);
    const right = _chartInst.addLineSeries(opts);
    right.setData([{ time: zs.end_dt, value: zs.zd }, { time: zs.end_dt, value: zs.zg }]);
    upper.setMarkers([{
      time: Math.floor((zs.start_dt + zs.end_dt) / 2),
      position: 'aboveBar', color: c.zhongshuStroke, shape: 'square',
      text: 'ZS ' + zs.bi_count + 'B',
    }]);
  });
}

// ── MA 均线 + 图例 ────────────────────────────────
function _drawChartMA(maLines) {
  if (!maLines?.length) return;
  _chartMaLegendData = [];
  maLines.forEach(ma => {
    if (!ma.data || ma.data.length < 2) return;
    const series = _chartInst.addLineSeries({
      color: ma.color, lineWidth: 1, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false,
      lastValueVisible: false, priceScaleId: '',
    });
    series.setData(ma.data);
    _chartMaSeries.push(series);
    _chartMaLegendData.push({
      label: ma.label, color: ma.color, series,
      lastValue: ma.data[ma.data.length - 1].value,
    });
  });

  // MA 图例
  const container = document.getElementById('chart-main-container');
  let legend = document.getElementById('chart-ma-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.id = 'chart-ma-legend';
    legend.className = 'ma-legend';
    container.appendChild(legend);
  }
  _updateChartMALegend(null);
  _chartInst.subscribeCrosshairMove(_updateChartMALegend);
}

function _updateChartMALegend(param) {
  const legend = document.getElementById('chart-ma-legend');
  if (!legend || _chartMaLegendData.length === 0) return;
  legend.innerHTML = _chartMaLegendData.map(item => {
    let val = '—';
    if (param?.seriesData) {
      const d = param.seriesData.get(item.series);
      if (d?.value !== undefined) val = d.value.toFixed(0);
    } else if (item.lastValue != null) {
      val = item.lastValue.toFixed(0);
    }
    return `<span style="color:${item.color};font-size:11px;font-family:var(--font-mono)">${item.label}: ${val}</span>`;
  }).join(' <span style="color:var(--text-muted);margin:0 4px">|</span> ');
}

// ── MACD 子图 ─────────────────────────────────────
function _drawChartMACD(macdData) {
  if (!macdData || macdData.length < 2) return;
  const c = _chartColors();
  _chartMacdBar = _chartInst.addHistogramSeries({
    priceScaleId: 'macd',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    lastValueVisible: false,
  });
  _chartInst.priceScale('macd').applyOptions({
    scaleMargins: { top: 0.85, bottom: 0 },
  });
  _chartMacdBar.setData(macdData.map(d => ({
    time: d.time, value: d.bar,
    color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown,
  })));
  _chartMacdDif = _chartInst.addLineSeries({
    color: c.macdDif, lineWidth: 1, priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DIF',
  });
  _chartMacdDif.setData(macdData.map(d => ({ time: d.time, value: d.dif })));
  _chartMacdDea = _chartInst.addLineSeries({
    color: c.macdDea, lineWidth: 1, priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DEA',
  });
  _chartMacdDea.setData(macdData.map(d => ({ time: d.time, value: d.dea })));
}

// ── 信号标记 ──────────────────────────────────────
function _drawChartSignals(signals) {
  if (!_chartCandle || !signals?.length) return;
  const c = _chartColors();
  const markers = signals.map(s => {
    const isBuy = s.type.includes('买');
    const isDivergence = s.type.includes('背驰');
    let color, shape;
    if (isDivergence && isBuy) { color = '#00e676'; shape = 'arrowUp'; }
    else if (isDivergence && !isBuy) { color = '#ff1744'; shape = 'arrowDown'; }
    else { color = isBuy ? c.signalBuy : c.signalSell; shape = isBuy ? 'arrowUp' : 'arrowDown'; }
    return { time: s.dt, position: isBuy ? 'belowBar' : 'aboveBar', color, shape, text: s.type };
  });
  markers.sort((a, b) => a.time - b.time);
  _chartCandle.setMarkers(markers);
}

// ── 信号详情面板 ──────────────────────────────────
function _renderChartDetails(data) {
  const body = document.getElementById('chart-details-body');
  if (!body) return;
  const { signals, report, zhongshu, report_signals } = data;
  let html = '';

  if (report?.conclusion) {
    html += `<div class="detail-conclusion">${report.conclusion}</div>`;
  }
  if (report?.daily_trend) {
    const cls = t => t === '上涨趋势' ? 'up' : t === '下跌趋势' ? 'down' : 'flat';
    html += `<div class="detail-trends">
      <span class="trend-chip ${cls(report.daily_trend)}">日线: ${report.daily_trend}</span>
      <span class="trend-chip ${cls(report.f30_trend)}">30M: ${report.f30_trend}</span>
      <span class="trend-chip ${cls(report.f15_trend)}">15M: ${report.f15_trend}</span>
    </div>
    ${report.three_level_aligned ? '<div class="detail-highlight">三级共振</div>' : ''}`;
  }
  if (report?.ma_trend) {
    html += `<div class="ma-trend-chip ${report.ma_trend === '多头排列' ? 'up' : report.ma_trend === '空头排列' ? 'down' : 'flat'}">${report.ma_trend}</div>`;
  }
  if (report_signals?.length) {
    html += '<div class="tf-signals">' + report_signals.map(rs =>
      `<span class="tf-signal-chip ${rs.type.includes('买') ? 'buy' : 'sell'}">${rs.freq}: ${rs.type}</span>`
    ).join('') + '</div>';
  }
  if (signals?.length) {
    html += `<div class="signal-count">${signals.length} 信号</div>`;
    signals.forEach(s => {
      const isBuy = s.type.includes('买');
      html += `<div class="signal-item ${isBuy ? 'buy' : 'sell'}">
        <span>[${s.freq}]</span> <b>${s.type}</b>
        <span class="mono">@ ${s.price?.toFixed(2) || '—'}</span>
        <span class="conf">conf ${s.confidence != null ? (s.confidence * 100).toFixed(0) : '—'}%</span>
      </div>`;
    });
  }
  if (report?.summary) {
    html += `<div class="chart-summary-text">${report.summary}</div>`;
  }
  body.innerHTML = html || '<div style="color:var(--text-muted)">无信号</div>';
}

// ── 加载图表 ──────────────────────────────────────
async function _loadChart(indexName, freq) {
  _chartSymbol = indexName;
  _chartFreq = freq || 'daily';

  document.getElementById('chart-title').textContent = indexName;
  document.querySelectorAll('.chart-freq-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.freq === _chartFreq);
  });
  document.querySelectorAll('.chart-index-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.name === indexName);
  });

  try {
    const data = await apiFetch(`/api/chart/${encodeURIComponent(indexName)}?freq=${_chartFreq}`);
    _createChartInst();

    if (data.ohlcv?.length) {
      const c = _chartColors();
      _chartCandle.setData(data.ohlcv);
      _chartVolume.setData(data.ohlcv.map(bar => ({
        time: bar.time, value: bar.volume,
        color: bar.close >= bar.open ? c.volUp : c.volDown,
      })));
    }

    _drawChartBi(data.bi_list);
    _drawChartZhongshu(data.zhongshu);
    _drawChartSignals(data.signals);
    _drawChartMA(data.ma_lines);
    _drawChartMACD(data.macd);
    _renderChartDetails(data);
    _chartInst.timeScale().fitContent();

  } catch (err) {
    console.error('Chart load failed:', err);
    showToast('图表加载失败: ' + err.message);
  }
}

window._loadChart = _loadChart;
