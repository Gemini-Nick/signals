/**
 * 隆小侠 LONG CLAW — 回测页（重写版）
 * K线图 + MACD 子图 + MA均线 + 缠论笔/中枢 + 信号标记 + KPI + 信号表格
 * 使用 TradingView Lightweight Charts v4
 */

let _btChart = null;
let _btCandleSeries = null;
let _btVolumeSeries = null;
let _btMacdBarSeries = null;
let _btMacdDifSeries = null;
let _btMacdDeaSeries = null;
let _btBiSeries = null;
let _btMaSeries = [];
let _btLoaded = false;
let _btEquityChart = null;

// ── 页面初始化 ──────────────────────────────────────
window.loadBacktestPage = function () {
  if (_btLoaded) return;
  _btLoaded = true;
  _initBtEvents();
};

function _initBtEvents() {
  const runBtn = document.getElementById('bt-run-btn');
  const simBtn = document.getElementById('bt-sim-btn');
  const codeInput = document.getElementById('bt-code-input');

  runBtn.addEventListener('click', _runBacktest);
  simBtn.addEventListener('click', _runSimulate);
  codeInput.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') _runBacktest();
  });

  // 点击模拟按钮时展开参数面板
  simBtn.addEventListener('mouseenter', () => {
    document.getElementById('bt-sim-params').style.display = 'flex';
  });
}

// ── 主运行逻辑 ──────────────────────────────────────
async function _runBacktest() {
  const code = document.getElementById('bt-code-input').value.trim();
  if (!code) return;

  const freq = document.getElementById('bt-freq-select').value;
  const signalGroup = document.getElementById('bt-signal-group').value;
  const status = document.getElementById('bt-status');
  const runBtn = document.getElementById('bt-run-btn');

  status.textContent = '加载中...';
  status.className = 'bt-status';
  runBtn.disabled = true;

  try {
    const params = new URLSearchParams({ code, freq, signal_group: signalGroup });
    const data = await apiFetch('/api/backtest/run?' + params.toString());

    if (data.error) {
      status.textContent = data.error;
      status.className = 'bt-status error';
      return;
    }

    status.textContent = `${data.symbol} ${data.freq} — ${data.signals.length} 信号`;
    status.className = 'bt-status';

    _createBtChart(data);
    _renderKPI(data.kpi);
    _renderSignalTable(data.signals);
    _renderDatePresets(data.date_presets);

  } catch (e) {
    console.error('Backtest error:', e);
    status.textContent = '失败: ' + e.message;
    status.className = 'bt-status error';
  } finally {
    runBtn.disabled = false;
  }
}

// ── 图表创建 ─────────────────────────────────────────
function _createBtChart(data) {
  const container = document.getElementById('bt-chart-container');
  const c = chartColors();

  // 销毁旧图表
  if (_btChart) {
    _btChart.remove();
    _btChart = null;
  }
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
    timeScale: {
      borderColor: c.grid,
      timeVisible: false,
    },
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

  // 设置数据
  if (data.ohlcv && data.ohlcv.length > 0) {
    _btCandleSeries.setData(data.ohlcv);
    _btVolumeSeries.setData(data.ohlcv.map(bar => ({
      time: bar.time,
      value: bar.volume,
      color: bar.close >= bar.open ? c.volUp : c.volDown,
    })));
  }

  // MA 均线
  _drawBtMALines(data.ma_lines);

  // 缠论笔线 + 中枢
  if (data.bi_list && data.bi_list.length > 0) {
    _drawBtBiLines(data.bi_list);
  }
  if (data.zhongshu && data.zhongshu.length > 0) {
    _drawBtZhongshu(data.zhongshu);
  }

  // MACD 子图
  _drawBtMACD(data.macd);

  // 信号标记
  _drawBtSignalMarkers(data.signals);

  // 响应式
  const ro = new ResizeObserver(() => {
    if (_btChart) _btChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);

  _btChart.timeScale().fitContent();
}

// ── MA 均线 ──────────────────────────────────────────
function _drawBtMALines(maLines) {
  if (!maLines || maLines.length === 0) return;

  maLines.forEach(ma => {
    if (!ma.data || ma.data.length < 2) return;
    const series = _btChart.addLineSeries({
      color: ma.color,
      lineWidth: 1,
      lineStyle: 0,
      crosshairMarkerVisible: false,
      priceLineVisible: false,
      lastValueVisible: false,
      priceScaleId: '',
    });
    series.setData(ma.data);
    _btMaSeries.push(series);
  });
}

// ── 笔线 ────────────────────────────────────────────
function _drawBtBiLines(biList) {
  if (!biList || biList.length === 0) return;
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

  // 去重 + 排序 + 合并
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
    color: c.biUp,
    lineWidth: 2,
    lineStyle: 0,
    crosshairMarkerVisible: false,
    priceLineVisible: false,
    lastValueVisible: false,
  });
  _btBiSeries.setData(merged);
}

