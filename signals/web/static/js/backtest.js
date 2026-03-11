/**
 * 隆小侠 LONG CLAW — 回测页
 * KPI + 信号分析(GroupStats表 + 衰减曲线 + 校准) + 买卖配对
 */

let _btLoaded = false;

window.loadBacktestPage = function () {
  if (_btLoaded) return;
  _btLoaded = true;
  loadBacktestData();
  _initBtEvents();
};

function _initBtEvents() {
  // 筛选
  document.getElementById('bt-signal-type').addEventListener('change', _onFilterChange);
  document.getElementById('bt-freq').addEventListener('change', _onFilterChange);
  document.getElementById('bt-refresh-btn').addEventListener('click', () => {
    _btLoaded = false;
    loadBacktestData();
    _btLoaded = true;
  });
  // 评估按钮
  document.getElementById('bt-eval-btn').addEventListener('click', _triggerEvaluate);
  // 视图 tab
  document.querySelectorAll('.bt-view-tab').forEach(tab => {
    tab.addEventListener('click', () => _switchBtView(tab.dataset.view));
  });
}

function _onFilterChange() {
  loadBacktestData();
}

async function _triggerEvaluate() {
  const btn = document.getElementById('bt-eval-btn');
  btn.disabled = true;
  btn.textContent = '评估中...';
  try {
    const res = await fetch(API_BASE + '/api/backtest/evaluate', { method: 'POST' });
    const data = await res.json();
    btn.textContent = data.message || '完成';
    setTimeout(() => { btn.textContent = '评估到期信号'; btn.disabled = false; }, 2000);
    _btLoaded = false;
    loadBacktestData();
    _btLoaded = true;
  } catch (e) {
    btn.textContent = '失败';
    btn.disabled = false;
  }
}

function _switchBtView(viewName) {
  document.querySelectorAll('.bt-view-tab').forEach(t => t.classList.toggle('active', t.dataset.view === viewName));
  document.querySelectorAll('.bt-view').forEach(v => v.classList.remove('active'));
  const view = document.getElementById('bt-view-' + viewName);
  if (view) view.classList.add('active');
}

async function loadBacktestData() {
  const sigType = document.getElementById('bt-signal-type').value;
  const freq = document.getElementById('bt-freq').value;
  const params = new URLSearchParams();
  if (sigType) params.set('signal_type', sigType);
  if (freq) params.set('freq', freq);

  try {
    const [summary, report, tradePairs] = await Promise.all([
      apiFetch('/api/backtest/summary'),
      apiFetch('/api/backtest/report?' + params.toString()),
      apiFetch('/api/backtest/trade-pairs'),
    ]);
    _renderSummaryBar(summary);
    if (report.empty) {
      _renderEmpty();
      return;
    }
    _renderKPIs(report.kpi);
    _renderByType(report.by_type, report.sqs);
    _renderDecayCurve(report.decay);
    _renderCalibration(report.calibration);
    _renderGroupTable('bt-by-freq', report.by_freq, '频率');
    _renderGroupTable('bt-by-direction', report.by_direction, '环境');
    _renderGroupTable('bt-by-resonance', report.by_resonance, '类型');
    _renderMfeMae(report.mfe_mae);
    _renderWeightRec(report.weight_rec, report.sqs);
    _renderTradeSummary(tradePairs.summary);
    _renderTradeList(tradePairs.pairs);
    _renderEquityCurve(tradePairs.pairs);
  } catch (e) {
    console.error('Backtest load error:', e);
    _renderEmpty('加载失败: ' + e.message);
  }
}

// ── Summary Bar ───────────────────────────────────
function _renderSummaryBar(s) {
  const el = document.getElementById('bt-summary-bar');
  if (s.error) {
    el.innerHTML = `<span class="bt-summary-error">数据库错误: ${s.error}</span>`;
    return;
  }
  el.innerHTML = `
    <span>信号数据库: 总计 <b>${s.total}</b> 条</span>
    <span class="bt-sep">|</span>
    <span>已评估 <b>${s.evaluated}</b></span>
    <span class="bt-sep">|</span>
    <span>待评估 <b>${s.pending}</b></span>
  `;
}

