/* ═══════════════════════════════════════════════════
   🐲 回测工作台 — backtest.js
   Phase 1: Tab化 + 布局修复
   Phase 2: 入场因子
   Phase 3: 高级出场
   Phase 4: 参数扫描可视化
   Phase 5: 导出 + 打磨
   Phase 6: 日期区间联动
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
let _btEquityChart = null;
let _btScanChart = null;
let _btLastData = null;  // 缓存最后一次回测数据用于导出

// Phase 6: 区间联动状态
let _btFullData = null;     // 完整 API 返回数据（不变）
let _btActiveRange = null;  // { from, to, label } | null = 全部
let _btDatePresets = [];    // preset 列表缓存
let _btSignals = [];        // 信号缓存

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
  // 主要按钮
  document.getElementById('bt-analyze')?.addEventListener('click', _runAnalyze);
  document.getElementById('bt-scan-run')?.addEventListener('click', _runScan);
  document.getElementById('bt-export')?.addEventListener('click', _exportCSV);
  document.getElementById('bt-push-wx')?.addEventListener('click', _pushToWeChat);
  document.getElementById('bt-code')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') _runAnalyze();
  });

  // 信号类型选择联动
  document.getElementById('bt-signal-type')?.addEventListener('change', _onSignalTypeChange);

  // 分批出场开关
  document.getElementById('bt-batch-exit')?.addEventListener('change', (e) => {
    document.querySelectorAll('.bt-batch-fields').forEach(el => {
      el.style.display = e.target.checked ? 'flex' : 'none';
    });
  });

  // 结果 Tab 切换
  document.querySelectorAll('.bt-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => _switchBtTab(btn.dataset.tab));
  });
}

// ── Expander 折叠 ──────────────────────────────────
window.toggleExpander = function(id) {
  const el = document.getElementById(id);
  const icon = document.getElementById(id + '-icon');
  if (!el) return;
  const isOpen = el.classList.toggle('open');
  if (icon) icon.textContent = isOpen ? '▼' : '▶';
};

// ── Tab 切换 ────────────────────────────────────────
function _switchBtTab(tabName) {
  document.querySelectorAll('.bt-tab-btn').forEach(b => b.classList.toggle('active', b.dataset.tab === tabName));
  document.querySelectorAll('.bt-tab-panel').forEach(p => p.classList.toggle('active', p.dataset.tab === tabName));
}

// ── 信号类型联动 ────────────────────────────────────
const _FACTOR_TYPES = ['gap', 'trend_breakout', 'vol_contraction'];
const _CLASSIC_TYPES = ['all', 'macd', 'czsc'];

function _getSignalType() {
  return document.getElementById('bt-signal-type').value;
}

function _onSignalTypeChange() {
  const st = _getSignalType();
  const paramsRow = document.getElementById('bt-factor-params');
  if (_FACTOR_TYPES.includes(st)) {
    paramsRow.style.display = 'flex';
    document.querySelectorAll('.bt-param-group').forEach(g => {
      g.classList.toggle('active', g.dataset.factor === st);
    });
  } else {
    paramsRow.style.display = 'none';
  }
}

// ── 收集信号参数 → signal_group + factor ─────────────
function _collectSignalParams() {
  const st = _getSignalType();
  const params = {};
  if (_CLASSIC_TYPES.includes(st)) {
    params.signal_group = st;
  } else {
    params.signal_group = 'all';
    params.factor = st;
    if (st === 'gap') {
      params.gap_pct_min = document.getElementById('bt-gap-pct').value;
      params.volume_ratio_min = document.getElementById('bt-gap-vol-ratio').value;
    } else if (st === 'trend_breakout') {
      params.trend_lookback = document.getElementById('bt-trend-lookback').value;
      params.volume_ratio_min = document.getElementById('bt-trend-vol-ratio').value;
    } else if (st === 'vol_contraction') {
      params.bb_period = document.getElementById('bt-bb-period').value;
      params.squeeze_threshold = document.getElementById('bt-squeeze-thresh').value;
    }
  }
  return params;
}

// ── 收集模拟参数 ────────────────────────────────────
function _collectSimParams() {
  return {
    stop_loss: document.getElementById('bt-stop-loss').value,
    trail_stop: document.getElementById('bt-trail-stop').value,
    max_hold: document.getElementById('bt-max-hold').value,
    slippage: document.getElementById('bt-slippage').value,
    take_profit: document.getElementById('bt-take-profit').value,
    ma_exit_period: document.getElementById('bt-ma-exit').value,
    profit_drawdown: document.getElementById('bt-profit-dd').value,
    batch_exit: document.getElementById('bt-batch-exit').checked ? '1' : '0',
    batch1_ratio: document.getElementById('bt-batch1-ratio').value,
    batch1_target: document.getElementById('bt-batch1-target').value,
    batch2_target: document.getElementById('bt-batch2-target').value,
  };
}

// ── 收集扫描参数 ────────────────────────────────────
function _collectScanParams() {
  const p1 = document.getElementById('bt-scan-param1').value;
  const v1 = document.getElementById('bt-scan-values1').value.trim();
  const p2 = document.getElementById('bt-scan-param2').value;
  const v2 = document.getElementById('bt-scan-values2').value.trim();
  const metric = document.getElementById('bt-scan-metric').value;
  if (!p1 || !v1) return {};
  const params = { scan_param: p1, scan_values: v1, scan_metric: metric };
  if (p2 && v2) {
    params.scan_param2 = p2;
    params.scan_values2 = v2;
  }
  return params;
}


// ═══════════════════════════════════════════════════
// 主运行逻辑
// ═══════════════════════════════════════════════════

async function _runAnalyze() {
  const code = document.getElementById('bt-code').value.trim();
  if (!code) return;

  const freq = document.getElementById('bt-freq').value;
  const btn = document.getElementById('bt-analyze');
  const statusEl = document.getElementById('bt-status');

  btn.disabled = true;
  btn.textContent = '分析中...';
  statusEl.textContent = '正在拉取数据...';

  try {
    const params = new URLSearchParams({ code, freq });
    for (const [k, v] of Object.entries(_collectSignalParams())) params.set(k, v);
    for (const [k, v] of Object.entries(_collectSimParams())) params.set(k, v);

    const data = await apiFetch('/api/backtest/analyze?' + params.toString(), 120000);

    if (data.error) {
      showToast(data.error);
      statusEl.textContent = '失败';
      return;
    }

    if (data.warnings?.length) {
      data.warnings.forEach(w => showToast('⚠️ ' + w, 5000));
    }

    _btFullData = data;
    _btLastData = data;
    _btActiveRange = null;

    const filledCount = (data.sim_kpi || {}).filled_trades || 0;
    showToast(`${data.symbol} ${data.freq} — ${data.signals.length} 信号, ${filledCount} 笔成交`);
    statusEl.textContent = `${data.signals.length} 信号 | ${filledCount} 笔成交`;

    // 渲染所有组件
    _createBtChart(data);
    _showResultArea();
    _renderKPI(data.kpi);
    _renderSignalTable(data.signals);
    _renderDatePresets(data.date_presets);

    // 模拟结果
    _renderSimKPI(data.sim_kpi || {});
    _renderEquityCurve(data.sim_equity || []);
    _renderTradeTable(data.sim_trades || []);
    _renderSkipReasons(data.sim_skip_reasons || {});

    // 清空扫描
    document.getElementById('bt-scan-best').innerHTML = '';
    document.getElementById('bt-scan-chart').innerHTML = '';
    document.getElementById('bt-scan-heatmap').innerHTML = '';
    document.getElementById('bt-scan-table').innerHTML = '<div class="empty-state">展开扫描面板 → 运行扫描</div>';

    _switchBtTab('perf');
    document.getElementById('bt-export').style.display = '';
    document.getElementById('bt-push-wx').style.display = '';

  } catch (e) {
    console.error('Analyze error:', e);
    showToast('分析失败: ' + e.message);
    statusEl.textContent = '错误';
  } finally {
    btn.disabled = false;
    btn.textContent = '运行分析';
  }
}

async function _runScan() {
  const code = document.getElementById('bt-code').value.trim();
  if (!code) { showToast('请先输入股票代码'); return; }

  const scanParams = _collectScanParams();
  if (!scanParams.scan_param || !scanParams.scan_values) {
    showToast('请选择扫描维度并填入取值');
    return;
  }

  const freq = document.getElementById('bt-freq').value;
  const btn = document.getElementById('bt-scan-run');
  const statusEl = document.getElementById('bt-status');

  btn.disabled = true;
  btn.textContent = '扫描中...';
  statusEl.textContent = '正在运行参数扫描...';

  try {
    const params = new URLSearchParams({ code, freq });
    for (const [k, v] of Object.entries(_collectSignalParams())) params.set(k, v);
    for (const [k, v] of Object.entries(_collectSimParams())) params.set(k, v);
    for (const [k, v] of Object.entries(scanParams)) params.set(k, v);

    const data = await apiFetch('/api/backtest/scan?' + params.toString(), 300000);

    if (data.error) {
      showToast('扫描失败: ' + data.error);
      statusEl.textContent = '扫描失败';
      return;
    }

    _renderScanResults(data);
    _switchBtTab('scan');
    statusEl.textContent = '扫描完成';
    showToast('参数扫描完成');

  } catch (e) {
    console.error('Scan error:', e);
    showToast('扫描失败: ' + e.message);
    statusEl.textContent = '错误';
  } finally {
    btn.disabled = false;
    btn.textContent = '运行扫描';
  }
}


// ═══════════════════════════════════════════════════
// Phase 6: 日期区间联动
// ═══════════════════════════════════════════════════

/**
 * 切换到指定区间：裁剪数据 → 重渲染 K线/KPI/信号/交易
 */
