/**
 * 隆小侠 LONG CLAW — P3-6: 历史形态匹配页 (专业版)
 *
 * 功能:
 * 1. 加载完整K线 + MACD 图表
 * 2. 用户手动输入日期区间 或 在图表上拖选
 * 3. 选定区间高亮显示
 * 4. 运行匹配 → 结果卡片 → 叠加对比图
 */

let analogKlineChart = null;
let analogCandleSeries = null;
let analogVolumeSeries = null;
let analogMacdBarSeries = null;
let analogMacdDifSeries = null;
let analogMacdDeaSeries = null;
let analogMaSeries = [];
let analogOverlayChart = null;
let currentKlineData = null;   // 缓存 K 线数据
let selectionMarkers = [];     // 选区标记

// ── 页面初始化 ────────────────────────────────────────
async function loadAnalogPage() {
  const select = document.getElementById('analog-index-select');
  // 加载当前选中指数的K线图
  await loadAnalogKline(select.value);
}

// ── 指数切换 ────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  const select = document.getElementById('analog-index-select');
  if (select) {
    select.addEventListener('change', () => {
      clearAnalogRange();
      loadAnalogKline(select.value);
    });
  }
});

// ── 清除日期选区 ────────────────────────────────────
function clearAnalogRange() {
  document.getElementById('analog-start-date').value = '';
  document.getElementById('analog-end-date').value = '';
  updateSelectionHighlight();
  document.getElementById('analog-info').textContent = 'Pearson相关系数 | Top5匹配';
}

// ── 加载K线主图 ────────────────────────────────────
async function loadAnalogKline(indexName) {
  const container = document.getElementById('analog-kline-container');

  try {
    const data = await apiFetch(`/api/analog/kline/${encodeURIComponent(indexName)}`);
    currentKlineData = data;
    renderKlineChart(container, data);
  } catch (err) {
    container.innerHTML = `<div class="empty-state">K线加载失败: ${err.message}</div>`;
  }
}

// ── 渲染专业K线 + MACD ──────────────────────────────
function renderKlineChart(container, data) {
  // 清理旧图表
  if (analogKlineChart) {
    analogKlineChart.remove();
    analogKlineChart = null;
  }
  container.innerHTML = '';

  if (!data.ohlcv || data.ohlcv.length === 0) {
    container.innerHTML = '<div class="empty-state">无K线数据</div>';
    return;
  }

  if (typeof LightweightCharts === 'undefined') {
    container.innerHTML = '<div class="empty-state">图表库未加载</div>';
    return;
  }

  const upColor = '#f23645';
  const downColor = '#26a69a';

  analogKlineChart = LightweightCharts.createChart(container, {
    width: container.clientWidth,
    height: container.clientHeight || 380,
    layout: {
      background: { type: 'solid', color: '#131722' },
      textColor: '#787b86',
    },
    grid: {
      vertLines: { color: '#1e222d' },
      horzLines: { color: '#1e222d' },
    },
    crosshair: {
      vertLine: { color: '#787b86', width: 1, style: 2 },
      horzLine: { color: '#787b86', width: 1, style: 2 },
    },
    timeScale: {
      borderColor: '#363a45',
      timeVisible: false,
    },
    rightPriceScale: {
      borderColor: '#363a45',
    },
  });

  // K线
  analogCandleSeries = analogKlineChart.addCandlestickSeries({
    upColor, downColor,
    borderUpColor: upColor, borderDownColor: downColor,
    wickUpColor: upColor, wickDownColor: downColor,
    priceScaleId: 'right',
  });
  analogCandleSeries.setData(data.ohlcv);

  // 成交量
  analogVolumeSeries = analogKlineChart.addHistogramSeries({
    priceFormat: { type: 'volume' },
    priceScaleId: 'volume',
  });
  analogKlineChart.priceScale('volume').applyOptions({
    scaleMargins: { top: 0.75, bottom: 0.17 },
  });
  analogVolumeSeries.setData(data.ohlcv.map(bar => ({
    time: bar.time,
    value: bar.volume,
    color: bar.close >= bar.open
      ? 'rgba(242,54,69,0.4)'
      : 'rgba(38,166,154,0.4)',
  })));

  // MA 均线
  analogMaSeries = [];
  if (data.ma_lines) {
    data.ma_lines.forEach(ma => {
      if (!ma.data || ma.data.length < 2) return;
      const s = analogKlineChart.addLineSeries({
        color: ma.color,
        lineWidth: 1,
        crosshairMarkerVisible: false,
        priceLineVisible: false,
        lastValueVisible: false,
        title: '',
        priceScaleId: '',
      });
      s.setData(ma.data);
      analogMaSeries.push(s);
    });
  }

  // MACD 子图
  if (data.macd && data.macd.length > 2) {
    analogMacdBarSeries = analogKlineChart.addHistogramSeries({
      priceScaleId: 'macd',
      priceFormat: { type: 'price', precision: 2, minMove: 0.01 },
      lastValueVisible: false,
    });
    analogKlineChart.priceScale('macd').applyOptions({
      scaleMargins: { top: 0.87, bottom: 0 },
    });
    analogMacdBarSeries.setData(data.macd.map(d => ({
      time: d.time,
      value: d.bar,
      color: d.bar >= 0 ? 'rgba(242,54,69,0.6)' : 'rgba(38,166,154,0.6)',
    })));

    analogMacdDifSeries = analogKlineChart.addLineSeries({
      color: '#f7931a', lineWidth: 1,
      priceScaleId: 'macd',
      crosshairMarkerVisible: false, priceLineVisible: false,
      lastValueVisible: false,
    });
    analogMacdDifSeries.setData(data.macd.map(d => ({ time: d.time, value: d.dif })));

    analogMacdDeaSeries = analogKlineChart.addLineSeries({
      color: '#2962ff', lineWidth: 1,
      priceScaleId: 'macd',
      crosshairMarkerVisible: false, priceLineVisible: false,
      lastValueVisible: false,
    });
    analogMacdDeaSeries.setData(data.macd.map(d => ({ time: d.time, value: d.dea })));
  }

  // MA 图例 overlay
  createAnalogMALegend(container, data);

  // 响应式
  const ro = new ResizeObserver(() => {
    if (analogKlineChart) {
      analogKlineChart.applyOptions({ width: container.clientWidth });
    }
  });
  ro.observe(container);

  // 日期输入联动 → 选区高亮
  const startInput = document.getElementById('analog-start-date');
  const endInput = document.getElementById('analog-end-date');
  const onDateChange = () => {
    updateSelectionHighlight();
    updateInfoText();
  };
  startInput.addEventListener('change', onDateChange);
  endInput.addEventListener('change', onDateChange);

  // 恢复已有选区
  updateSelectionHighlight();

  analogKlineChart.timeScale().fitContent();
}