// ── Empty State ───────────────────────────────────
function _renderEmpty(msg) {
  const kpi = document.getElementById('bt-kpis');
  kpi.innerHTML = `<div class="empty-state">${msg || '暂无已评估信号数据。请先运行盘中监测或盘后复盘积累信号，等待20天后再来查看。'}</div>`;
  ['bt-by-type', 'bt-decay-chart', 'bt-calibration', 'bt-by-freq', 'bt-by-direction',
    'bt-by-resonance', 'bt-mfe-mae', 'bt-weight-rec', 'bt-trade-summary', 'bt-trade-list', 'bt-equity-chart']
    .forEach(id => { document.getElementById(id).innerHTML = ''; });
}

// ── KPI Cards ─────────────────────────────────────
function _renderKPIs(kpi) {
  const el = document.getElementById('bt-kpis');
  const items = [
    { value: kpi.total, label: '评估样本', cls: '' },
    { value: kpi.win_rate + '%', label: '总胜率', cls: kpi.win_rate >= 50 ? 'up' : 'down' },
    { value: kpi.profit_factor, label: '盈亏比(PF)', cls: kpi.profit_factor >= 1.5 ? 'up' : kpi.profit_factor < 1 ? 'down' : '' },
    { value: (kpi.expectancy >= 0 ? '+' : '') + kpi.expectancy + '%', label: '期望收益', cls: kpi.expectancy >= 0 ? 'up' : 'down' },
  ];
  el.innerHTML = items.map(it => `
    <div class="bt-kpi-card">
      <div class="bt-kpi-value ${it.cls}">${it.value}</div>
      <div class="bt-kpi-label">${it.label}</div>
    </div>
  `).join('');
}