window._btSelectRange = function(unixTime, label) {
  if (!_btFullData) return;

  const from = unixTime - 30 * 86400;   // 事件前 30 天
  const to = unixTime + 60 * 86400;     // 事件后 60 天
  _btActiveRange = { from, to, label };

  _applyRange();

  // 高亮当前 chip
  document.querySelectorAll('.bt-preset-chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.key === label);
  });

  // 更新状态栏
  const statusEl = document.getElementById('bt-status');
  const rangeSignals = _filterByRange(_btFullData.signals, 'dt');
  if (statusEl) statusEl.textContent = `${rangeSignals.length} 信号 | ${label}`;
};

/**
 * 重置为全量显示
 */
window._btResetRange = function() {
  if (!_btFullData) return;
  _btActiveRange = null;

  _applyRange();

  document.querySelectorAll('.bt-preset-chip').forEach(chip => {
    chip.classList.toggle('active', chip.dataset.key === '__all__');
  });

  const statusEl = document.getElementById('bt-status');
  if (statusEl) statusEl.textContent = `${_btFullData.signals.length} 信号`;
};

/**
 * 根据当前 _btActiveRange 重新渲染所有组件
 */
function _applyRange() {
  const data = _btFullData;
  if (!data) return;

  if (!_btActiveRange) {
    // 全量模式
    _createBtChart(data);
    _renderKPI(data.forward_kpi || data.kpi);
    _renderSignalTable(data.signals);
    if (data.sim_kpi) _renderSimKPI(data.sim_kpi);
    if (data.sim_equity) _renderEquityCurve(data.sim_equity);
    if (data.sim_trades) _renderTradeTable(data.sim_trades);
    return;
  }

  // 裁剪数据
  const rangeOhlcv = data.ohlcv.filter(b => b.time >= _btActiveRange.from && b.time <= _btActiveRange.to);
  const rangeMacd = (data.macd || []).filter(b => b.time >= _btActiveRange.from && b.time <= _btActiveRange.to);
  const rangeMaLines = (data.ma_lines || []).map(ma => ({
    ...ma,
    data: (ma.data || []).filter(d => d.time >= _btActiveRange.from && d.time <= _btActiveRange.to),
  }));
  const rangeSignals = _filterByRange(data.signals, 'dt');
  const rangeBiList = _filterBiByRange(data.bi_list);
  const rangeZhongshu = _filterZhongshuByRange(data.zhongshu);

  // 构建区间数据对象
  const rangeData = {
    ...data,
    ohlcv: rangeOhlcv,
    macd: rangeMacd,
    ma_lines: rangeMaLines,
    signals: rangeSignals,
    bi_list: rangeBiList,
    zhongshu: rangeZhongshu,
    date_presets: [],  // 区间模式下不显示事件标记
  };

  // 重渲染图表
  _createBtChart(rangeData);

  // 重算 KPI（客户端）
  const rangeKpi = _computeKpiFromSignals(rangeSignals);
  _renderKPI(rangeKpi);

  // 重渲染信号表
  _renderSignalTable(rangeSignals);

  // 重渲染模拟相关（如有）
  if (data.sim_trades) {
    const rangeTrades = data.sim_trades.filter(t => {
      if (!t.signal_date) return false;
      const ts = new Date(t.signal_date).getTime() / 1000;
      return ts >= _btActiveRange.from && ts <= _btActiveRange.to;
    });
    _renderTradeTable(rangeTrades);
  }
  if (data.sim_equity) {
    const rangeEquity = data.sim_equity.filter(e => e.time >= _btActiveRange.from && e.time <= _btActiveRange.to);
    if (rangeEquity.length >= 2) _renderEquityCurve(rangeEquity);
  }
}