// ── MA 图例 ──────────────────────────────────────────
function createAnalogMALegend(container, data) {
  let legend = container.querySelector('.analog-ma-legend');
  if (!legend) {
    legend = document.createElement('div');
    legend.className = 'analog-ma-legend ma-legend';
    container.appendChild(legend);
  }

  if (!data.ma_lines || data.ma_lines.length === 0) {
    legend.style.display = 'none';
    return;
  }

  const items = data.ma_lines.map(ma => {
    const lastVal = ma.data && ma.data.length > 0 ? ma.data[ma.data.length - 1].value : 0;
    return `<span class="ma-legend-item" style="color:${ma.color}">${ma.label}: ${lastVal.toFixed(0)}</span>`;
  });
  legend.innerHTML = items.join(' <span class="ma-legend-sep">|</span> ');
  legend.style.display = '';

  // crosshair 联动
  if (analogKlineChart && analogMaSeries.length > 0) {
    analogKlineChart.subscribeCrosshairMove(param => {
      if (!param || !param.seriesData) return;
      const parts = data.ma_lines.map((ma, i) => {
        let val = '—';
        if (i < analogMaSeries.length) {
          const d = param.seriesData.get(analogMaSeries[i]);
          if (d && d.value !== undefined) val = d.value.toFixed(0);
        }
        return `<span class="ma-legend-item" style="color:${ma.color}">${ma.label}: ${val}</span>`;
      });
      legend.innerHTML = parts.join(' <span class="ma-legend-sep">|</span> ');
    });
  }
}

// ── 选区高亮 ──────────────────────────────────────────
function updateSelectionHighlight() {
  if (!analogCandleSeries || !currentKlineData) return;

  const startDate = document.getElementById('analog-start-date').value;
  const endDate = document.getElementById('analog-end-date').value;

  if (!startDate || !endDate) {
    analogCandleSeries.setMarkers([]);
    return;
  }

  // 在选区起止位置添加标记
  const markers = [];
  const ohlcv = currentKlineData.ohlcv;

  // 找到选区内第一根和最后一根K线
  let firstBar = null, lastBar = null;
  let count = 0;
  for (const bar of ohlcv) {
    if (bar.time >= startDate && bar.time <= endDate) {
      if (!firstBar) firstBar = bar;
      lastBar = bar;
      count++;
    }
  }

  if (firstBar) {
    markers.push({
      time: firstBar.time,
      position: 'aboveBar',
      color: '#f7931a',
      shape: 'arrowDown',
      text: '选区起',
    });
  }
  if (lastBar && lastBar.time !== (firstBar && firstBar.time)) {
    markers.push({
      time: lastBar.time,
      position: 'aboveBar',
      color: '#f7931a',
      shape: 'arrowDown',
      text: `选区止 (${count}天)`,
    });
  }

  analogCandleSeries.setMarkers(markers);
}

