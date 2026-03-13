/* ═══════════════════════════════════════════════════
   MACD 回测页面 — backtest.js
   迁移自 signals/web/static/js/backtest.js
   ═══════════════════════════════════════════════════ */

let _btChart = null;
let _btCandleSeries = null;
let _btVolumeSeries = null;
let _btMacdBarSeries = null;
let _btMacdDifSeries = null;
let _btMacdDeaSeries = null;
let _btBiSeries = null;
let _btMaSeries = [];
let _btLoaded = false;

// ── 图表配色 ────────────────────────────────────────
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

// ── 页面初始化 ──────────────────────────────────────
onPageLoad('backtest', () => {
  if (_btLoaded) return;
  _btLoaded = true;
  _initBtEvents();
});

function _initBtEvents() {
  document.getElementById('bt-run')?.addEventListener('click', _runBacktest);
  document.getElementById('bt-code')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') _runBacktest();
  });
}

// ── 主运行逻辑 ──────────────────────────────────────
async function _runBacktest() {
  const code = document.getElementById('bt-code').value.trim();
  if (!code) return;

  const freq = document.getElementById('bt-freq').value;
  const signalGroup = document.getElementById('bt-group').value;
  const runBtn = document.getElementById('bt-run');

  runBtn.disabled = true;
  runBtn.textContent = '加载中...';

  try {
    const params = new URLSearchParams({ code, freq, signal_group: signalGroup });
    const data = await apiFetch('/api/backtest/run?' + params.toString());

    if (data.error) {
      showToast(data.error);
      return;
    }

    showToast(`${data.symbol} ${data.freq} — ${data.signals.length} 信号`);

    _createBtChart(data);
    _renderKPI(data.kpi);
    _renderSignalTable(data.signals);
    _renderDatePresets(data.date_presets);
  } catch (e) {
    console.error('Backtest error:', e);
    showToast('失败: ' + e.message);
  } finally {
    runBtn.disabled = false;
    runBtn.textContent = '运行回测';
  }
}

// ── 图表创建 ─────────────────────────────────────────
function _createBtChart(data) {
  const container = document.getElementById('bt-chart-container');
  const c = chartColors();

  if (_btChart) { _btChart.remove(); _btChart = null; }
  container.innerHTML = '';
  _btBiSeries = null;
  _btMaSeries = [];

  _btChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 520,
    layout: {
      background: { type: 'solid', color: c.bg },
      textColor: c.text,
    },
    grid: {
      vertLines: { color: c.grid },
      horzLines: { color: c.grid },
    },
    crosshair: {
      vertLine: { color: c.crosshair, width: 1, style: 2 },
      horzLine: { color: c.crosshair, width: 1, style: 2 },
    },
    timeScale: { borderColor: c.grid, timeVisible: false },
    rightPriceScale: { borderColor: c.grid },
  });

  // K线
  _btCandleSeries = _btChart.addCandlestickSeries({
    upColor: c.upColor, downColor: c.downColor,
    borderUpColor: c.upColor, borderDownColor: c.downColor,
    wickUpColor: c.upColor, wickDownColor: c.downColor,
    priceScaleId: 'right',
  });

  // 成交量
  _btVolumeSeries = _btChart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  _btChart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.72, bottom: 0.18 },
  });

  if (data.ohlcv && data.ohlcv.length > 0) {
    _btCandleSeries.setData(data.ohlcv);
    _btVolumeSeries.setData(data.ohlcv.map(bar => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? c.volUp : c.volDown,
    })));
  }

  _drawBtMALines(data.ma_lines);
  if (data.bi_list?.length > 0) _drawBtBiLines(data.bi_list);
  if (data.zhongshu?.length > 0) _drawBtZhongshu(data.zhongshu);
  _drawBtMACD(data.macd);
  _drawBtSignalMarkers(data.signals);

  const ro = new ResizeObserver(() => {
    if (_btChart) _btChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);

  _btChart.timeScale().fitContent();
}

// ── MA 均线 ──────────────────────────────────────────
function _drawBtMALines(maLines) {
  if (!maLines?.length) return;
  maLines.forEach(ma => {
    if (!ma.data || ma.data.length < 2) return;
    const series = _btChart.addLineSeries({
      color: ma.color, lineWidth: 1, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false,
      lastValueVisible: false, priceScaleId: '',
    });
    series.setData(ma.data);
    _btMaSeries.push(series);
  });
}