/** 通用时间戳范围过滤 */
function _filterByRange(arr, timeField) {
  if (!arr || !_btActiveRange) return arr || [];
  return arr.filter(item => item[timeField] >= _btActiveRange.from && item[timeField] <= _btActiveRange.to);
}

/** 笔线范围过滤：只要笔线与区间有交集 */
function _filterBiByRange(biList) {
  if (!biList?.length || !_btActiveRange) return biList || [];
  return biList.filter(bi => bi.edt >= _btActiveRange.from && bi.sdt <= _btActiveRange.to);
}

/** 中枢范围过滤：只要中枢与区间有交集 */
function _filterZhongshuByRange(zhongshuList) {
  if (!zhongshuList?.length || !_btActiveRange) return zhongshuList || [];
  return zhongshuList.filter(zs => zs.end_dt >= _btActiveRange.from && zs.start_dt <= _btActiveRange.to);
}

/**
 * 客户端 KPI 重算（从信号数组）
 */
function _computeKpiFromSignals(signals) {
  if (!signals?.length) return { total: 0, evaluated: 0, win_rate: 0, expectancy: 0, avg_return_t10: 0, avg_mfe: 0, avg_mae: 0, by_type: {} };

  const total = signals.length;
  let evaluated = 0, wins = 0, sumT10 = 0, sumMfe = 0, sumMae = 0;
  const byType = {};

  for (const s of signals) {
    const ev = s.eval || {};
    const t10 = ev.return_t10;

    // 按类型统计
    const key = s.type || 'unknown';
    if (!byType[key]) byType[key] = { count: 0, wins: 0, sumT10: 0, evaluated: 0 };
    byType[key].count++;

    if (t10 != null) {
      evaluated++;
      sumT10 += t10;
      if (t10 > 0) wins++;
      byType[key].evaluated++;
      byType[key].sumT10 += t10;
      if (t10 > 0) byType[key].wins++;
    }
    if (ev.mfe != null) sumMfe += ev.mfe;
    if (ev.mae != null) sumMae += ev.mae;
  }

  const winRate = evaluated > 0 ? Math.round(wins / evaluated * 100 * 10) / 10 : 0;
  const avgT10 = evaluated > 0 ? Math.round(sumT10 / evaluated * 100) / 100 : 0;
  const avgMfe = evaluated > 0 ? Math.round(sumMfe / evaluated * 100) / 100 : 0;
  const avgMae = evaluated > 0 ? Math.round(sumMae / evaluated * 100) / 100 : 0;
  const expectancy = evaluated > 0 ? Math.round(sumT10 / evaluated * 100) / 100 : 0;

  const by_type = {};
  for (const [sigType, info] of Object.entries(byType)) {
    by_type[sigType] = {
      count: info.count,
      win_rate: info.evaluated > 0 ? Math.round(info.wins / info.evaluated * 100 * 10) / 10 : 0,
      avg_return_t10: info.evaluated > 0 ? Math.round(info.sumT10 / info.evaluated * 100) / 100 : 0,
    };
  }

  return { total, evaluated, win_rate: winRate, expectancy, avg_return_t10: avgT10, avg_mfe: avgMfe, avg_mae: avgMae, by_type };
}