// ── 更新信息文字 ──────────────────────────────────────
function updateInfoText() {
  const startDate = document.getElementById('analog-start-date').value;
  const endDate = document.getElementById('analog-end-date').value;
  const info = document.getElementById('analog-info');

  if (startDate && endDate && currentKlineData) {
    let count = 0;
    for (const bar of currentKlineData.ohlcv) {
      if (bar.time >= startDate && bar.time <= endDate) count++;
    }
    info.textContent = `选区: ${startDate} ~ ${endDate} (${count}交易日) | Pearson相关系数 | Top5`;
  } else {
    info.textContent = '默认最近30天 | Pearson相关系数 | Top5匹配';
  }
}

// ── 运行匹配 ──────────────────────────────────────────
async function runAnalogMatch() {
  const select = document.getElementById('analog-index-select');
  const indexName = select.value;
  const btn = document.getElementById('analog-run-btn');
  const resultsDiv = document.getElementById('analog-results');

  const startDate = document.getElementById('analog-start-date').value;
  const endDate = document.getElementById('analog-end-date').value;

  btn.disabled = true;
  btn.textContent = '匹配中...';
  resultsDiv.innerHTML = '<div class="empty-state">正在运行历史匹配，请稍候...</div>';

  try {
    let url = `/api/analog/run/${encodeURIComponent(indexName)}`;
    if (startDate && endDate) {
      url += `?start_date=${startDate}&end_date=${endDate}`;
    }
    const data = await apiFetch(url);
    renderAnalogResults(indexName, data.matches || [], startDate, endDate);
  } catch (err) {
    resultsDiv.innerHTML = `<div class="empty-state">匹配失败: ${err.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '运行匹配';
  }
}

// ── 渲染匹配结果卡片 ──────────────────────────────────
function renderAnalogResults(indexName, matches, selStart, selEnd) {
  const container = document.getElementById('analog-results');

  if (!matches || matches.length === 0) {
    container.innerHTML = '<div class="empty-state">未找到足够相似的历史片段（可尝试降低阈值或更换指数）</div>';
    document.getElementById('analog-chart-container').style.display = 'none';
    return;
  }

  let html = '<div class="analog-cards">';
  matches.forEach((m, i) => {
    const simPct = ((m.similarity || 0) * 100).toFixed(0);
    const simCls = simPct >= 70 ? 'sim-high' : simPct >= 50 ? 'sim-mid' : 'sim-low';
    const ret10Cls = m.next_10d_return > 0 ? 'up' : m.next_10d_return < 0 ? 'down' : 'flat';
    const ret30Cls = m.next_30d_return > 0 ? 'up' : m.next_30d_return < 0 ? 'down' : 'flat';

    html += `<div class="analog-card" data-idx="${i}" data-start="${m.match_start}" data-end="${m.match_end}">
      <div class="analog-card-header">
        <span class="analog-rank">#${i + 1}</span>
        <span class="analog-window">${m.window_days}天</span>
        <span class="similarity-badge ${simCls}">${simPct}%</span>
      </div>
      <div class="analog-card-period">${m.match_start} ~ ${m.match_end}</div>
      <div class="analog-card-returns">
        <div class="analog-return">
          <span class="analog-return-label">10日</span>
          <span class="analog-return-value ${ret10Cls}">${m.next_10d_return > 0 ? '+' : ''}${m.next_10d_return.toFixed(1)}%</span>
        </div>
        <div class="analog-return">
          <span class="analog-return-label">30日</span>
          <span class="analog-return-value ${ret30Cls}">${m.next_30d_return > 0 ? '+' : ''}${m.next_30d_return.toFixed(1)}%</span>
        </div>
      </div>
      ${m.what_happened ? `<div class="analog-card-desc">${m.what_happened}</div>` : ''}
      ${m.key_observation ? `<div class="analog-card-obs">${m.key_observation}</div>` : ''}
    </div>`;
  });
  html += '</div>';
  container.innerHTML = html;

  // 点击卡片加载叠加图表
  container.querySelectorAll('.analog-card').forEach(card => {
    card.style.cursor = 'pointer';
    card.addEventListener('click', () => {
      const start = card.dataset.start;
      const end = card.dataset.end;
      loadAnalogOverlayChart(indexName, start, end, selStart, selEnd);
      container.querySelectorAll('.analog-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
    });
  });

  // 自动加载第一个
  if (matches.length > 0) {
    loadAnalogOverlayChart(indexName, matches[0].match_start, matches[0].match_end, selStart, selEnd);
    container.querySelector('.analog-card').classList.add('selected');
  }
}

// ── 叠加对比图 ──────────────────────────────────────
async function loadAnalogOverlayChart(indexName, matchStart, matchEnd, selStart, selEnd) {
  const chartContainer = document.getElementById('analog-chart-container');
  chartContainer.style.display = 'block';

  try {
    let url = `/api/analog/chart/${encodeURIComponent(indexName)}?match_start=${matchStart}&match_end=${matchEnd}`;
    if (selStart && selEnd) {
      url += `&sel_start=${selStart}&sel_end=${selEnd}`;
    }

    const data = await apiFetch(url);

    // 清理旧图表
    if (analogOverlayChart) {
      analogOverlayChart.remove();
      analogOverlayChart = null;
    }
    chartContainer.innerHTML = '';

    if (typeof LightweightCharts === 'undefined') {
      chartContainer.innerHTML = '<div class="empty-state">图表库未加载</div>';
      return;
    }

    // 创建标题
    const titleDiv = document.createElement('div');
    titleDiv.className = 'analog-overlay-title';
    titleDiv.innerHTML = `<span style="color:#2962ff">━</span> 选定走势 &nbsp;&nbsp; <span style="color:#f7931a">╍╍</span> 历史匹配 (${matchStart}~${matchEnd}) &nbsp;&nbsp; <span style="color:#f7931a;opacity:0.5">···</span> 后续走势`;
    chartContainer.appendChild(titleDiv);

    const chartDiv = document.createElement('div');
    chartDiv.style.height = '300px';
    chartContainer.appendChild(chartDiv);

    analogOverlayChart = LightweightCharts.createChart(chartDiv, {
      width: chartContainer.clientWidth,
      height: 300,
      layout: {
        background: { type: 'solid', color: '#131722' },
        textColor: '#787b86',
      },
      grid: {
        vertLines: { color: '#1e222d' },
        horzLines: { color: '#1e222d' },
      },
      rightPriceScale: {
        borderColor: '#363a45',
      },
      timeScale: {
        borderColor: '#363a45',
        timeVisible: false,
        rightOffset: 5,
      },
    });

    // 当前走势（实线 蓝色）
    if (data.current && data.current.length > 0) {
      const currentSeries = analogOverlayChart.addLineSeries({
        color: '#2962ff',
        lineWidth: 2,
        title: '',
      });
      currentSeries.setData(data.current);
    }

    // 历史走势（虚线 橙色）
    if (data.historical && data.historical.length > 0 && data.current && data.current.length > 0) {
      const matchLen = data.historical.filter(h => !h.is_future).length;
      const windowLen = Math.min(matchLen, data.current.length);

      // 匹配部分 — 映射到当前时间轴
      const histMatchSeries = analogOverlayChart.addLineSeries({
        color: '#f7931a',
        lineWidth: 2,
        lineStyle: 2,  // Dashed
        title: '',
      });
      const histMatchData = data.historical.slice(0, windowLen).map((h, i) => ({
        time: data.current[i].time,
        value: h.value,
      }));
      histMatchSeries.setData(histMatchData);

      // 后续走势 (未来部分) — 延伸时间轴
      const futureData = data.historical.filter(h => h.is_future);
      if (futureData.length > 0 && data.current.length > 0) {
        const extSeries = analogOverlayChart.addLineSeries({
          color: '#f7931a',
          lineWidth: 1,
          lineStyle: 3, // Dotted
          title: '',
        });
        const lastTime = data.current[data.current.length - 1].time;
        const extData = futureData.map((h, i) => {
          const d = new Date(lastTime);
          d.setDate(d.getDate() + i + 1);
          return { time: d.toISOString().slice(0, 10), value: h.value };
        });
        extSeries.setData(extData);
      }
    }

    analogOverlayChart.timeScale().fitContent();

    // 响应式
    new ResizeObserver(() => {
      if (analogOverlayChart) {
        analogOverlayChart.applyOptions({ width: chartContainer.clientWidth });
      }
    }).observe(chartContainer);

  } catch (err) {
    chartContainer.innerHTML = `<div class="empty-state">图表加载失败: ${err.message}</div>`;
  }
}

// ── 暴露给全局 ──────────────────────────────────────
window.loadAnalogPage = loadAnalogPage;
window.runAnalogMatch = runAnalogMatch;
window.clearAnalogRange = clearAnalogRange;
