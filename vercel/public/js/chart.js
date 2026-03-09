/**
 * 隆小侠 LONG CLAW — K线图表 + CZSC 缠论叠加
 * 使用 TradingView Lightweight Charts v4
 */

// ── 图表状态 ─────────────────────────────────────────
let chart = null;
let candleSeries = null;
let volumeSeries = null;
let biSeries = null;         // 笔线 (LineSeries)
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
    zhongshuFill: cssVar('--zhongshu-fill') || 'rgba(41,98,255,0.12)',
    zhongshuStroke: cssVar('--zhongshu-stroke') || 'rgba(41,98,255,0.35)',
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
    height: container.clientHeight || 500,
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
  });

  // 成交量
  volumeSeries = chart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  chart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.8, bottom: 0 },
  });

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

  // 移除旧笔线
  if (biSeries) {
    chart.removeSeries(biSeries);
    biSeries = null;
  }

  if (!biList || biList.length === 0) return;

  // 构建笔线端点序列
  // 每根笔有 sdt(起) 和 edt(止)，方向决定哪端是高/低
  const points = [];
  biList.forEach((bi, i) => {
    if (bi.direction === 'up') {
      points.push({ time: bi.sdt, value: bi.low });
      points.push({ time: bi.edt, value: bi.high });
    } else {
      points.push({ time: bi.sdt, value: bi.high });
      points.push({ time: bi.edt, value: bi.low });
    }
  });

  // 去重 + 按时间排序（Lightweight Charts 要求 time 单调递增）
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

  // 合并相同时间戳的点（取最后一个）
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

// ── 绘制买卖点标记 ──────────────────────────────────
function drawSignalMarkers(signals) {
  if (!candleSeries || !signals || signals.length === 0) return;

  const c = chartColors();
  const markers = signals.map(s => {
    const isBuy = s.type.includes('买');
    return {
      time: s.dt,
      position: isBuy ? 'belowBar' : 'aboveBar',
      color: isBuy ? c.signalBuy : c.signalSell,
      shape: isBuy ? 'arrowUp' : 'arrowDown',
      text: s.type,
    };
  });

  // 按时间排序
  markers.sort((a, b) => a.time - b.time);
  candleSeries.setMarkers(markers);
}

// ── 信号详情面板 ─────────────────────────────────────
function renderSignalDetails(data) {
  const body = document.getElementById('signal-details-body');
  const { signals, meta } = data;

  if (!signals || signals.length === 0) {
    body.innerHTML = '<div class="detail-item" style="color:var(--text-muted);">无信号</div>';
    return;
  }

  body.innerHTML = signals.map(s => `
    <div class="detail-item">
      <span class="detail-freq">[${s.freq}]</span>
      <span class="detail-type">${s.type}</span>
      conf=${s.confidence.toFixed(2)} @ ${s.price.toFixed(2)}
      <div class="detail-desc">${s.details || ''}</div>
    </div>
  `).join('');
}

// ── 摘要栏 ───────────────────────────────────────────
function renderChartSummary(data) {
  const summary = document.getElementById('chart-summary');
  const { meta, signals } = data;

  const buyCount = signals.filter(s => s.type.includes('买')).length;
  const sellCount = signals.filter(s => s.type.includes('卖')).length;
  const direction = buyCount > sellCount ? '偏多' : sellCount > buyCount ? '偏空' : '中性';

  summary.innerHTML = `
    <span class="summary-item">方向: <span class="summary-value">${direction}</span></span>
    <span class="summary-item">买信号: <span class="summary-value">${buyCount}</span></span>
    <span class="summary-item">卖信号: <span class="summary-value">${sellCount}</span></span>
    <span class="summary-item">周期: <span class="summary-value">${meta.freq}</span></span>
  `;
}

// ── 加载图表数据 ─────────────────────────────────────
async function loadChart(indexName, freq) {
  currentSymbol = indexName;
  currentFreq = freq || 'daily';

  // 更新标题
  document.getElementById('chart-title').textContent = indexName;

  // 更新周期按钮激活状态
  document.querySelectorAll('.freq-btn').forEach(btn => {
    btn.classList.toggle('active', btn.dataset.freq === currentFreq);
  });

  try {
    const data = await apiFetch(`/api/chart/${encodeURIComponent(indexName)}?freq=${currentFreq}`);

    // 创建/重建图表
    createChart();

    // K线数据
    if (data.ohlcv && data.ohlcv.length > 0) {
      candleSeries.setData(data.ohlcv);

      // 成交量（带涨跌颜色）
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
    drawSignalMarkers(data.signals);

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