// 兼容：信号表行点击仍然可以定位到图表上的某个时间点
window._btScrollTo = function(unixTime) {
  if (!_btChart) return;
  // 如果在全量模式，局部缩放到该时间附近
  const from = unixTime - 30 * 86400;
  const to = unixTime + 15 * 86400;
  _btChart.timeScale().setVisibleRange({ from, to });
};


// ═══════════════════════════════════════════════════
// 图表
// ═══════════════════════════════════════════════════

function _showResultArea() {
  document.getElementById('bt-result-area').style.display = '';
}

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

  // 日期事件 markers（全量模式下才显示）
  _btDatePresets = (data.date_presets || []).filter(p => {
    if (!data.ohlcv?.length) return false;
    const first = data.ohlcv[0]?.time, last = data.ohlcv[data.ohlcv.length - 1]?.time;
    return p.time >= first && p.time <= last;
  });
  _drawBtSignalMarkers(data.signals);

  const ro = new ResizeObserver(() => {
    if (_btChart) _btChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);

  _btChart.timeScale().fitContent();
}

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

function _drawBtZhongshu(zhongshuList) {
  if (!zhongshuList?.length) return;
  const c = chartColors();
  zhongshuList.forEach(zs => {
    const opts = {
      color: c.zhongshuStroke, lineWidth: 2, lineStyle: 0,
      crosshairMarkerVisible: false, priceLineVisible: false, lastValueVisible: false,
    };
    const upper = _btChart.addLineSeries(opts);
    upper.setData([{ time: zs.start_dt, value: zs.zg }, { time: zs.end_dt, value: zs.zg }]);
    const lower = _btChart.addLineSeries(opts);
    lower.setData([{ time: zs.start_dt, value: zs.zd }, { time: zs.end_dt, value: zs.zd }]);
    const left = _btChart.addLineSeries(opts);
    left.setData([{ time: zs.start_dt, value: zs.zd }, { time: zs.start_dt, value: zs.zg }]);
    const right = _btChart.addLineSeries(opts);
    right.setData([{ time: zs.end_dt, value: zs.zd }, { time: zs.end_dt, value: zs.zg }]);
    upper.setMarkers([{
      time: Math.floor((zs.start_dt + zs.end_dt) / 2),
      position: 'aboveBar', color: c.zhongshuStroke, shape: 'square',
      text: 'ZS ' + zs.bi_count + 'B',
    }]);
  });
}

function _drawBtMACD(macdData) {
  if (!macdData || macdData.length < 2) return;
  const c = chartColors();
  _btMacdBarSeries = _btChart.addHistogramSeries({
    priceScaleId: 'macd',
    priceFormat: { type: 'price', precision: 4, minMove: 0.0001 },
    lastValueVisible: false,
  });
  _btChart.priceScale('macd').applyOptions({ scaleMargins: { top: 0.85, bottom: 0 } });
  _btMacdBarSeries.setData(macdData.map(d => ({
    time: d.time, value: d.bar,
    color: d.bar >= 0 ? c.macdBarUp : c.macdBarDown,
  })));
  _btMacdDifSeries = _btChart.addLineSeries({
    color: c.macdDif, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd', crosshairMarkerVisible: false,
    priceLineVisible: false, lastValueVisible: false, title: 'DIF',
  });
  _btMacdDifSeries.setData(macdData.map(d => ({ time: d.time, value: d.dif })));
  _btMacdDeaSeries = _btChart.addLineSeries({
    color: c.macdDea, lineWidth: 1, lineStyle: 0,
    priceScaleId: 'macd', crosshairMarkerVisible: false,
    priceLineVisible: false, lastValueVisible: false, title: 'DEA',
  });
  _btMacdDeaSeries.setData(macdData.map(d => ({ time: d.time, value: d.dea })));
}