// ── 笔线 ────────────────────────────────────────────
function _drawBtBiLines(biList) {
  if (!biList?.length) return;
  const c = chartColors();

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
    } else {
      merged.push(p);
    }
  });

  if (merged.length < 2) return;

  _btBiSeries = _btChart.addLineSeries({
    color: c.biUp, lineWidth: 2, lineStyle: 0,
    crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
  });
  _btBiSeries.setData(merged);
}

// ── 中枢 ────────────────────────────────────────────
function _drawBtZhongshu(zhongshuList) {
  if (!zhongshuList?.length) return;
  const c = chartColors();

  zhongshuList.forEach(zs => {
    const opts = {
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    };

    // 上沿
    const upper = _btChart.addLineSeries(opts);
    upper.setData([
      { time: zs.start_dt, value: zs.zg },
      { time: zs.end_dt, value: zs.zg },
    ]);
    // 下沿
    const lower = _btChart.addLineSeries(opts);
    lower.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zd },
    ]);
    // 左竖线
    const left = _btChart.addLineSeries(opts);
    left.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.start_dt, value: zs.zg },
    ]);
    // 右竖线
    const right = _btChart.addLineSeries(opts);
    right.setData([
      { time: zs.end_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zg },
    ]);

    upper.setMarkers([{
      time: Math.floor((zs.start_dt + zs.end_dt) / 2),
      position: 'aboveBar', color: c.zhongshuStroke, shape: 'square',
      text: 'ZS ' + zs.bi_count + 'B',
    }]);
  });
}

// ── MACD 子图 ────────────────────────────────────────
function _drawBtMACD(macdData) {
  if (!macdData || macdData.length < 2) return;
  const c = chartColors();

  _btMacdBarSeries = _btChart.addHistogramSeries({
    priceScaleId: 'macd',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    lastValueVisible: false,
  });
  _btChart.priceScale('macd').applyOptions({
    scaleMargins: { top: 0.85, bottom: 0 },
  });
  _btMacdBarSeries.setData(macdData.map(d => ({
    time: d.time, value: d.bar,
    color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown,
  })));

  _btMacdDifSeries = _btChart.addLineSeries({
    color: c.macdDif, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DIF',
  });
  _btMacdDifSeries.setData(macdData.map(d => ({ time: d.time, value: d.dif })));

  _btMacdDeaSeries = _btChart.addLineSeries({
    color: c.macdDea, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DEA',
  });
  _btMacdDeaSeries.setData(macdData.map(d => ({ time: d.time, value: d.dea })));
}

// ── 信号箭头标记 ────────────────────────────────────
function _drawBtSignalMarkers(signals) {
  if (!_btCandleSeries || !signals?.length) return;
  const c = chartColors();

  const markers = signals.map(s => {
    let color, shape, position;

    if (s.group === 'macd') {
      const isA = s.type.includes('A_') || s.type.includes('零上');
      color = isA ? '#26a69a' : '#f7931a';
      shape = 'arrowUp';
      position = 'belowBar';
    } else {
      const isBuy = s.type.includes('买') || s.type.includes('背驰');
      const isSell = s.type.includes('卖');
      if (isSell && !isBuy) {
        color = c.signalSell; shape = 'arrowDown'; position = 'aboveBar';
      } else {
        color = c.signalBuy; shape = 'arrowUp'; position = 'belowBar';
      }
    }

    return { time: s.dt, position, color, shape, text: s.type };
  });

  markers.sort((a, b) => a.time - b.time);
  _btCandleSeries.setMarkers(markers);
}