// ── 中枢 ────────────────────────────────────────────
function _drawBtZhongshu(zhongshuList) {
  if (!zhongshuList || zhongshuList.length === 0) return;
  const c = chartColors();

  zhongshuList.forEach(zs => {
    // 上沿
    const upper = _btChart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    upper.setData([
      { time: zs.start_dt, value: zs.zg },
      { time: zs.end_dt, value: zs.zg },
    ]);

    // 下沿
    const lower = _btChart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    lower.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zd },
    ]);

    // 左竖线
    const left = _btChart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    left.setData([
      { time: zs.start_dt, value: zs.zd },
      { time: zs.start_dt, value: zs.zg },
    ]);

    // 右竖线
    const right = _btChart.addLineSeries({
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    });
    right.setData([
      { time: zs.end_dt, value: zs.zd },
      { time: zs.end_dt, value: zs.zg },
    ]);

    // 中枢标记
    upper.setMarkers([{
      time: Math.floor((zs.start_dt + zs.end_dt) / 2),
      position: 'aboveBar',
      color: c.zhongshuStroke,
      shape: 'square',
      text: 'ZS ' + zs.bi_count + 'B',
    }]);
  });
}

// ── MACD 子图 ────────────────────────────────────────
function _drawBtMACD(macdData) {
  if (!macdData || macdData.length < 2) return;
  const c = chartColors();

  // MACD 柱
  _btMacdBarSeries = _btChart.addHistogramSeries({
    priceScaleId: 'macd',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    lastValueVisible: false,
  });
  _btChart.priceScale('macd').applyOptions({
    scaleMargins: { top: 0.85, bottom: 0 },
  });
  _btMacdBarSeries.setData(macdData.map(d => ({
    time: d.time,
    value: d.bar,
    color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown,
  })));

  // DIF
  _btMacdDifSeries = _btChart.addLineSeries({
    color: c.macdDif, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd',
    crosshairMarkerVisible: false, priceLineVisible: false,
    lastValueVisible: false, title: 'DIF',
  });
  _btMacdDifSeries.setData(macdData.map(d => ({ time: d.time, value: d.dif })));

  // DEA
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
  if (!_btCandleSeries || !signals || signals.length === 0) return;

  const c = chartColors();
  const markers = signals.map(s => {
    let color, shape, position;

    if (s.group === 'macd') {
      // MACD A(零上回踩) → 绿色, B(零下企稳) → 橙色
      const isA = s.type.includes('A_') || s.type.includes('零上');
      color = isA ? '#26a69a' : '#f7931a';
      shape = 'arrowUp';
      position = 'belowBar';
    } else {
      // 缠论信号
      const isBuy = s.type.includes('买') || s.type.includes('背驰');
      const isSell = s.type.includes('卖');
      if (isSell && !isBuy) {
        color = c.signalSell;
        shape = 'arrowDown';
        position = 'aboveBar';
      } else {
        color = c.signalBuy;
        shape = 'arrowUp';
        position = 'belowBar';
      }
    }

    return {
      time: s.dt,
      position: position,
      color: color,
      shape: shape,
      text: s.type,
    };
  });

  markers.sort((a, b) => a.time - b.time);
  _btCandleSeries.setMarkers(markers);
}