// 信号类型 → 所属 group 映射
const _SIGNAL_TYPE_TO_GROUPS = {
  all: null,
  macd: ['macd'],
  czsc: ['czsc'],
  gap: ['gap'],
  trend_breakout: ['trend'],
  vol_contraction: ['vol'],
};

function _drawBtSignalMarkers(signals) {
  if (!_btCandleSeries) return;
  const c = chartColors();
  const selectedType = _getSignalType();
  const highlightGroups = _SIGNAL_TYPE_TO_GROUPS[selectedType] || null;

  const markers = [];

  // 1) 信号 markers (箭头) — 统一放 belowBar
  if (signals?.length) {
    for (const s of signals) {
      let color, shape, position;
      const isHighlighted = !highlightGroups || highlightGroups.includes(s.group);

      if (s.group === 'macd') {
        const isA = s.type.includes('A_') || s.type.includes('零上');
        color = isA ? '#26a69a' : '#f7931a';
        shape = 'arrowUp'; position = 'belowBar';
      } else if (s.group === 'gap') {
        color = '#f7931a'; shape = 'arrowUp'; position = 'belowBar';
      } else if (s.group === 'trend') {
        color = '#26a69a'; shape = 'arrowUp'; position = 'belowBar';
      } else if (s.group === 'vol') {
        color = '#e040fb'; shape = 'arrowUp'; position = 'belowBar';
      } else {
        const isBuy = s.type.includes('买') || s.type.includes('背驰');
        const isSell = s.type.includes('卖');
        if (isSell && !isBuy) {
          color = c.signalSell; shape = 'arrowDown'; position = 'aboveBar';
        } else {
          color = c.signalBuy; shape = 'arrowUp'; position = 'belowBar';
        }
      }
      if (!isHighlighted) color = color + '40';
      // 截断文字避免与日期轴重叠
      const text = isHighlighted ? (s.type.length > 8 ? s.type.slice(0, 8) : s.type) : '';
      markers.push({ time: s.dt, position, color, shape, text });
    }
  }

  // 2) 日期事件 markers (琥珀色方块) — 放 aboveBar，与信号分层
  if (_btDatePresets?.length) {
    for (const p of _btDatePresets) {
      markers.push({
        time: p.time,
        position: 'aboveBar',
        color: 'rgba(255, 193, 7, 0.9)',
        shape: 'square',
        text: p.label.split('—')[0].trim().slice(0, 4),
      });
    }
  }

  markers.sort((a, b) => a.time - b.time);
  _btCandleSeries.setMarkers(markers);
}


// ═══════════════════════════════════════════════════
// 渲染函数
// ═══════════════════════════════════════════════════