// ── KPI 卡片 ────────────────────────────────────────
function _renderKPI(kpi) {
  const el = document.getElementById('bt-kpis');
  if (!kpi || kpi.total === 0) {
    el.innerHTML = '<div class="cl-empty">无信号数据</div>';
    return;
  }

  const items = [
    { value: kpi.total, label: '总信号', cls: '' },
    { value: (kpi.evaluated || kpi.total) + '/' + kpi.total, label: '已评估', cls: '' },
    { value: kpi.win_rate + '%', label: '胜率(T+10)', cls: kpi.win_rate >= 50 ? 'up' : 'down' },
    { value: (kpi.expectancy >= 0 ? '+' : '') + kpi.expectancy + '%', label: '期望收益', cls: kpi.expectancy >= 0 ? 'up' : 'down' },
    { value: kpi.avg_return_t10 + '%', label: '平均T+10', cls: kpi.avg_return_t10 >= 0 ? 'up' : 'down' },
    { value: '+' + (kpi.avg_mfe || 0) + '%', label: 'MFE均', cls: 'up' },
    { value: (kpi.avg_mae || 0) + '%', label: 'MAE均', cls: 'down' },
  ];

  el.innerHTML = items.map(it => `
    <div class="bt-kpi-card">
      <div class="bt-kpi-value ${it.cls}">${it.value}</div>
      <div class="bt-kpi-label">${it.label}</div>
    </div>
  `).join('');

  // 按类型分组
  if (kpi.by_type && Object.keys(kpi.by_type).length > 0) {
    let html = '<table class="bt-stats-table"><thead><tr><th>信号类型</th><th>数量</th><th>胜率</th><th>平均T+10</th></tr></thead><tbody>';
    for (const [sigType, info] of Object.entries(kpi.by_type)) {
      html += `<tr>
        <td><b>${sigType}</b></td>
        <td>${info.count}</td>
        <td class="${info.win_rate >= 50 ? 'up' : 'down'}">${info.win_rate}%</td>
        <td class="${info.avg_return_t10 >= 0 ? 'up' : 'down'}">${info.avg_return_t10 >= 0 ? '+' : ''}${info.avg_return_t10}%</td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML += html;
  }
}

// ── 信号列表表格 ────────────────────────────────────
function _renderSignalTable(signals) {
  const container = document.getElementById('bt-signal-list');

  if (!signals?.length) {
    container.innerHTML = '<div class="empty-state">无信号</div>';
    return;
  }

  let html = `<table class="bt-stats-table">
    <thead><tr>
      <th>日期</th><th>类型</th><th>组</th><th>价格</th><th>置信度</th>
      <th>T+5</th><th>T+10</th><th>T+20</th><th>MFE</th><th>MAE</th>
    </tr></thead><tbody>`;

  for (const s of signals) {
    const ev = s.eval || {};
    const t10 = ev.return_t10;
    const isWin = t10 != null && t10 > 0;
    const isLoss = t10 != null && t10 < 0;
    const rowCls = isWin ? 'bt-signal-win' : isLoss ? 'bt-signal-loss' : '';
    const groupBadge = s.group === 'macd'
      ? '<span class="bt-pattern-badge macd">MACD</span>'
      : '<span class="bt-pattern-badge czsc">缠论</span>';

    html += `<tr class="bt-signal-row ${rowCls}" data-time="${s.dt}" onclick="_btScrollTo(${s.dt})">
      <td>${s.date_str}</td>
      <td><b>${s.type}</b></td>
      <td>${groupBadge}</td>
      <td>${s.price.toFixed(2)}</td>
      <td>${s.confidence != null ? (s.confidence * 100).toFixed(0) + '%' : '—'}</td>
      <td class="${_retCls(ev.return_t5)}">${_fmtRet(ev.return_t5)}</td>
      <td class="${_retCls(ev.return_t10)}">${_fmtRet(ev.return_t10)}</td>
      <td class="${_retCls(ev.return_t20)}">${_fmtRet(ev.return_t20)}</td>
      <td class="up">${ev.mfe != null ? '+' + ev.mfe + '%' : '—'}</td>
      <td class="down">${ev.mae != null ? ev.mae + '%' : '—'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

function _fmtRet(val) {
  if (val == null) return '—';
  return (val >= 0 ? '+' : '') + val + '%';
}

function _retCls(val) {
  if (val == null) return '';
  return val >= 0 ? 'up' : 'down';
}

// ── 点击信号跳转 ────────────────────────────────────
window._btScrollTo = function (unixTime) {
  if (!_btChart) return;
  const from = unixTime - 60 * 86400;
  const to = unixTime + 30 * 86400;
  _btChart.timeScale().setVisibleRange({ from, to });
};

// ── 日期预设 chips ──────────────────────────────────
function _renderDatePresets(presets) {
  const container = document.getElementById('bt-presets');
  if (!container) return;
  if (!presets?.length) { container.innerHTML = ''; return; }

  container.innerHTML = presets.map(p =>
    `<span class="bt-preset-chip" onclick="_btScrollTo(${p.time})" title="${p.date}">${p.label}</span>`
  ).join('');
}