// ── KPI 卡片 ────────────────────────────────────────
function _renderKPI(kpi) {
  const el = document.getElementById('bt-kpis');
  if (!kpi || kpi.total === 0) {
    el.innerHTML = '<div class="empty-state">无信号数据</div>';
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

  // 按类型分组 KPI
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
  const countEl = document.getElementById('bt-signal-count');

  if (!signals || signals.length === 0) {
    container.innerHTML = '<div class="bt-empty-msg">无信号</div>';
    countEl.textContent = '';
    return;
  }

  countEl.textContent = `(${signals.length})`;

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
  _btChart.timeScale().scrollToPosition(-5, false);

  // 使用 setVisibleRange 定位到信号附近
  const from = unixTime - 60 * 86400;  // 前60天
  const to = unixTime + 30 * 86400;    // 后30天
  _btChart.timeScale().setVisibleRange({ from, to });
};

// ── 日期预设 chips ──────────────────────────────────
function _renderDatePresets(presets) {
  const container = document.getElementById('bt-preset-chips');
  if (!presets || presets.length === 0) {
    container.innerHTML = '';
    return;
  }

  container.innerHTML = presets.map(p =>
    `<span class="bt-preset-chip" onclick="_btScrollTo(${p.time})" title="${p.date}">${p.label}</span>`
  ).join('');
}

// ── 模拟交易逻辑 ──────────────────────────────────────
async function _runSimulate() {
  const code = document.getElementById('bt-code-input').value.trim();
  if (!code) return;

  const freq = document.getElementById('bt-freq-select').value;
  const signalGroup = document.getElementById('bt-signal-group').value;
  const status = document.getElementById('bt-status');
  const simBtn = document.getElementById('bt-sim-btn');

  // 展开参数面板
  document.getElementById('bt-sim-params').style.display = 'flex';

  const stopLoss = document.getElementById('bt-stop-loss').value;
  const trailStop = document.getElementById('bt-trail-stop').value;
  const maxHold = document.getElementById('bt-max-hold').value;
  const slippage = document.getElementById('bt-slippage').value;

  status.textContent = '模拟中...';
  status.className = 'bt-status';
  simBtn.disabled = true;

  try {
    const params = new URLSearchParams({
      code, freq, signal_group: signalGroup,
      stop_loss: stopLoss, trail_stop: trailStop,
      max_hold: maxHold, slippage: slippage,
    });
    const data = await apiFetch('/api/backtest/simulate?' + params.toString());

    if (data.error) {
      status.textContent = data.error;
      status.className = 'bt-status error';
      return;
    }

    const filledCount = (data.sim_kpi || {}).filled_trades || 0;
    status.textContent = `${data.symbol} ${data.freq} — ${data.signals.length} 信号, ${filledCount} 笔成交`;
    status.className = 'bt-status';

    // 渲染K线图 (复用现有逻辑)
    _createBtChart(data);
    _renderKPI(data.forward_kpi);
    _renderSignalTable(data.signals);
    _renderDatePresets(data.date_presets);

    // 渲染模拟结果
    _renderSimResults(data);

  } catch (e) {
    console.error('Simulate error:', e);
    status.textContent = '模拟失败: ' + e.message;
    status.className = 'bt-status error';
  } finally {
    simBtn.disabled = false;
  }
}

function _renderSimResults(data) {
  const section = document.getElementById('bt-sim-section');
  section.style.display = 'block';

  // 模拟 KPI
  _renderSimKPI(data.sim_kpi || {});

  // 资金曲线
  _renderEquityCurve(data.sim_equity || []);

  // 交易记录表
  _renderTradeTable(data.sim_trades || []);

  // 跳过原因
  _renderSkipReasons(data.sim_skip_reasons || {});
}

function _renderSimKPI(kpi) {
  const el = document.getElementById('bt-sim-kpis');
  if (!kpi || kpi.total_trades === 0) {
    el.innerHTML = '<div class="empty-state">无成交记录</div>';
    return;
  }

  const items = [
    { value: kpi.filled_trades, label: '成交笔数', cls: '' },
    { value: kpi.win_rate + '%', label: '胜率', cls: kpi.win_rate >= 50 ? 'up' : 'down' },
    { value: (kpi.total_return_pct >= 0 ? '+' : '') + kpi.total_return_pct + '%', label: '总收益', cls: kpi.total_return_pct >= 0 ? 'up' : 'down' },
    { value: kpi.sharpe, label: 'Sharpe', cls: kpi.sharpe >= 1 ? 'up' : kpi.sharpe < 0 ? 'down' : '' },
    { value: kpi.sortino, label: 'Sortino', cls: kpi.sortino >= 1 ? 'up' : kpi.sortino < 0 ? 'down' : '' },
    { value: kpi.profit_factor, label: '盈亏比', cls: kpi.profit_factor >= 1 ? 'up' : 'down' },
    { value: '-' + kpi.max_drawdown_pct + '%', label: '最大回撤', cls: 'down' },
    { value: (kpi.expectancy >= 0 ? '+' : '') + kpi.expectancy + '%', label: '期望', cls: kpi.expectancy >= 0 ? 'up' : 'down' },
    { value: kpi.avg_hold_days + 'D', label: '平均持仓', cls: '' },
    { value: '+' + kpi.avg_mfe + '%', label: 'MFE均', cls: 'up' },
    { value: kpi.avg_mae + '%', label: 'MAE均', cls: 'down' },
    { value: kpi.avg_cost_pct + '%', label: '成本均', cls: '' },
  ];

  el.innerHTML = items.map(it => `
    <div class="bt-kpi-card">
      <div class="bt-kpi-value ${it.cls}">${it.value}</div>
      <div class="bt-kpi-label">${it.label}</div>
    </div>
  `).join('');
}

function _renderEquityCurve(equity) {
  const container = document.getElementById('bt-equity-container');
  if (_btEquityChart) {
    _btEquityChart.remove();
    _btEquityChart = null;
  }
  container.innerHTML = '';

  if (!equity || equity.length < 2) {
    container.innerHTML = '<div class="empty-state">资金曲线数据不足</div>';
    return;
  }

  const c = chartColors();
  _btEquityChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 250,
    layout: { background: { type: 'solid', color: c.bg }, textColor: c.text },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    timeScale: { borderColor: c.grid, timeVisible: false },
    rightPriceScale: { borderColor: c.grid },
  });

  const areaSeries = _btEquityChart.addAreaSeries({
    topColor: 'rgba(38, 166, 154, 0.28)',
    bottomColor: 'rgba(38, 166, 154, 0.02)',
    lineColor: '#26a69a',
    lineWidth: 2,
    priceLineVisible: false,
    lastValueVisible: true,
  });
  areaSeries.setData(equity);
  _btEquityChart.timeScale().fitContent();

  const ro = new ResizeObserver(() => {
    if (_btEquityChart) _btEquityChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}

function _renderTradeTable(trades) {
  const container = document.getElementById('bt-trade-list');
  const countEl = document.getElementById('bt-trade-count');

  const filled = trades.filter(t => t.entry_price != null);
  const skipped = trades.filter(t => t.skip_reason != null);

  countEl.textContent = `(${filled.length} 成交 / ${skipped.length} 跳过)`;

  if (filled.length === 0) {
    container.innerHTML = '<div class="bt-empty-msg">无成交记录</div>';
    return;
  }

  let html = `<table class="bt-stats-table">
    <thead><tr>
      <th>信号日</th><th>类型</th><th>入场日</th><th>入场价</th><th>成交方式</th>
      <th>出场日</th><th>出场价</th><th>出场原因</th><th>持仓天</th>
      <th>毛利</th><th>净利</th><th>成本</th><th>MFE</th><th>MAE</th>
    </tr></thead><tbody>`;

  for (const t of filled) {
    const retCls = (t.net_return_pct || 0) >= 0 ? 'up' : 'down';
    const rowCls = (t.net_return_pct || 0) >= 0 ? 'bt-signal-win' : 'bt-signal-loss';
    const exitLabel = {
      'stop_loss': '止损', 'trail_stop': '移动止盈', 'time_exit': '时间止损',
      'signal_exit': '信号出场', 'data_end': '数据终点',
    }[t.exit_reason] || t.exit_reason || '—';
    const fillLabel = t.fill_type === 'open_fill' ? '开盘成交' : t.fill_type === 'trigger_fill' ? '触发成交' : t.fill_type || '—';

    html += `<tr class="bt-signal-row ${rowCls}" onclick="_btScrollTo(${new Date(t.signal_date).getTime() / 1000 | 0})">
      <td>${t.signal_date || '—'}</td>
      <td><b>${t.signal_type}</b></td>
      <td>${t.entry_date || '—'}</td>
      <td>${t.entry_price != null ? t.entry_price.toFixed(2) : '—'}</td>
      <td>${fillLabel}</td>
      <td>${t.exit_date || '—'}</td>
      <td>${t.exit_price != null ? t.exit_price.toFixed(2) : '—'}</td>
      <td>${exitLabel}</td>
      <td>${t.holding_days != null ? t.holding_days : '—'}</td>
      <td class="${retCls}">${_fmtRet(t.return_pct)}</td>
      <td class="${retCls}"><b>${_fmtRet(t.net_return_pct)}</b></td>
      <td>${t.cost_pct != null ? t.cost_pct + '%' : '—'}</td>
      <td class="up">${t.mfe_pct != null ? '+' + t.mfe_pct + '%' : '—'}</td>
      <td class="down">${t.mae_pct != null ? t.mae_pct + '%' : '—'}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

function _renderSkipReasons(skipReasons) {
  const el = document.getElementById('bt-skip-reasons');
  const keys = Object.keys(skipReasons);
  if (keys.length === 0) { el.innerHTML = ''; return; }

  const labels = {
    'unfilled': '未触发', 'zero_volume': '零成交量', 'locked_bar': '一字板',
    'insufficient_data': '数据不足', 'date_not_found': '日期未匹配',
    'overlapping_position': '持仓重叠', 'no_exit_data': '无出场数据',
  };

  el.innerHTML = '<div style="font-size:12px;color:var(--text-secondary);margin-top:4px;">跳过原因: ' +
    keys.map(k => `${labels[k] || k} (${skipReasons[k]})`).join(' | ') + '</div>';
}

// ── 主题切换重建 ────────────────────────────────────
window.btChartInstance = {
  applyTheme: () => {
    // 主题切换时不自动重建，用户点回测按钮即可
  }
};