function _renderKPI(kpi) {
  const el = document.getElementById('bt-kpis');
  const byTypeEl = document.getElementById('bt-kpi-bytype');
  if (!kpi || kpi.total === 0) {
    el.innerHTML = '<div class="empty-state">无信号数据</div>';
    if (byTypeEl) byTypeEl.innerHTML = '';
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

  // 区间模式标记
  const rangeLabel = _btActiveRange ? `<div class="bt-range-badge">${_btActiveRange.label}</div>` : '';

  el.innerHTML = rangeLabel + items.map(it => `
    <div class="bt-metric-card">
      <div class="bt-metric-label">${it.label}</div>
      <div class="bt-metric-value ${it.cls}">${it.value}</div>
    </div>
  `).join('');

  // 按类型分组表
  if (byTypeEl && kpi.by_type && Object.keys(kpi.by_type).length > 0) {
    let html = '<div class="section-title" style="margin-top:16px;">按信号类型</div>';
    html += '<table class="bt-stats-table"><thead><tr><th>信号类型</th><th>数量</th><th>胜率</th><th>平均T+10</th></tr></thead><tbody>';
    for (const [sigType, info] of Object.entries(kpi.by_type)) {
      html += `<tr>
        <td><b>${sigType}</b></td>
        <td>${info.count}</td>
        <td class="${info.win_rate >= 50 ? 'up' : 'down'}">${info.win_rate}%</td>
        <td class="${info.avg_return_t10 >= 0 ? 'up' : 'down'}">${info.avg_return_t10 >= 0 ? '+' : ''}${info.avg_return_t10}%</td>
      </tr>`;
    }
    html += '</tbody></table>';

    // 按 MA 确认分组（如果后端返回了 by_ma）
    if (kpi.by_ma && Object.keys(kpi.by_ma).length > 0) {
      html += '<div class="section-title" style="margin-top:12px;">MA确认对比</div>';
      html += '<table class="bt-stats-table"><thead><tr><th>分组</th><th>数量</th><th>胜率</th><th>平均T+10</th></tr></thead><tbody>';
      for (const [label, info] of Object.entries(kpi.by_ma)) {
        html += `<tr>
          <td><b>${label}</b></td>
          <td>${info.count}</td>
          <td class="${info.win_rate >= 50 ? 'up' : 'down'}">${info.win_rate}%</td>
          <td class="${info.avg_return_t10 >= 0 ? 'up' : 'down'}">${info.avg_return_t10 >= 0 ? '+' : ''}${info.avg_return_t10}%</td>
        </tr>`;
      }
      html += '</tbody></table>';
    }

    byTypeEl.innerHTML = html;
  } else if (byTypeEl) {
    byTypeEl.innerHTML = '';
  }
}

function _renderSimKPI(kpi) {
  const el = document.getElementById('bt-sim-kpis');
  if (!kpi || !kpi.filled_trades) {
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
      <div class="bt-kpi-label">${it.label}</div>
      <div class="bt-kpi-value ${it.cls}">${it.value}</div>
    </div>
  `).join('');
}

function _renderEquityCurve(equity) {
  const container = document.getElementById('bt-equity-container');
  if (_btEquityChart) { _btEquityChart.remove(); _btEquityChart = null; }
  container.innerHTML = '';
  if (!equity || equity.length < 2) {
    container.innerHTML = '<div class="empty-state">资金曲线数据不足</div>';
    return;
  }
  const c = chartColors();
  _btEquityChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 280,
    layout: { background: { type: 'solid', color: c.bg }, textColor: c.text },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
    timeScale: { borderColor: c.grid, timeVisible: false },
    rightPriceScale: { borderColor: c.grid },
  });
  const areaSeries = _btEquityChart.addAreaSeries({
    topColor: 'rgba(38, 166, 154, 0.28)',
    bottomColor: 'rgba(38, 166, 154, 0.02)',
    lineColor: '#26a69a', lineWidth: 2,
    priceLineVisible: false, lastValueVisible: true,
  });
  areaSeries.setData(equity);
  _btEquityChart.timeScale().fitContent();
  const ro = new ResizeObserver(() => {
    if (_btEquityChart) _btEquityChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}

function _renderSignalTable(signals, filterGroups) {
  if (!filterGroups) {
    _btSignals = signals || [];
    _buildSignalFilterChips(_btSignals);
  }

  const container = document.getElementById('bt-signal-list');
  const filtered = filterGroups
    ? (signals || _btSignals).filter(s => filterGroups.includes(s.group))
    : (signals || _btSignals);

  const countEl = document.getElementById('bt-signal-count');
  if (countEl) {
    countEl.textContent = filterGroups
      ? `${filtered.length} / ${_btSignals.length} 条`
      : `${_btSignals.length} 条`;
  }

  if (!filtered?.length) {
    container.innerHTML = '<div class="empty-state">无信号</div>';
    return;
  }
  let html = `<table class="bt-stats-table">
    <thead><tr>
      <th>日期</th><th>类型</th><th>组</th><th>价格</th><th>置信度</th>
      <th>MA位置</th><th>量能</th>
      <th>T+5</th><th>T+10</th><th>T+20</th><th>MFE</th><th>MAE</th>
    </tr></thead><tbody>`;
  for (const s of filtered) {
    const ev = s.eval || {};
    const t10 = ev.return_t10;
    const isWin = t10 != null && t10 > 0;
    const isLoss = t10 != null && t10 < 0;
    const rowCls = isWin ? 'bt-signal-win' : isLoss ? 'bt-signal-loss' : '';
    const groupBadge = _groupBadge(s.group);
    const maStatus = s.ma_status || '—';
    const maCls = s.ma_confirmed ? 'up' : '';
    const volStatus = s.volume_status || '—';
    const volCls = s.vol_confirmed ? 'up' : '';
    html += `<tr class="bt-signal-row ${rowCls}" onclick="_btScrollTo(${s.dt})">
      <td>${s.date_str}</td>
      <td><b>${s.type}</b></td>
      <td>${groupBadge}</td>
      <td>${s.price.toFixed(2)}</td>
      <td>${s.confidence != null ? (s.confidence * 100).toFixed(0) + '%' : '—'}</td>
      <td class="${maCls}" style="font-size:11px;">${maStatus}</td>
      <td class="${volCls}" style="font-size:11px;">${volStatus}</td>
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

function _buildSignalFilterChips(signals) {
  const container = document.getElementById('bt-signal-filter-chips');
  if (!container) return;
  if (!signals?.length) { container.innerHTML = ''; return; }

  const groupCounts = {};
  signals.forEach(s => { groupCounts[s.group] = (groupCounts[s.group] || 0) + 1; });

  let html = `<span class="bt-filter-chip active" data-group="all" onclick="_filterSignals('all')">全部 (${signals.length})</span>`;
  for (const [group, count] of Object.entries(groupCounts)) {
    html += `<span class="bt-filter-chip" data-group="${group}" onclick="_filterSignals('${group}')">${_groupBadge(group)} (${count})</span>`;
  }
  container.innerHTML = html;
}

window._filterSignals = function(group) {
  const chips = document.querySelectorAll('.bt-filter-chip');

  if (group === 'all') {
    chips.forEach(c => c.classList.toggle('active', c.dataset.group === 'all'));
    _renderSignalTable(null, null);
    return;
  }

  const allChip = document.querySelector('.bt-filter-chip[data-group="all"]');
  if (allChip) allChip.classList.remove('active');
  const clicked = document.querySelector(`.bt-filter-chip[data-group="${group}"]`);
  if (clicked) clicked.classList.toggle('active');

  const activeGroups = [];
  chips.forEach(c => {
    if (c.dataset.group !== 'all' && c.classList.contains('active')) {
      activeGroups.push(c.dataset.group);
    }
  });

  if (activeGroups.length === 0) {
    chips.forEach(c => c.classList.toggle('active', c.dataset.group === 'all'));
    _renderSignalTable(null, null);
  } else {
    _renderSignalTable(null, activeGroups);
  }
};

function _renderTradeTable(trades) {
  const container = document.getElementById('bt-trade-list');
  const summaryEl = document.getElementById('bt-trade-summary');
  const filled = trades.filter(t => t.entry_price != null);
  const skipped = trades.filter(t => t.skip_reason != null);
  if (summaryEl) {
    summaryEl.innerHTML = `<span>✅ ${filled.length} 成交</span><span>⏭ ${skipped.length} 跳过</span>`;
  }
  if (filled.length === 0) {
    container.innerHTML = '<div class="empty-state">无成交记录</div>';
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
    const rowCls = (t.net_return_pct || 0) >= 0 ? 'signal-row win' : 'signal-row loss';
    const exitLabel = { 'stop_loss': '止损', 'trail_stop': '移动止盈', 'time_exit': '时间止损',
      'signal_exit': '信号出场', 'data_end': '数据终点', 'take_profit': '固定止盈',
      'ma_exit': '均线离场', 'profit_drawdown': '利润回撤', 'batch_exit': '分批止盈',
    }[t.exit_reason] || t.exit_reason || '—';
    const fillLabel = t.fill_type === 'open_fill' ? '开盘' : t.fill_type === 'trigger_fill' ? '触发' : t.fill_type || '—';
    html += `<tr class="${rowCls}" onclick="_btScrollTo(${new Date(t.signal_date).getTime() / 1000 | 0})">
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

function _renderDatePresets(presets) {
  const container = document.getElementById('bt-presets');
  if (!container) return;
  if (!presets?.length) { container.innerHTML = ''; return; }

  // 「全部」chip + 各事件 chip
  let html = `<span class="bt-preset-chip active" data-key="__all__" onclick="_btResetRange()">📅 全部</span>`;
  html += presets.map(p => {
    const shortLabel = p.label.split('—')[0].trim();
    return `<span class="bt-preset-chip" data-key="${p.key}" data-time="${p.time}" onclick="_btSelectRange(${p.time}, '${shortLabel}')" title="${p.date} ${p.label}">${shortLabel}</span>`;
  }).join('');
  container.innerHTML = html;
}



// ═══════════════════════════════════════════════════
// Phase 4: 参数扫描可视化
// ═══════════════════════════════════════════════════

function _renderScanResults(scan) {
  const bestEl = document.getElementById('bt-scan-best');
  const chartEl = document.getElementById('bt-scan-chart');
  const heatmapEl = document.getElementById('bt-scan-heatmap');
  const tableEl = document.getElementById('bt-scan-table');

  if (scan.best_params && Object.keys(scan.best_params).length > 0) {
    const paramStr = Object.entries(scan.best_params).map(([k, v]) => `${k}=${v}`).join(', ');
    bestEl.innerHTML = `🏆 最优参数: <b>${paramStr}</b>`;
  } else {
    bestEl.innerHTML = '';
  }

  if (scan.heatmap) {
    _renderHeatmap(heatmapEl, scan.heatmap, scan.best_params);
    chartEl.innerHTML = '';
  } else if (scan.scan_results && scan.scan_results.length > 1) {
    _renderScanLineChart(chartEl, scan.scan_results, scan.best_params);
    heatmapEl.innerHTML = '';
  } else {
    chartEl.innerHTML = '';
    heatmapEl.innerHTML = '';
  }

  if (scan.scan_results?.length > 0) {
    let html = '<table class="bt-stats-table"><thead><tr><th>参数</th><th>胜率</th><th>Sharpe</th><th>期望</th><th>总收益</th><th>盈亏比</th><th>最大回撤</th><th>笔数</th></tr></thead><tbody>';
    for (const r of scan.scan_results) {
      const paramStr = Object.entries(r.params).map(([k, v]) => `${v}`).join(' / ');
      const isBest = scan.best_params && JSON.stringify(r.params) === JSON.stringify(scan.best_params);
      html += `<tr${isBest ? ' style="background:var(--conclusion-bg);"' : ''}>
        <td><b>${paramStr}</b></td>
        <td class="${r.win_rate >= 50 ? 'up' : 'down'}">${r.win_rate}%</td>
        <td>${r.sharpe}</td>
        <td class="${r.expectancy >= 0 ? 'up' : 'down'}">${r.expectancy}%</td>
        <td class="${r.total_return >= 0 ? 'up' : 'down'}">${r.total_return}%</td>
        <td>${r.profit_factor}</td>
        <td class="down">-${r.max_drawdown}%</td>
        <td>${r.total_trades}</td>
      </tr>`;
    }
    html += '</tbody></table>';
    tableEl.innerHTML = html;
  } else {
    tableEl.innerHTML = '<div class="empty-state">无扫描结果</div>';
  }
}

function _renderHeatmap(container, hm, bestParams) {
  const { x_label, x_values, y_label, y_values, z_label, data } = hm;
  let min = Infinity, max = -Infinity;
  data.forEach(row => row.forEach(v => { if (v < min) min = v; if (v > max) max = v; }));
  const range = max - min || 1;

  let html = '<table><thead><tr><th>' + y_label + ' \\ ' + x_label + '</th>';
  x_values.forEach(x => { html += `<th>${x}</th>`; });
  html += '</tr></thead><tbody>';

  for (let yi = 0; yi < y_values.length; yi++) {
    html += `<tr><th>${y_values[yi]}</th>`;
    for (let xi = 0; xi < x_values.length; xi++) {
      const val = data[yi][xi];
      const ratio = (val - min) / range;
      const r = Math.round(255 * (1 - ratio));
      const g = Math.round(180 * ratio);
      const bgColor = `rgba(${r}, ${g + 60}, ${60}, 0.5)`;
      const isBest = bestParams &&
        bestParams[x_label] === x_values[xi] &&
        bestParams[y_label] === y_values[yi];
      html += `<td style="background:${bgColor};color:var(--text-primary);" class="${isBest ? 'best-cell' : ''}">${val}</td>`;
    }
    html += '</tr>';
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

function _renderScanLineChart(container, results, bestParams) {
  if (_btScanChart) { _btScanChart.remove(); _btScanChart = null; }
  container.innerHTML = '';

  const c = chartColors();
  _btScanChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 280,
    layout: { background: { type: 'solid', color: c.bg }, textColor: c.text },
    grid: { vertLines: { color: c.grid }, horzLines: { color: c.grid } },
  });

  const paramKey = Object.keys(results[0].params)[0];
  const lineData = results.map((r, i) => ({
    time: i + 1,
    value: r.sharpe,
  }));

  const series = _btScanChart.addLineSeries({
    color: c.macdDif, lineWidth: 2,
    crosshairMarkerVisible: true,
  });
  series.setData(lineData);

  if (bestParams) {
    const bestIdx = results.findIndex(r => JSON.stringify(r.params) === JSON.stringify(bestParams));
    if (bestIdx >= 0) {
      series.setMarkers([{
        time: bestIdx + 1,
        position: 'aboveBar',
        color: '#f7931a',
        shape: 'circle',
        text: '🏆',
      }]);
    }
  }

  _btScanChart.timeScale().fitContent();
  const ro = new ResizeObserver(() => {
    if (_btScanChart) _btScanChart.applyOptions({ width: container.clientWidth });
  });
  ro.observe(container);
}


// ═══════════════════════════════════════════════════
// Phase 5: CSV 导出
// ═══════════════════════════════════════════════════

async function _exportCSV() {
  const code = document.getElementById('bt-code').value.trim();
  if (!code) return;

  const freq = document.getElementById('bt-freq').value;
  const params = new URLSearchParams({ code, freq });
  for (const [k, v] of Object.entries(_collectSignalParams())) params.set(k, v);
  for (const [k, v] of Object.entries(_collectSimParams())) params.set(k, v);

  try {
    const resp = await fetch('/api/backtest/export?' + params.toString());
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
    const blob = await resp.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `backtest_${code}_${new Date().toISOString().slice(0, 10)}.csv`;
    a.click();
    URL.revokeObjectURL(url);
    showToast('导出成功');
  } catch (e) {
    showToast('导出失败: ' + e.message);
  }
}


async function _pushToWeChat() {
  if (!_btFullData) { showToast('请先运行分析'); return; }
  const btn = document.getElementById('bt-push-wx');
  btn.disabled = true;
  btn.textContent = '推送中...';
  try {
    const resp = await fetch('/api/backtest/push', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(_btFullData),
    });
    const result = await resp.json();
    if (result.ok) {
      showToast('已推送到微信');
    } else {
      showToast('推送失败: ' + (result.error || '未知错误'));
    }
  } catch (e) {
    showToast('推送失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '📱 推送';
  }
}


// ═══════════════════════════════════════════════════
// 工具函数
// ═══════════════════════════════════════════════════

function _fmtRet(val) {
  if (val == null) return '—';
  return (val >= 0 ? '+' : '') + val + '%';
}

function _retCls(val) {
  if (val == null) return '';
  return val >= 0 ? 'up' : 'down';
}

function _groupBadge(group) {
  const map = {
    'macd': '<span class="bt-pattern-badge macd">MACD</span>',
    'czsc': '<span class="bt-pattern-badge czsc">缠论</span>',
    'gap': '<span class="bt-pattern-badge gap">跳空</span>',
    'trend': '<span class="bt-pattern-badge trend">突破</span>',
    'vol': '<span class="bt-pattern-badge vol">收缩</span>',
  };
  return map[group] || `<span class="bt-pattern-badge">${group}</span>`;
}
