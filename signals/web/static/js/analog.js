/**
 * 隆小侠 LONG CLAW — P3-6: 历史形态匹配页
 * 指数选择 → 运行匹配 → 结果卡片 → 叠加图表
 */

let analogChart = null;

// ── 运行匹配 ──────────────────────────────────────────
async function runAnalogMatch() {
  const select = document.getElementById('analog-index-select');
  const indexName = select.value;
  const btn = document.getElementById('analog-run-btn');
  const resultsDiv = document.getElementById('analog-results');

  btn.disabled = true;
  btn.textContent = '匹配中...';
  resultsDiv.innerHTML = '<div class="empty-state">正在运行历史匹配，请稍候...</div>';

  try {
    const data = await apiFetch(`/api/analog/run/${encodeURIComponent(indexName)}`);
    renderAnalogResults(indexName, data.matches || []);
  } catch (err) {
    resultsDiv.innerHTML = `<div class="empty-state">匹配失败: ${err.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '运行匹配';
  }
}

// ── 加载缓存结果 ──────────────────────────────────────
async function loadAnalogCache() {
  try {
    const data = await apiFetch('/api/analog/results');
    if (data && data.results) {
      const select = document.getElementById('analog-index-select');
      const indexName = select.value;
      if (data.results[indexName] && data.results[indexName].length > 0) {
        renderAnalogResults(indexName, data.results[indexName]);
      }
    }
  } catch (e) {
    // 无缓存，不处理
  }
}

// ── 渲染匹配结果卡片 ──────────────────────────────────
function renderAnalogResults(indexName, matches) {
  const container = document.getElementById('analog-results');

  if (!matches || matches.length === 0) {
    container.innerHTML = '<div class="empty-state">未找到足够相似的历史片段</div>';
    document.getElementById('analog-chart-container').style.display = 'none';
    return;
  }

  let html = '<div class="analog-cards">';
  matches.forEach((m, i) => {
    const simPct = ((m.similarity || 0) * 100).toFixed(0);
    const simCls = simPct >= 80 ? 'sim-high' : simPct >= 70 ? 'sim-mid' : 'sim-low';
    const ret10Cls = m.next_10d_return > 0 ? 'up' : m.next_10d_return < 0 ? 'down' : 'flat';
    const ret30Cls = m.next_30d_return > 0 ? 'up' : m.next_30d_return < 0 ? 'down' : 'flat';

    html += `<div class="analog-card" data-idx="${i}" data-start="${m.match_start}" data-end="${m.match_end}">
      <div class="analog-card-header">
        <span class="analog-rank">#${i + 1}</span>
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
      loadAnalogChart(indexName, start, end);
      // 高亮选中
      container.querySelectorAll('.analog-card').forEach(c => c.classList.remove('selected'));
      card.classList.add('selected');
    });
  });

  // 自动加载第一个匹配的图表
  if (matches.length > 0) {
    loadAnalogChart(indexName, matches[0].match_start, matches[0].match_end);
    container.querySelector('.analog-card').classList.add('selected');
  }
}

// ── 叠加图表 ──────────────────────────────────────────
async function loadAnalogChart(indexName, matchStart, matchEnd) {
  const chartContainer = document.getElementById('analog-chart-container');
  chartContainer.style.display = 'block';

  try {
    const data = await apiFetch(
      `/api/analog/chart/${encodeURIComponent(indexName)}?match_start=${matchStart}&match_end=${matchEnd}`
    );

    // 清理旧图表
    if (analogChart) {
      analogChart.remove();
      analogChart = null;
    }
    chartContainer.innerHTML = '';

    // 检查 LightweightCharts
    if (typeof LightweightCharts === 'undefined') {
      chartContainer.innerHTML = '<div class="empty-state">图表库未加载</div>';
      return;
    }

    const isDark = true;
    analogChart = LightweightCharts.createChart(chartContainer, {
      width: chartContainer.clientWidth,
      height: 400,
      layout: {
        background: { type: 'solid', color: 'transparent' },
        textColor: isDark ? '#787b86' : '#333',
      },
      grid: {
        vertLines: { color: isDark ? '#1e222d' : '#e1e3e6' },
        horzLines: { color: isDark ? '#1e222d' : '#e1e3e6' },
      },
      rightPriceScale: {
        borderColor: isDark ? '#363a45' : '#c8c8c8',
      },
      timeScale: {
        borderColor: isDark ? '#363a45' : '#c8c8c8',
        timeVisible: false,
      },
    });

    // 当前走势（实线）
    if (data.current && data.current.length > 0) {
      const currentSeries = analogChart.addLineSeries({
        color: '#2962ff',
        lineWidth: 2,
        title: '当前走势',
      });
      currentSeries.setData(data.current);
    }

    // 历史走势（虚线叠加）
    if (data.historical && data.historical.length > 0) {
      // 历史数据用 day 索引，需要转换为与当前走势相同的时间轴
      // 使用当前走势的时间轴作基准
      if (data.current && data.current.length > 0) {
        const histSeries = analogChart.addLineSeries({
          color: '#f7931a',
          lineWidth: 2,
          lineStyle: 2, // Dashed
          title: `历史 (${matchStart}~${matchEnd})`,
        });

        // 将历史数据映射到当前时间轴
        const histData = data.historical.slice(0, data.current.length).map((h, i) => {
          if (i < data.current.length) {
            return { time: data.current[i].time, value: h.value };
          }
          return null;
        }).filter(Boolean);
        histSeries.setData(histData);

        // 如果历史数据比当前数据长（延展部分），用另一条虚线
        if (data.historical.length > data.current.length) {
          const extSeries = analogChart.addLineSeries({
            color: '#f7931a',
            lineWidth: 1,
            lineStyle: 3, // Dotted
            title: '后续走势',
          });

          // 延展部分需要生成虚拟时间
          const lastTime = data.current[data.current.length - 1].time;
          const extData = [];
          for (let i = data.current.length; i < data.historical.length; i++) {
            // 每天加1天作为虚拟时间
            const dayOffset = i - data.current.length + 1;
            const d = new Date(lastTime);
            d.setDate(d.getDate() + dayOffset);
            const timeStr = d.toISOString().slice(0, 10);
            extData.push({ time: timeStr, value: data.historical[i].value });
          }
          if (extData.length > 0) {
            extSeries.setData(extData);
          }
        }
      }
    }

    analogChart.timeScale().fitContent();

    // 响应式
    const resizeObserver = new ResizeObserver(() => {
      if (analogChart) {
        analogChart.applyOptions({ width: chartContainer.clientWidth });
      }
    });
    resizeObserver.observe(chartContainer);

  } catch (err) {
    chartContainer.innerHTML = `<div class="empty-state">图表加载失败: ${err.message}</div>`;
  }
}

// ── 页面切入时加载缓存 ──────────────────────────────
window.loadAnalogPage = loadAnalogCache;
window.runAnalogMatch = runAnalogMatch;
