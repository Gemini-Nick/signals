const WB_STATE = {
  shell: null,
  cluster: null,
  target: { label: '', kind: 'auto', freq: '30min' },
  symbolData: null,
  backtestData: null,
  activeTab: 'backtest',
  range: null,
  chart: null,
  clusterCache: null,
  loading: false,
  retryTimer: null,
};

const WB_FREQS = ['30min', '15min', '5min', 'daily', 'weekly'];

function wbCssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function wbInitTheme() {
  const saved = localStorage.getItem('lc-theme') || 'tradingview';
  document.documentElement.dataset.theme = saved;
}

function wbToggleTheme() {
  const current = document.documentElement.dataset.theme || 'tradingview';
  const next = current === 'anthropic' ? 'tradingview' : 'anthropic';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('lc-theme', next);
  if (WB_STATE.symbolData) {
    wbRenderChart(WB_STATE.symbolData);
  }
}

function wbShowToast(message, timeout = 2200) {
  const el = document.getElementById('wb-toast');
  el.textContent = message;
  el.classList.add('show');
  clearTimeout(wbShowToast._timer);
  wbShowToast._timer = setTimeout(() => el.classList.remove('show'), timeout);
}

async function wbApiFetch(path) {
  const res = await fetch(path);
  let data = {};
  try {
    data = await res.json();
  } catch (_) {
    data = {};
  }
  if (!res.ok) {
    const error = new Error(data.detail || data.error || `${res.status} ${path}`);
    error.status = res.status;
    error.payload = data;
    throw error;
  }
  return data;
}

function wbSetBacktestReportButtons(enabled) {
  document.getElementById('wb-backtest-html').disabled = !enabled;
  document.getElementById('wb-backtest-pdf').disabled = !enabled;
}

function wbFormatNumber(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—';
  return Number(value).toFixed(digits);
}

function wbFormatPct(value, digits = 2) {
  if (value == null || Number.isNaN(Number(value))) return '—';
  const num = Number(value);
  return `${num > 0 ? '+' : ''}${num.toFixed(digits)}%`;
}

function wbUnixToLabel(ts, withTime = false) {
  if (!ts) return '';
  const dt = new Date(ts * 1000);
  if (withTime) {
    return `${dt.getMonth() + 1}-${String(dt.getDate()).padStart(2, '0')} ${String(dt.getHours()).padStart(2, '0')}:${String(dt.getMinutes()).padStart(2, '0')}`;
  }
  return `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`;
}