// ── Signal Type Table (with SQS) ──────────────────
function _renderByType(byType, sqs) {
  const container = document.getElementById('bt-by-type');
  const entries = Object.entries(byType).sort((a, b) => (sqs[b[0]] || 0) - (sqs[a[0]] || 0));
  if (!entries.length) { container.innerHTML = '<div class="empty-state">无数据</div>'; return; }

  let html = `<table class="bt-stats-table">
    <thead><tr>
      <th>信号</th><th>样本</th><th>胜率</th><th>PF</th><th>期望值</th>
      <th>MFE均</th><th>MAE均</th><th>MFE/MAE</th><th>SQS</th>
    </tr></thead><tbody>`;
  for (const [sigType, s] of entries) {
    const score = sqs[sigType] || 0;
    const sqsCls = score >= 70 ? 'sqs-high' : score >= 50 ? 'sqs-mid' : 'sqs-low';
    html += `<tr>
      <td><b>${sigType}</b></td>
      <td>${s.count}</td>
      <td class="${s.win_rate >= 50 ? 'up' : 'down'}">${s.win_rate}%</td>
      <td>${s.profit_factor}</td>
      <td class="${s.expectancy >= 0 ? 'up' : 'down'}">${s.expectancy >= 0 ? '+' : ''}${s.expectancy}%</td>
      <td class="up">${s.avg_mfe >= 0 ? '+' : ''}${s.avg_mfe}</td>
      <td class="down">${s.avg_mae}</td>
      <td>${s.mfe_mae_ratio}</td>
      <td><span class="bt-sqs-badge ${sqsCls}">${score}</span></td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

// ── Generic GroupStats Table ──────────────────────
function _renderGroupTable(containerId, data, label) {
  const container = document.getElementById(containerId);
  const entries = Object.entries(data);
  if (!entries.length) { container.innerHTML = '<div class="empty-state">无数据</div>'; return; }

  let html = `<table class="bt-stats-table">
    <thead><tr>
      <th>${label}</th><th>样本</th><th>胜率</th><th>PF</th><th>期望值</th><th>MFE/MAE</th>
    </tr></thead><tbody>`;
  for (const [key, s] of entries) {
    html += `<tr>
      <td><b>${key}</b></td>
      <td>${s.count}</td>
      <td class="${s.win_rate >= 50 ? 'up' : 'down'}">${s.win_rate}%</td>
      <td>${s.profit_factor}</td>
      <td class="${s.expectancy >= 0 ? 'up' : 'down'}">${s.expectancy >= 0 ? '+' : ''}${s.expectancy}%</td>
      <td>${s.mfe_mae_ratio}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

// ── Signal Decay Curve (Canvas) ───────────────────
function _renderDecayCurve(decay) {
  const container = document.getElementById('bt-decay-chart');
  if (!decay || (!decay['买'] && !decay['卖'])) {
    container.innerHTML = '<div class="empty-state">无衰减数据</div>';
    return;
  }
  container.innerHTML = '<canvas id="bt-decay-canvas"></canvas>';
  const canvas = document.getElementById('bt-decay-canvas');
  canvas.width = container.clientWidth;
  canvas.height = container.clientHeight;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { top: 30, right: 30, bottom: 40, left: 60 };

  const windows = [5, 10, 20];
  const buyData = windows.map(w => (decay['买'] || {})[w] || 0);
  const sellData = windows.map(w => (decay['卖'] || {})[w] || 0);
  const allVals = [...buyData, ...sellData];
  const yMin = Math.min(0, ...allVals) - 0.5;
  const yMax = Math.max(0, ...allVals) + 0.5;

  const xScale = i => pad.left + i * (W - pad.left - pad.right) / (windows.length - 1);
  const yScale = v => pad.top + (1 - (v - yMin) / (yMax - yMin)) * (H - pad.top - pad.bottom);

  // Background
  ctx.fillStyle = cssVar('--bg-secondary') || '#1e222d';
  ctx.fillRect(0, 0, W, H);

  // Grid
  ctx.strokeStyle = cssVar('--border') || '#2a2e39';
  ctx.lineWidth = 1;
  const yZero = yScale(0);
  ctx.beginPath(); ctx.moveTo(pad.left, yZero); ctx.lineTo(W - pad.right, yZero); ctx.stroke();

  // Axes labels
  ctx.fillStyle = cssVar('--text-secondary') || '#787b86';
  ctx.font = '12px sans-serif';
  ctx.textAlign = 'center';
  windows.forEach((w, i) => ctx.fillText('T+' + w, xScale(i), H - pad.bottom + 20));
  ctx.textAlign = 'right';
  [yMin, 0, yMax].forEach(v => ctx.fillText(v.toFixed(1) + '%', pad.left - 8, yScale(v) + 4));

  function drawLine(data, color) {
    ctx.strokeStyle = color;
    ctx.lineWidth = 2.5;
    ctx.beginPath();
    data.forEach((v, i) => {
      const x = xScale(i), y = yScale(v);
      if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
    });
    ctx.stroke();
    // dots
    data.forEach((v, i) => {
      ctx.beginPath();
      ctx.arc(xScale(i), yScale(v), 4, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
    });
  }

  const colorUp = cssVar('--color-up') || '#26a69a';
  const colorDown = cssVar('--color-down') || '#ef5350';
  drawLine(buyData, colorUp);
  drawLine(sellData, colorDown);

  // Legend
  ctx.font = '12px sans-serif';
  ctx.fillStyle = colorUp;
  ctx.fillText('\u25CF 买信号', pad.left + 20, pad.top - 10);
  ctx.fillStyle = colorDown;
  ctx.fillText('\u25CF 卖信号', pad.left + 100, pad.top - 10);
}

// ── Confidence Calibration (Canvas) ───────────────
function _renderCalibration(calibration) {
  const container = document.getElementById('bt-calibration');
  if (!calibration || !calibration.length) {
    container.innerHTML = '<div class="empty-state">无校准数据</div>';
    return;
  }
  container.innerHTML = '<canvas id="bt-cal-canvas"></canvas>';
  const canvas = document.getElementById('bt-cal-canvas');
  canvas.width = container.clientWidth;
  canvas.height = container.clientHeight;
  const ctx = canvas.getContext('2d');
  const W = canvas.width, H = canvas.height;
  const pad = { top: 30, right: 20, bottom: 50, left: 50 };

  const n = calibration.length;
  const barW = Math.min(40, (W - pad.left - pad.right) / (n * 3));
  const maxVal = Math.max(100, ...calibration.map(c => Math.max(c.avg_confidence, c.actual_win_rate)));
  const yScale = v => pad.top + (1 - v / maxVal) * (H - pad.top - pad.bottom);
  const groupW = barW * 2 + 8;
  const groupStart = i => pad.left + (W - pad.left - pad.right) / 2 - (n * groupW) / 2 + i * groupW;

  // Background
  ctx.fillStyle = cssVar('--bg-secondary') || '#1e222d';
  ctx.fillRect(0, 0, W, H);

  const colorPred = cssVar('--text-muted') || '#787b86';
  const colorActual = cssVar('--color-up') || '#26a69a';

  calibration.forEach((c, i) => {
    const x = groupStart(i);
    // Predicted bar
    ctx.fillStyle = colorPred;
    const h1 = H - pad.bottom - yScale(c.avg_confidence);
    ctx.fillRect(x, yScale(c.avg_confidence), barW, h1);
    // Actual bar
    ctx.fillStyle = colorActual;
    const h2 = H - pad.bottom - yScale(c.actual_win_rate);
    ctx.fillRect(x + barW + 4, yScale(c.actual_win_rate), barW, h2);
    // Label
    ctx.fillStyle = cssVar('--text-secondary') || '#787b86';
    ctx.font = '11px sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText(c.bucket, x + groupW / 2, H - pad.bottom + 16);
    // Values on top
    ctx.fillStyle = colorPred;
    ctx.fillText(c.avg_confidence.toFixed(0), x + barW / 2, yScale(c.avg_confidence) - 4);
    ctx.fillStyle = colorActual;
    ctx.fillText(c.actual_win_rate.toFixed(0), x + barW + 4 + barW / 2, yScale(c.actual_win_rate) - 4);
  });

  // Legend
  ctx.font = '12px sans-serif';
  ctx.textAlign = 'left';
  ctx.fillStyle = colorPred;
  ctx.fillText('\u25A0 预测置信度', pad.left, pad.top - 10);
  ctx.fillStyle = colorActual;
  ctx.fillText('\u25A0 实际胜率', pad.left + 100, pad.top - 10);
}

// ── MFE/MAE ───────────────────────────────────────
function _renderMfeMae(data) {
  const container = document.getElementById('bt-mfe-mae');
  const entries = Object.entries(data);
  if (!entries.length) { container.innerHTML = '<div class="empty-state">无数据</div>'; return; }

  let html = `<table class="bt-stats-table">
    <thead><tr><th>信号</th><th>样本</th><th>MFE均</th><th>MAE均</th><th>MFE/MAE</th></tr></thead><tbody>`;
  for (const [sigType, s] of entries) {
    html += `<tr>
      <td><b>${sigType}</b></td><td>${s.count}</td>
      <td class="up">${s.avg_mfe >= 0 ? '+' : ''}${s.avg_mfe}</td>
      <td class="down">${s.avg_mae}</td>
      <td>${s.mfe_mae_ratio}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

// ── Weight Recommendation ─────────────────────────
function _renderWeightRec(rec, sqs) {
  const container = document.getElementById('bt-weight-rec');
  const entries = Object.entries(rec);
  if (!entries.length) { container.innerHTML = '<div class="empty-state">无权重建议</div>'; return; }

  entries.sort((a, b) => (sqs[b[0]] || 0) - (sqs[a[0]] || 0));

  let html = `<table class="bt-stats-table">
    <thead><tr><th>信号</th><th>当前</th><th>建议</th><th>说明</th></tr></thead><tbody>`;
  for (const [sigType, r] of entries) {
    const diff = r.suggested - r.current;
    const cls = diff > 0 ? 'up' : diff < 0 ? 'down' : '';
    html += `<tr>
      <td><b>${sigType}</b></td>
      <td>${r.current}</td>
      <td class="${cls}"><b>${r.suggested}</b></td>
      <td>${r.note}</td>
    </tr>`;
  }
  html += '</tbody></table>';
  container.innerHTML = html;
}

// ── Trade Summary ─────────────────────────────────
function _renderTradeSummary(summary) {
  const container = document.getElementById('bt-trade-summary');
  if (!summary || !summary.pair_count) {
    container.innerHTML = '<div class="empty-state">无买卖配对数据</div>';
    return;
  }
  const s = summary;
  const best = s.best_pair || {};
  const worst = s.worst_pair || {};
  container.innerHTML = `
    <div class="bt-trade-summary-card">
      <div class="bt-trade-stat"><b>${s.pair_count}</b> 组配对</div>
      <div class="bt-trade-stat">平均持仓 <b>${s.avg_holding_days}天</b></div>
      <div class="bt-trade-stat">平均收益 <b class="${s.avg_return_pct >= 0 ? 'up' : 'down'}">${s.avg_return_pct >= 0 ? '+' : ''}${s.avg_return_pct}%</b></div>
      <div class="bt-trade-stat">连盈 <b>${s.max_consecutive_wins}</b> | 连亏 <b>${s.max_consecutive_losses}</b></div>
      ${best.symbol ? `<div class="bt-trade-stat best">最佳: ${best.symbol} ${best.buy_signal_type || ''}→${best.sell_signal_type || ''} <b class="up">${best.return_pct >= 0 ? '+' : ''}${best.return_pct}%</b> (${best.holding_days}日)</div>` : ''}
      ${worst.symbol ? `<div class="bt-trade-stat worst">最差: ${worst.symbol} <b class="down">${worst.return_pct}%</b></div>` : ''}
    </div>
  `;
}

// ── Trade List ────────────────────────────────────
function _renderTradeList(pairs) {
  const container = document.getElementById('bt-trade-list');
  if (!pairs || !pairs.length) { container.innerHTML = '<div class="empty-state">无配对</div>'; return; }

  let html = `<div class="bt-trade-list-wrapper"><table class="bt-stats-table">
    <thead><tr><th>代码</th><th>买入日</th><th>卖出日</th><th>买型</th><th>卖型</th><th>持仓</th><th>收益%</th></tr></thead><tbody>`;
  for (const p of pairs) {
    const cls = p.return_pct > 0 ? 'bt-trade-win' : p.return_pct < 0 ? 'bt-trade-loss' : '';
    html += `<tr class="${cls}">
      <td>${p.symbol}</td>
      <td>${(p.buy_date || '').slice(5)}</td>
      <td>${(p.sell_date || '').slice(5)}</td>
      <td>${p.buy_signal_type || ''}</td>
      <td>${p.sell_signal_type || ''}</td>
      <td>${p.holding_days}天</td>
      <td class="${p.return_pct >= 0 ? 'up' : 'down'}"><b>${p.return_pct >= 0 ? '+' : ''}${p.return_pct}%</b></td>
    </tr>`;
  }
  html += '</tbody></table></div>';
  container.innerHTML = html;
}

// ── Equity Curve (Lightweight Charts) ─────────────
let _equityChart = null;

function _renderEquityCurve(pairs) {
  const container = document.getElementById('bt-equity-chart');
  if (!pairs || !pairs.length) {
    container.innerHTML = '<div class="empty-state">无配对数据</div>';
    return;
  }
  container.innerHTML = '';

  // Sort by sell_date, compute cumulative return
  const sorted = [...pairs].filter(p => p.sell_date).sort((a, b) => a.sell_date.localeCompare(b.sell_date));
  let cum = 0;
  const data = sorted.map(p => {
    cum += p.return_pct;
    const [y, m, d] = p.sell_date.split('-').map(Number);
    return { time: `${y}-${String(m).padStart(2, '0')}-${String(d).padStart(2, '0')}`, value: Math.round(cum * 100) / 100 };
  });

  if (!data.length) return;

  // Dedup same dates (keep last)
  const deduped = [];
  for (const d of data) {
    if (deduped.length && deduped[deduped.length - 1].time === d.time) {
      deduped[deduped.length - 1].value = d.value;
    } else {
      deduped.push(d);
    }
  }

  if (typeof LightweightCharts === 'undefined') {
    container.innerHTML = '<div class="empty-state">图表库未加载</div>';
    return;
  }

  if (_equityChart) { _equityChart.remove(); _equityChart = null; }

  const isDark = document.documentElement.dataset.theme === 'tradingview';
  _equityChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: 300,
    layout: {
      background: { type: 'solid', color: isDark ? '#1e222d' : '#f8f6f2' },
      textColor: isDark ? '#d1d4dc' : '#4a4a4a',
    },
    grid: {
      vertLines: { color: isDark ? '#2a2e39' : '#e0ddd4' },
      horzLines: { color: isDark ? '#2a2e39' : '#e0ddd4' },
    },
    rightPriceScale: { borderColor: isDark ? '#2a2e39' : '#d0cdc4' },
    timeScale: { borderColor: isDark ? '#2a2e39' : '#d0cdc4' },
  });

  const series = _equityChart.addAreaSeries({
    lineColor: deduped[deduped.length - 1].value >= 0 ? '#26a69a' : '#ef5350',
    topColor: deduped[deduped.length - 1].value >= 0 ? 'rgba(38,166,154,0.3)' : 'rgba(239,83,80,0.3)',
    bottomColor: 'rgba(0,0,0,0)',
    lineWidth: 2,
  });
  series.setData(deduped);
  _equityChart.timeScale().fitContent();
}