function wbEscapeHtml(raw) {
  return String(raw || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;');
}

function wbEmpty(text = '暂无数据') {
  return `<div class="wb-empty">${wbEscapeHtml(text)}</div>`;
}

function wbClearRetryTimer() {
  if (!WB_STATE.retryTimer) return;
  clearTimeout(WB_STATE.retryTimer);
  WB_STATE.retryTimer = null;
}

function wbSetLoading(loading) {
  WB_STATE.loading = loading;
  document.body.style.cursor = loading ? 'progress' : '';
}

function wbUpdateUrl() {
  const url = new URL(window.location.href);
  url.searchParams.set('symbol', WB_STATE.target.label);
  url.searchParams.set('kind', WB_STATE.target.kind);
  url.searchParams.set('freq', WB_STATE.target.freq);
  window.history.replaceState({}, '', url);
}

function wbReadUrlTarget() {
  const url = new URL(window.location.href);
  const symbol = url.searchParams.get('symbol');
  const kind = url.searchParams.get('kind');
  const freq = url.searchParams.get('freq');
  if (!symbol) return null;
  return {
    label: symbol,
    kind: kind || 'auto',
    freq: freq || '30min',
  };
}

function wbSetSession(session) {
  const badge = document.getElementById('wb-session-badge');
  const meta = document.getElementById('wb-session-meta');
  badge.className = 'wb-session-badge';
  const statusClass = session.ready ? (session.a_live || session.hk_live || session.us_live ? 'live' : null) : 'loading';
  if (statusClass) badge.classList.add(statusClass);
  badge.textContent = session.label || (session.ready ? '已就绪' : '加载中');
  meta.textContent = session.error
    ? session.error
    : `${session.mode || 'runtime'} · ${session.data_as_of ? `数据截至 ${session.data_as_of}` : '等待首轮分析完成'}`;
}

function wbRenderNotices(notices) {
  const el = document.getElementById('wb-notices');
  if (!notices || !notices.length) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = notices.map(item => `<div class="wb-notice">${wbEscapeHtml(item)}</div>`).join('');
}

function wbRenderQuickTargets(shell) {
  const el = document.getElementById('wb-quick-targets');
  const defaults = (shell.indices || []).slice(0, 8).map(item => ({
    label: item.name,
    kind: 'index',
  }));
  el.innerHTML = defaults.map(item => `
    <button class="wb-chip ${WB_STATE.target.label === item.label ? 'active' : ''}" data-kind="${item.kind}" data-label="${item.label}">
      ${wbEscapeHtml(item.label)}
    </button>
  `).join('');
  document.getElementById('wb-default-target-label').textContent = shell.default_target?.label || '默认';
}

function wbRenderCandidates(shell) {
  const el = document.getElementById('wb-candidates');
  const items = shell.buy_candidates || [];
  document.getElementById('wb-candidate-count').textContent = String(items.length);
  if (!items.length) {
    el.innerHTML = wbEmpty('候选池会在 L3 完成后出现');
    return;
  }
  el.innerHTML = items.map(item => `
    <div class="wb-list-item" data-kind="stock" data-label="${wbEscapeHtml(item.symbol)}">
      <div class="wb-list-row">
        <div class="wb-list-title">${wbEscapeHtml(item.name || item.symbol)}</div>
        <div class="wb-list-side"><span class="wb-score-pill">${wbEscapeHtml(item.fused_total ?? item.total_score ?? '—')}</span></div>
      </div>
      <div class="wb-list-subtitle">${wbEscapeHtml(item.symbol)} · ${wbEscapeHtml(item.direction || '')} · ${wbEscapeHtml(item.ma_confirmation || '')}</div>
    </div>
  `).join('');
}

function wbRenderClusters(clusterSummary) {
  const industryEl = document.getElementById('wb-industry-clusters');
  const conceptEl = document.getElementById('wb-concept-clusters');
  const industryTop = clusterSummary.industry_top || [];
  const conceptTop = clusterSummary.concept_top || [];

  industryEl.innerHTML = industryTop.length ? industryTop.map((item, idx) => `
    <div class="wb-cluster-card" data-kind="industry" data-label="${wbEscapeHtml(item.label)}">
      <div class="wb-cluster-row">
        <span class="wb-cluster-rank">#${idx + 1}</span>
        <span class="wb-cluster-title">${wbEscapeHtml(item.label)}</span>
      </div>
      <div class="wb-cluster-meta-line">${wbFormatPct(item.avg_gain ?? 0)} · 广度 ${(item.avg_breadth * 100).toFixed(0)}% · ${item.size || 0} 板块</div>
    </div>
  `).join('') : wbEmpty('聚类结果加载中');

  conceptEl.innerHTML = conceptTop.length ? conceptTop.map((item, idx) => `
    <div class="wb-cluster-card" data-kind="concept" data-label="${wbEscapeHtml(item.label)}">
      <div class="wb-cluster-row">
        <span class="wb-cluster-rank">C${idx + 1}</span>
        <span class="wb-cluster-title">${wbEscapeHtml(item.label)}</span>
      </div>
      <div class="wb-cluster-meta-line">${wbFormatPct(item.avg_gain ?? 0)} · 主题簇分数 ${(item.score * 100).toFixed(0)}</div>
    </div>
  `).join('') : wbEmpty('暂无概念热点');
}

function wbRenderSummaryCard(summary) {
  document.getElementById('wb-summary-kind').textContent = summary.latest_signal || 'chart-first';
  document.getElementById('wb-hero-title').textContent = summary.conclusion || '等待更多确认';
  document.getElementById('wb-hero-text').textContent = [
    summary.daily_trend,
    summary.f30_trend,
    summary.f15_trend,
  ].filter(Boolean).join(' / ') || '图表已就绪，可从左侧切换观察方向或直接搜索标的。';
}

function wbRenderKeyLevels(summary) {
  const el = document.getElementById('wb-key-levels');
  const levels = summary.key_levels || [];
  if (!levels.length) {
    el.innerHTML = wbEmpty('当前目标没有结构关键位');
    return;
  }
  el.innerHTML = levels.map(item => `
    <div class="wb-level-item">
      <div class="wb-level-line">
        <span class="wb-level-pill">${wbEscapeHtml(item.name)}</span>
        <strong>${wbEscapeHtml(wbFormatNumber(item.value, 2))}</strong>
      </div>
      <div class="wb-list-subtitle">${wbEscapeHtml(item.position || '')} · 距离 ${wbEscapeHtml(wbFormatPct(item.distance_pct ?? 0, 2))}</div>
    </div>
  `).join('');
}

function wbRenderSignals(signals) {
  const el = document.getElementById('wb-signal-list');
  document.getElementById('wb-signal-count').textContent = String((signals || []).length);
  if (!signals || !signals.length) {
    el.innerHTML = wbEmpty('当前目标暂无信号');
    return;
  }
  const recent = [...signals].slice(-8).reverse();
  el.innerHTML = recent.map(item => {
    const isBuy = String(item.type || '').includes('买');
    return `
      <div class="wb-list-item">
        <div class="wb-list-row">
          <span class="wb-signal-pill ${isBuy ? 'buy' : 'sell'}">${wbEscapeHtml(item.type || '信号')}</span>
          <span class="wb-list-title">${wbEscapeHtml(item.freq || '')}</span>
          <span class="wb-list-side">${wbEscapeHtml(wbFormatNumber(item.price, 2))}</span>
        </div>
        <div class="wb-list-subtitle">${wbEscapeHtml(item.dt ? wbUnixToLabel(item.dt, true) : '')} · 置信度 ${wbEscapeHtml(wbFormatNumber(item.confidence ?? 0, 2))}</div>
      </div>
    `;
  }).join('');
}

function wbRenderBootState(session, message = '分析引擎正在构建首轮快照，图表就绪后会自动切入当前目标。') {
  const phase = session?.loading_phase ? `阶段 ${session.loading_phase}` : '等待首轮分析';
  document.getElementById('wb-target-kind').textContent = 'BOOT';
  document.getElementById('wb-target-title').textContent = session?.label ? `${session.label} · 初始化中` : '分析引擎初始化中';
  document.getElementById('wb-target-subtitle').textContent = phase;
  document.getElementById('wb-target-stats').innerHTML = `
    <div class="wb-stat-chip">
      <div class="wb-stat-label">状态</div>
      <div class="wb-stat-value">${wbEscapeHtml(session?.running ? '加载中' : '等待')}</div>
    </div>
    <div class="wb-stat-chip">
      <div class="wb-stat-label">阶段</div>
      <div class="wb-stat-value">${wbEscapeHtml(session?.loading_phase || 'boot')}</div>
    </div>
  `;
  wbRenderSummaryCard({
    latest_signal: 'bootstrapping',
    conclusion: '正在建立图表终端上下文',
    daily_trend: message,
  });
  wbRenderKeyLevels({ key_levels: [] });
  wbRenderSignals([]);
  wbRenderPlan(null, { candidate_stocks: [] });
  wbRenderRisks({ review: { error: session?.error || '' } });
  wbRenderReview({
    completed: false,
    is_running: !!session?.running,
    phase: session?.loading_phase || '',
    phase_detail: message,
    timeline: [],
  });
  wbRenderTrades({ summary: {}, related_trades: [], missed_signals: [] });
  wbRenderBacktestEmpty('引擎就绪后会自动载入增强回测');
  document.getElementById('wb-chart').innerHTML = `<div class="wb-loading">${wbEscapeHtml(message)}</div>`;
}

function wbRenderPlan(plan, symbolData) {
  const el = document.getElementById('wb-plan-list');
  if (plan && plan.scenarios && plan.scenarios.length) {
    el.innerHTML = plan.scenarios.map(item => `
      <div class="wb-plan-card">
        <div class="wb-plan-title">${wbEscapeHtml(item.name || '情景')}</div>
        <div class="wb-plan-body">触发：${wbEscapeHtml(item.trigger || '—')}</div>
        <div class="wb-plan-body">动作：${wbEscapeHtml(item.action || '—')}</div>
        ${item.target_prices && item.target_prices.length ? `<div class="wb-plan-body">目标：${wbEscapeHtml(item.target_prices.join(' / '))}</div>` : ''}
      </div>
    `).join('');
    return;
  }

  if (plan && plan.layered_position && Object.keys(plan.layered_position).length) {
    const layered = plan.layered_position;
    el.innerHTML = `
      <div class="wb-plan-card">
        <div class="wb-plan-title">分层仓位建议</div>
        <div class="wb-plan-body">底仓：${wbEscapeHtml(wbFormatNumber(layered.base_pct, 1))}% · 弹性：${wbEscapeHtml(wbFormatNumber(layered.flex_pct, 1))}%</div>
        <div class="wb-plan-body">${wbEscapeHtml(layered.rationale || '')}</div>
      </div>
    `;
    return;
  }

  if (symbolData.candidate_stocks && symbolData.candidate_stocks.length) {
    el.innerHTML = symbolData.candidate_stocks.slice(0, 5).map(item => `
      <div class="wb-plan-card">
        <div class="wb-plan-title">${wbEscapeHtml(item.name || item.code)}</div>
        <div class="wb-plan-body">${wbEscapeHtml(item.code || '')} · ${wbEscapeHtml(item.role || '')} · ${wbEscapeHtml(item.detail || '')}</div>
      </div>
    `).join('');
    return;
  }

  el.innerHTML = wbEmpty('当前目标暂无额外预案');
}

function wbRenderRisks(symbolData) {
  const el = document.getElementById('wb-risk-list');
  const summary = symbolData.summary || {};
  const risks = [];
  if (symbolData.stock_analysis?.risk?.description) risks.push(symbolData.stock_analysis.risk.description);
  if (summary.phase_hint) risks.push(summary.phase_hint);
  if (summary.style_switch) risks.push(summary.style_switch);
  if (symbolData.review?.error) risks.push(symbolData.review.error);
  if (!risks.length) {
    el.innerHTML = wbEmpty('暂无额外失效条件');
    return;
  }
  el.innerHTML = risks.map(text => `<div class="wb-risk-item"><div class="wb-risk-text">${wbEscapeHtml(text)}</div></div>`).join('');
}

function wbRenderTargetMeta(data) {
  const target = data.target;
  const summary = data.summary || {};
  document.getElementById('wb-target-kind').textContent = String(target.kind || '').toUpperCase();
  document.getElementById('wb-target-title').textContent = summary.title || target.label;
  document.getElementById('wb-target-subtitle').textContent = [summary.subtitle, `周期 ${target.effective_freq}`].filter(Boolean).join(' · ');

  const statItems = [
    ['最新价', wbFormatNumber(summary.latest_price, 2)],
    ['最新信号', summary.latest_signal || '—'],
    ['结论', summary.conclusion ? '已生成' : '—'],
  ];
  if (summary.quote_status_label) statItems.push(['行情', summary.quote_status_label]);
  if (summary.score != null) statItems.push(['评分', wbFormatNumber(summary.score, 1)]);
  if (summary.gain_pct != null && ['realtime', 'delayed'].includes(summary.quote_status)) {
    statItems.push(['今日涨幅', wbFormatPct(summary.gain_pct, 2)]);
  }

  document.getElementById('wb-target-stats').innerHTML = statItems.map(([label, value]) => `
    <div class="wb-stat-chip">
      <div class="wb-stat-label">${wbEscapeHtml(label)}</div>
      <div class="wb-stat-value">${wbEscapeHtml(value)}</div>
    </div>
  `).join('');
}

function wbChartOption(data) {
  const chart = data.chart || {};
  const colors = {
    up: wbCssVar('--color-up') || '#f23645',
    down: wbCssVar('--color-down') || '#26a69a',
    bg: wbCssVar('--chart-bg') || '#131722',
    grid: wbCssVar('--chart-grid') || '#1e222d',
    text: wbCssVar('--text-secondary') || '#787b86',
    accent: wbCssVar('--accent') || '#2962ff',
    ma5: wbCssVar('--ma5-color') || '#f7931a',
    ma10: wbCssVar('--ma10-color') || '#2962ff',
    ma20: wbCssVar('--ma20-color') || '#e040fb',
    ma60: wbCssVar('--ma60-color') || '#26a69a',
    zhongshu: wbCssVar('--zhongshu-stroke') || 'rgba(41, 98, 255, 0.6)',
    macdDif: wbCssVar('--macd-dif') || '#f7931a',
    macdDea: wbCssVar('--macd-dea') || '#2962ff',
  };

  const ohlcv = chart.ohlcv || [];
  const macd = chart.macd || [];
  const maLines = chart.ma_lines || [];
  const signals = chart.signals || [];
  const biList = chart.bi_list || [];
  const zhongshu = chart.zhongshu || [];

  const xAxisData = ohlcv.map(item => item.time * 1000);
  const candleData = ohlcv.map(item => [item.open, item.close, item.low, item.high]);
  const volumeData = ohlcv.map(item => [item.time * 1000, item.volume || 0, item.close >= item.open ? 1 : -1]);

  const biPoints = [];
  biList.forEach(item => {
    if (item.direction === 'up') {
      biPoints.push([item.sdt * 1000, item.low], [item.edt * 1000, item.high]);
    } else {
      biPoints.push([item.sdt * 1000, item.high], [item.edt * 1000, item.low]);
    }
  });

  const markAreas = zhongshu.map(item => [
    { xAxis: item.start_dt * 1000, yAxis: item.zd, itemStyle: { color: 'rgba(41,98,255,0.08)', borderColor: colors.zhongshu } },
    { xAxis: item.end_dt * 1000, yAxis: item.zg },
  ]);

  const signalMarkPoints = signals.map(item => {
    const typeName = String(item.type || '');
    const isBuy = typeName.includes('买');
    return {
      name: typeName,
      coord: [item.dt * 1000, item.price],
      value: typeName,
      symbol: 'pin',
      symbolRotate: isBuy ? 0 : 180,
      symbolSize: 32,
      itemStyle: {
        color: isBuy ? colors.up : colors.down,
      },
      label: {
        show: true,
        color: isBuy ? colors.up : colors.down,
        formatter: () => typeName.length > 6 ? typeName.slice(0, 6) : typeName,
        fontSize: 11,
        fontWeight: 700,
        position: isBuy ? 'top' : 'bottom',
        distance: 6,
        textShadowBlur: 6,
        textShadowColor: 'rgba(0,0,0,0.8)',
      },
    };
  });

  const maColorMap = {
    MA5: colors.ma5,
    MA10: colors.ma10,
    MA20: colors.ma20,
    MA60: colors.ma60,
  };

  const series = [
    {
      name: 'K',
      type: 'candlestick',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: candleData,
      itemStyle: {
        color: colors.up,
        color0: colors.down,
        borderColor: colors.up,
        borderColor0: colors.down,
      },
      markPoint: {
        data: signalMarkPoints,
      },
    },
    {
      name: '中枢',
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: [],
      lineStyle: { opacity: 0 },
      showSymbol: false,
      markArea: { silent: true, data: markAreas },
    },
    {
      name: '笔',
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: biPoints,
      showSymbol: false,
      smooth: false,
      lineStyle: {
        width: 2,
        color: colors.accent,
      },
    },
    {
      name: 'Volume',
      type: 'bar',
      xAxisIndex: 1,
      yAxisIndex: 1,
      data: volumeData.map(item => ({
        value: item[1],
        itemStyle: { color: item[2] > 0 ? 'rgba(242,54,69,0.45)' : 'rgba(38,166,154,0.45)' },
      })),
    },
    {
      name: 'MACD',
      type: 'bar',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: macd.map(item => ({
        value: item.bar,
        itemStyle: { color: item.bar >= 0 ? 'rgba(242,54,69,0.55)' : 'rgba(38,166,154,0.55)' },
      })),
    },
    {
      name: 'DIF',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: macd.map(item => [item.time * 1000, item.dif]),
      showSymbol: false,
      lineStyle: { width: 1.5, color: colors.macdDif },
    },
    {
      name: 'DEA',
      type: 'line',
      xAxisIndex: 2,
      yAxisIndex: 2,
      data: macd.map(item => [item.time * 1000, item.dea]),
      showSymbol: false,
      lineStyle: { width: 1.5, color: colors.macdDea },
    },
  ];

  maLines.forEach(line => {
    series.push({
      name: line.label,
      type: 'line',
      xAxisIndex: 0,
      yAxisIndex: 0,
      data: (line.data || []).map(item => [item.time * 1000, item.value]),
      showSymbol: false,
      smooth: true,
      lineStyle: {
        width: 1.25,
        color: maColorMap[line.label] || line.color || colors.accent,
      },
    });
  });

  return {
    animation: false,
    backgroundColor: 'transparent',
    tooltip: {
      trigger: 'axis',
      axisPointer: { type: 'cross' },
      backgroundColor: 'rgba(19,23,34,0.96)',
      borderColor: 'rgba(41,98,255,0.35)',
      textStyle: { color: '#d1d4dc' },
    },
    legend: {
      top: 10,
      right: 14,
      textStyle: { color: colors.text },
      itemWidth: 16,
      itemHeight: 8,
    },
    axisPointer: {
      link: [{ xAxisIndex: 'all' }],
      label: { backgroundColor: '#1e222d' },
    },
    brush: {
      toolbox: ['lineX', 'clear'],
      xAxisIndex: 'all',
      brushLink: 'all',
      outOfBrush: { colorAlpha: 0.14 },
    },
    toolbox: {
      show: true,
      feature: {
        dataZoom: { yAxisIndex: false },
        brush: { type: ['lineX', 'clear'] },
        restore: {},
      },
      iconStyle: { borderColor: colors.text },
    },
    grid: [
      { left: 56, right: 18, top: 74, height: '50%' },
      { left: 56, right: 18, top: '62%', height: '10%' },
      { left: 56, right: 18, top: '77%', height: '15%' },
    ],
    xAxis: [
      {
        type: 'time',
        gridIndex: 0,
        axisLine: { lineStyle: { color: colors.grid } },
        axisLabel: { color: colors.text, hideOverlap: true },
        splitLine: { show: false },
      },
      {
        type: 'time',
        gridIndex: 1,
        axisLine: { lineStyle: { color: colors.grid } },
        axisLabel: { show: false },
        splitLine: { show: false },
      },
      {
        type: 'time',
        gridIndex: 2,
        axisLine: { lineStyle: { color: colors.grid } },
        axisLabel: { color: colors.text, formatter: value => wbUnixToLabel(Math.floor(value / 1000)) },
        splitLine: { show: false },
      },
    ],
    yAxis: [
      {
        scale: true,
        gridIndex: 0,
        position: 'right',
        axisLine: { show: false },
        axisLabel: { color: colors.text },
        splitLine: { lineStyle: { color: colors.grid } },
      },
      {
        scale: true,
        gridIndex: 1,
        position: 'right',
        axisLine: { show: false },
        axisLabel: { color: colors.text, show: false },
        splitLine: { show: false },
      },
      {
        scale: true,
        gridIndex: 2,
        position: 'right',
        axisLine: { show: false },
        axisLabel: { color: colors.text },
        splitLine: { lineStyle: { color: colors.grid } },
      },
    ],
    dataZoom: [
      { type: 'inside', xAxisIndex: [0, 1, 2], start: 55, end: 100 },
      { type: 'slider', xAxisIndex: [0, 1, 2], bottom: 8, height: 18, start: 55, end: 100 },
    ],
    series,
  };
}

function wbApplyBrushHandler(chart, symbolData) {
  let brushTimer = null;
  chart.off('brushSelected');
  chart.on('brushSelected', params => {
    clearTimeout(brushTimer);
    brushTimer = setTimeout(() => {
      const area = params?.batch?.[0]?.areas?.[0];
      if (!area || !area.coordRange || !WB_STATE.symbolData) return;
      const [start, end] = area.coordRange;
      if (!start || !end) return;
      WB_STATE.range = { start, end };
      document.getElementById('wb-chart-range-label').textContent = `选区：${wbUnixToLabel(Math.floor(start / 1000))} → ${wbUnixToLabel(Math.floor(end / 1000))}`;
      if (WB_STATE.symbolData.review) wbRenderReview(WB_STATE.symbolData.review);
      if (WB_STATE.symbolData.analysis_target) {
        wbLoadBacktest(WB_STATE.symbolData.analysis_target, WB_STATE.symbolData.target?.effective_freq || 'daily');
      }
    }, 260);
  });
}

function wbRenderChart(symbolData) {
  const dom = document.getElementById('wb-chart');
  if (!WB_STATE.chart) {
    WB_STATE.chart = echarts.init(dom, null, { renderer: 'canvas' });
    window.addEventListener('resize', () => WB_STATE.chart && WB_STATE.chart.resize());
  }
  const option = wbChartOption(symbolData);
  WB_STATE.chart.setOption(option, true);
  wbApplyBrushHandler(WB_STATE.chart, symbolData);

  const signals = symbolData.chart?.signals || [];
  const summaryEl = document.getElementById('wb-chart-signal-summary');
  if (signals.length && summaryEl) {
    const recent = signals.slice(-3).reverse();
    const parts = recent.map(s => {
      const isBuy = String(s.type || '').includes('买');
      return `<span style="color:${isBuy ? wbCssVar('--color-up') : wbCssVar('--color-down')}">${wbEscapeHtml(s.type || '')}</span>`;
    });
    summaryEl.innerHTML = `最近信号: ${parts.join(' · ')}`;
  } else if (summaryEl) {
    summaryEl.textContent = '';
  }
}

function wbRenderReview(review) {
  const metaEl = document.getElementById('wb-review-meta');
  const cardsEl = document.getElementById('wb-review-cards');
  const timelineEl = document.getElementById('wb-review-timeline');

  metaEl.textContent = review.completed
    ? `${review.start_label || review.start_date || '最近一次'} · 已完成`
    : review.is_running
      ? `运行中 · ${review.phase_detail || review.phase || '加载中'}`
      : '暂无最近复盘';

  const cards = [];
  if (review.reviewed_report) {
    cards.push(`
      <div class="wb-review-card">
        <div class="wb-list-title">${wbEscapeHtml(review.reviewed_report.name)}</div>
        <div class="wb-list-subtitle">${wbEscapeHtml(review.reviewed_report.summary || '')}</div>
      </div>
    `);
  }
  if (review.reviewed_symbol) {
    cards.push(`
      <div class="wb-review-card">
        <div class="wb-list-title">${wbEscapeHtml(review.reviewed_symbol.name)}</div>
        <div class="wb-list-subtitle">${wbEscapeHtml(review.reviewed_symbol.direction || '')} · 总分 ${wbEscapeHtml(wbFormatNumber(review.reviewed_symbol.total_score, 1))}</div>
      </div>
    `);
  }
  if (review.industry) {
    cards.push(`
      <div class="wb-review-card">
        <div class="wb-list-title">${wbEscapeHtml(review.industry.name)}</div>
        <div class="wb-list-subtitle">${wbEscapeHtml(review.industry.phase || '')} · ${wbEscapeHtml(review.industry.rotation_line || '')}</div>
      </div>
    `);
  }
  cardsEl.innerHTML = cards.length ? cards.join('') : wbEmpty('当前目标暂无额外复盘卡片');

  const timeline = review.timeline || [];
  if (!timeline.length) {
    timelineEl.innerHTML = wbEmpty('没有可展示的信号回放时间线');
    return;
  }

  let filtered = timeline;
  if (WB_STATE.range?.start && WB_STATE.range?.end) {
    const low = Math.min(WB_STATE.range.start, WB_STATE.range.end) / 1000;
    const high = Math.max(WB_STATE.range.start, WB_STATE.range.end) / 1000;
    filtered = timeline.filter(item => item.dt >= low && item.dt <= high);
  }

  timelineEl.innerHTML = filtered.length ? filtered.map(item => `
    <div class="wb-timeline-row">
      <div class="wb-timeline-time">${wbEscapeHtml(item.dt_str || '')}</div>
      <div>
        <div class="wb-list-title">${wbEscapeHtml(item.signal_type || item.action || '')}</div>
        <div class="wb-timeline-meta">${wbEscapeHtml(item.action || '')} · 价格 ${wbEscapeHtml(wbFormatNumber(item.price, 2))}</div>
      </div>
      <div class="wb-list-side">${wbEscapeHtml(wbFormatNumber(item.confidence, 2))}</div>
    </div>
  `).join('') : wbEmpty('当前选区没有时间线数据');
}

function wbKpiCard(label, value, detail = '') {
  return `
    <div class="wb-kpi-card">
      <div class="wb-kpi-label">${wbEscapeHtml(label)}</div>
      <div class="wb-kpi-value">${wbEscapeHtml(value)}</div>
      ${detail ? `<div class="wb-kpi-detail">${wbEscapeHtml(detail)}</div>` : ''}
    </div>
  `;
}

function wbRenderBacktestEmpty(message) {
  document.getElementById('wb-backtest-target').textContent = '';
  document.getElementById('wb-backtest-kpis').innerHTML = wbEmpty(message);
  document.getElementById('wb-sim-kpis').innerHTML = '';
  document.getElementById('wb-backtest-config').textContent = '';
  document.getElementById('wb-backtest-signals').innerHTML = `<tr><td colspan="5">${wbEscapeHtml(message)}</td></tr>`;
  wbSetBacktestReportButtons(false);
}

function wbRenderBacktest(backtest) {
  document.getElementById('wb-backtest-target').textContent = `${backtest.target?.symbol || ''} · ${backtest.target?.effective_freq || ''}`;
  wbSetBacktestReportButtons(true);
  const kpiEl = document.getElementById('wb-backtest-kpis');
  const simEl = document.getElementById('wb-sim-kpis');
  const configEl = document.getElementById('wb-backtest-config');
  const signalEl = document.getElementById('wb-backtest-signals');

  const kpi = backtest.kpi || backtest.forward_kpi || {};
  const sim = backtest.sim_kpi || {};
  kpiEl.innerHTML = [
    wbKpiCard('信号总数', String(kpi.total ?? backtest.signals?.length ?? 0), `已评估 ${kpi.evaluated ?? 0}`),
    wbKpiCard('T+5', wbFormatPct(kpi.return_t5_avg ?? 0, 2), '平均前瞻'),
    wbKpiCard('T+10', wbFormatPct(kpi.return_t10_avg ?? 0, 2), '平均前瞻'),
    wbKpiCard('胜率', wbFormatPct(sim.win_rate ?? 0, 1), `${sim.filled_trades ?? 0} 笔成交`),
  ].join('');

  simEl.innerHTML = [
    wbKpiCard('Sharpe', wbFormatNumber(sim.sharpe ?? 0, 2)),
    wbKpiCard('期望收益', wbFormatPct(sim.expectancy ?? 0, 2)),
    wbKpiCard('总收益', wbFormatPct(sim.total_return_pct ?? 0, 2)),
    wbKpiCard('最大回撤', wbFormatPct(sim.max_drawdown_pct ?? 0, 2)),
  ].join('');

  const warnings = backtest.warnings || [];
  const config = {
    range: backtest.range || null,
    warnings,
    sim_config: backtest.sim_config || {},
    skip_reasons: backtest.sim_skip_reasons || {},
  };
  configEl.textContent = JSON.stringify(config, null, 2);

  const signals = backtest.signals || [];
  signalEl.innerHTML = signals.length ? signals.slice(-18).reverse().map(item => `
    <tr>
      <td>${wbEscapeHtml(item.type || item.signal_type || '')}</td>
      <td>${wbEscapeHtml(item.date_str || item.signal_date || '')}</td>
      <td>${wbEscapeHtml(wbFormatNumber(item.price, 2))}</td>
      <td>${wbEscapeHtml(item.freq || '')}</td>
      <td>${wbEscapeHtml(wbFormatNumber(item.confidence, 2))}</td>
    </tr>
  `).join('') : `<tr><td colspan="5">当前选区没有信号</td></tr>`;
}

async function wbDownloadBacktestReport(format) {
  if (!WB_STATE.backtestData) {
    wbShowToast('请先载入回测数据');
    return;
  }
  const button = document.getElementById(`wb-backtest-${format}`);
  const original = button.textContent;
  button.disabled = true;
  button.textContent = '生成中';
  try {
    const res = await fetch(`/api/backtest/report?format=${encodeURIComponent(format)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(WB_STATE.backtestData),
    });
    if (!res.ok) {
      let message = `${res.status} report`;
      try {
        const data = await res.json();
        message = data.error || data.detail || message;
      } catch (_) {}
      throw new Error(message);
    }
    const blob = await res.blob();
    const disposition = res.headers.get('Content-Disposition') || '';
    const match = disposition.match(/filename="([^"]+)"/);
    const target = WB_STATE.backtestData.target?.code || WB_STATE.backtestData.code || 'unknown';
    const filename = match ? match[1] : `backtest_${target}.${format}`;
    const url = URL.createObjectURL(blob);
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    link.click();
    URL.revokeObjectURL(url);
    wbShowToast(`${format.toUpperCase()} 报告已生成`);
  } catch (error) {
    wbShowToast(error.message || '报告生成失败', 3200);
  } finally {
    button.textContent = original;
    button.disabled = !WB_STATE.backtestData;
  }
}

function wbRenderTrades(trade) {
  document.getElementById('wb-trade-meta').textContent = `${trade.related_trades?.length || 0} 笔相关交易`;
  document.getElementById('wb-trade-kpis').innerHTML = [
    wbKpiCard('总交易', String(trade.summary?.total_trades ?? 0)),
    wbKpiCard('胜率', wbFormatPct(trade.summary?.win_rate ?? 0, 1)),
    wbKpiCard('平均盈亏', wbFormatPct(trade.summary?.avg_pnl_pct ?? 0, 2)),
    wbKpiCard('平均评分', wbFormatNumber(trade.summary?.avg_score ?? 0, 1)),
  ].join('');

  document.getElementById('wb-related-trades').innerHTML = (trade.related_trades || []).length
    ? trade.related_trades.map(item => `
      <tr>
        <td>${wbEscapeHtml(item.name || item.symbol)}</td>
        <td>${wbEscapeHtml(item.entry_date)} @ ${wbEscapeHtml(wbFormatNumber(item.entry_price, 2))}</td>
        <td>${item.exit_date ? `${wbEscapeHtml(item.exit_date)} @ ${wbEscapeHtml(wbFormatNumber(item.exit_price, 2))}` : '持仓中'}</td>
        <td>${wbEscapeHtml(wbFormatPct(item.pnl_pct, 2))}</td>
        <td>${wbEscapeHtml(wbFormatNumber(item.total_score, 1))}</td>
      </tr>
    `).join('')
    : `<tr><td colspan="5">暂无相关交易</td></tr>`;

  document.getElementById('wb-missed-trades').innerHTML = (trade.missed_signals || []).length
    ? trade.missed_signals.map(item => `
      <tr>
        <td>${wbEscapeHtml(item.name || item.symbol)}</td>
        <td>${wbEscapeHtml(item.signal_type)}</td>
        <td>${wbEscapeHtml(item.signal_date)}</td>
        <td>${wbEscapeHtml(wbFormatPct(item.potential_pnl_pct, 2))}</td>
      </tr>
    `).join('')
    : `<tr><td colspan="4">暂无遗漏信号</td></tr>`;
}

function wbUpdateFreqButtons(target) {
  const available = target.available_freqs || [];
  const effective = target.effective_freq || target.requested_freq || WB_STATE.target.freq || '30min';
  document.querySelectorAll('.wb-freq-btn').forEach(btn => {
    const freq = btn.dataset.freq;
    btn.disabled = available.length ? !available.includes(freq) : false;
    btn.classList.toggle('active', freq === effective);
  });
  WB_STATE.target.freq = effective;
}

async function wbLoadShell() {
  const shell = await wbApiFetch('/api/workbench/shell');
  WB_STATE.shell = shell;
  wbSetSession(shell.session);
  wbRenderNotices(shell.notices || []);
  wbRenderQuickTargets(shell);
  wbRenderCandidates(shell);
  wbRenderClusters(shell.cluster_summary || {});
}

async function wbLoadCluster(direction = '', mode = 'belief') {
  const query = new URLSearchParams();
  if (direction) query.set('direction', direction);
  if (mode) query.set('mode', mode);
  const data = await wbApiFetch(`/api/workbench/cluster${query.toString() ? `?${query.toString()}` : ''}`);
  WB_STATE.cluster = data;
  if (data.latest) {
    wbRenderClusters({
      industry_top: data.latest?.industry?.top || [],
      concept_top: data.latest?.concept?.top || [],
    });
  }
  const watchEl = document.getElementById('wb-watch-results');
  const scan = data.scan;
  if (!scan) {
    watchEl.innerHTML = wbEmpty('输入方向后可快速扫描候选股');
    return;
  }
  const results = scan.results || [];
  watchEl.innerHTML = results.length ? results.slice(0, 12).map(item => `
    <div class="wb-list-item" data-kind="stock" data-label="${wbEscapeHtml(item.symbol || item.code || '')}">
      <div class="wb-list-row">
        <div class="wb-list-title">${wbEscapeHtml(item.name || item.symbol || '')}</div>
        <div class="wb-list-side">${wbEscapeHtml(item.grade || '')}</div>
      </div>
      <div class="wb-list-subtitle">${wbEscapeHtml(item.symbol || item.code || '')} · ${wbEscapeHtml(item.detail || '')}</div>
    </div>
  `).join('') : wbEmpty(scan.error || '这个方向暂时没有扫描结果');
}

async function wbLoadSymbol(label, kind = 'auto', silent = false) {
  if (!label) return;
  WB_STATE.target.label = label;
  WB_STATE.target.kind = kind;
  wbUpdateUrl();
  if (!silent) wbSetLoading(true);
  try {
    const data = await wbApiFetch(`/api/workbench/symbol/${encodeURIComponent(label)}?kind=${encodeURIComponent(kind)}&freq=${encodeURIComponent(WB_STATE.target.freq)}`);
    wbClearRetryTimer();
    WB_STATE.symbolData = data;
    WB_STATE.range = null;
    document.getElementById('wb-search-input').value = data.target?.symbol || label;
    document.getElementById('wb-chart-range-label').textContent = '未选择区间';
    wbRenderTargetMeta(data);
    wbRenderSummaryCard(data.summary || {});
    wbRenderKeyLevels(data.summary || {});
    wbRenderSignals(data.signals || []);
    wbRenderPlan(data.plan, data);
    wbRenderRisks(data);
    wbRenderChart(data);
    wbUpdateFreqButtons(data.target || {});
    wbRenderReview(data.review || {});
    wbRenderTrades(data.trade || { summary: {}, related_trades: [], missed_signals: [] });
    if (data.analysis_target) {
      try {
        await wbLoadBacktest(data.analysis_target, data.target?.effective_freq || 'daily');
      } catch (backtestError) {
        WB_STATE.backtestData = null;
        wbRenderBacktestEmpty(backtestError.message || '回测工作台加载失败');
        wbShowToast(backtestError.message || '回测工作台加载失败', 2800);
      }
    } else {
      WB_STATE.backtestData = null;
      wbRenderBacktestEmpty('当前目标没有可联动的回测对象');
    }
  } catch (error) {
    if (error.status === 503) {
      wbRenderBootState(error.payload?.session, error.message);
      wbScheduleSymbolRetry(label, kind);
      return;
    }
    wbShowToast(error.message, 3200);
  } finally {
    wbSetLoading(false);
  }
}

async function wbLoadBacktest(symbol, freq) {
  if (!symbol) return;
  const query = new URLSearchParams({
    symbol,
    freq: ['daily', 'weekly', 'monthly'].includes(freq) ? freq : 'daily',
  });
  if (WB_STATE.range?.start && WB_STATE.range?.end) {
    query.set('start_ts', String(Math.floor(WB_STATE.range.start / 1000)));
    query.set('end_ts', String(Math.floor(WB_STATE.range.end / 1000)));
  }
  const data = await wbApiFetch(`/api/workbench/backtest?${query.toString()}`);
  WB_STATE.backtestData = data;
  wbRenderBacktest(data);
}

function wbScheduleSymbolRetry(label, kind, attempt = 1) {
  wbClearRetryTimer();
  const delay = Math.min(8000, 1200 * attempt);
  WB_STATE.retryTimer = window.setTimeout(async () => {
    try {
      await wbLoadShell();
      if (WB_STATE.target.label !== label || WB_STATE.target.kind !== kind) return;
      await wbLoadSymbol(label, kind, true);
    } catch (error) {
      if (attempt < 8) {
        wbScheduleSymbolRetry(label, kind, attempt + 1);
        return;
      }
      wbShowToast(error.message || '初始化失败', 3600);
    }
  }, delay);
}

async function wbRefreshAll() {
  await fetch('/api/index/refresh', { method: 'POST' });
  wbShowToast('已触发后台刷新，稍后重新拉取数据', 3000);
  setTimeout(async () => {
    await wbLoadShell();
    if (WB_STATE.target.label) {
      await wbLoadSymbol(WB_STATE.target.label, WB_STATE.target.kind, true);
    }
  }, 1800);
}

function wbDelegateClicks() {
  document.body.addEventListener('click', event => {
    const actionable = event.target.closest('[data-kind][data-label]');
    if (!actionable) return;
    wbLoadSymbol(actionable.dataset.label, actionable.dataset.kind);
  });
}

function wbSwitchTab(tab) {
  WB_STATE.activeTab = tab;
  document.querySelectorAll('.wb-tab-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.tab === tab);
  });
  document.querySelectorAll('.wb-tab-panel').forEach(panel => {
    panel.classList.toggle('active', panel.dataset.tab === tab);
  });
}

function wbBindEvents() {
  document.getElementById('wb-theme-toggle').addEventListener('click', wbToggleTheme);
  document.getElementById('wb-refresh-btn').addEventListener('click', wbRefreshAll);
  document.getElementById('wb-backtest-html').addEventListener('click', () => wbDownloadBacktestReport('html'));
  document.getElementById('wb-backtest-pdf').addEventListener('click', () => wbDownloadBacktestReport('pdf'));
  document.getElementById('wb-search-form').addEventListener('submit', event => {
    event.preventDefault();
    const value = document.getElementById('wb-search-input').value.trim();
    if (!value) return;
    wbLoadSymbol(value, 'auto');
  });
  document.getElementById('wb-watch-btn').addEventListener('click', () => {
    const direction = document.getElementById('wb-watch-direction').value.trim();
    const mode = document.getElementById('wb-watch-mode').value;
    wbLoadCluster(direction, mode).catch(error => wbShowToast(error.message, 3000));
  });
  document.getElementById('wb-watch-direction').addEventListener('keydown', event => {
    if (event.key === 'Enter') {
      event.preventDefault();
      document.getElementById('wb-watch-btn').click();
    }
  });
  document.querySelectorAll('.wb-freq-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      if (btn.disabled) return;
      WB_STATE.target.freq = btn.dataset.freq;
      if (WB_STATE.target.label) wbLoadSymbol(WB_STATE.target.label, WB_STATE.target.kind);
    });
  });
  document.querySelectorAll('.wb-tab-btn').forEach(btn => {
    btn.addEventListener('click', () => wbSwitchTab(btn.dataset.tab));
  });
  wbDelegateClicks();
  document.addEventListener('keydown', event => {
    const tag = document.activeElement?.tagName?.toLowerCase();
    if (tag === 'input' || tag === 'textarea' || tag === 'select') return;
    if (event.key === '/') {
      event.preventDefault();
      document.getElementById('wb-search-input').focus();
      return;
    }
    if (event.key === '1') document.querySelector('.wb-freq-btn[data-freq="30min"]')?.click();
    if (event.key === '2') document.querySelector('.wb-freq-btn[data-freq="15min"]')?.click();
    if (event.key === '3') document.querySelector('.wb-freq-btn[data-freq="5min"]')?.click();
    if (event.key === '4') document.querySelector('.wb-freq-btn[data-freq="daily"]')?.click();
    if (event.key === '5') document.querySelector('.wb-freq-btn[data-freq="weekly"]')?.click();
  });
}

async function wbBootstrap() {
  wbInitTheme();
  wbBindEvents();
  document.getElementById('wb-chart').innerHTML = '<div class="wb-loading">正在创建图表工作台…</div>';
  await wbLoadShell();
  await wbLoadCluster();
  if (!WB_STATE.shell?.session?.ready) {
    wbRenderBootState(WB_STATE.shell.session);
  }
  const deepLink = wbReadUrlTarget();
  const target = deepLink || WB_STATE.shell?.default_target || { label: '沪深300', kind: 'index', freq: '30min' };
  WB_STATE.target = {
    label: target.label,
    kind: target.kind || 'index',
    freq: target.freq || '30min',
  };
  document.getElementById('wb-search-input').value = WB_STATE.target.label;
  await wbLoadSymbol(WB_STATE.target.label, WB_STATE.target.kind);
}

document.addEventListener('DOMContentLoaded', () => {
  wbBootstrap().catch(error => {
    console.error(error);
    wbShowToast(error.message || '初始化失败', 4000);
  });
});
