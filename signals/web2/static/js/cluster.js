/* ═══════════════════════════════════════════════════
   行业聚类页面 — cluster.js
   行业板块（东财）+ 概念板块（THS）双维聚类展示
   ═══════════════════════════════════════════════════ */

let _clusterLoaded = false;

onPageLoad('cluster', () => {
  if (!_clusterLoaded) {
    _clusterLoaded = true;
    _initClusterEvents();
  }
  loadCluster();
});

// ── 事件绑定 ─────────────────────────────────────────
function _initClusterEvents() {
  document.getElementById('cluster-refresh')?.addEventListener('click', refreshCluster);
  document.getElementById('watchlist-scan')?.addEventListener('click', scanWatchlist);
  // Enter 键触发扫描
  document.getElementById('watchlist-direction')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') scanWatchlist();
  });
}

// ── 加载最新聚类 ─────────────────────────────────────
async function loadCluster() {
  const topN = 3;
  const industryGrid = document.getElementById('cluster-cards');
  const conceptGrid = document.getElementById('concept-cards');
  const status = document.getElementById('cluster-meta');
  const industrySrc = document.getElementById('industry-source');
  const conceptSrc = document.getElementById('concept-source');

  industryGrid.innerHTML = '<div class="cl-loading">加载中...</div>';
  conceptGrid.innerHTML = '<div class="cl-loading">加载中...</div>';
  status.textContent = '';

  try {
    const data = await apiFetch(`/api/cluster/latest?top=${topN}`);

    // 行业板块
    const industry = data.industry || data;  // 兼容旧格式
    if (industry.meta?.error && !industry.top?.length) {
      industryGrid.innerHTML = `<div class="cl-empty">${industry.meta.error}</div>`;
    } else {
      _renderClusterCards(industry.top, industryGrid);
      _renderClusterMeta(industry.meta, data.market_status, status);
      if (industrySrc) industrySrc.textContent = industry.meta?.source || '东财';
    }

    // 概念板块
    const concept = data.concept;
    if (concept && concept.top && concept.top.length > 0) {
      _renderClusterCards(concept.top, conceptGrid);
      if (conceptSrc) conceptSrc.textContent = concept.meta?.source || 'THS';
    } else {
      conceptGrid.innerHTML = '<div class="cl-empty">概念聚类数据加载中...</div>';
      if (conceptSrc) conceptSrc.textContent = '';
    }

    // 数据过期提示
    if (data.data_warning) {
      const warnEl = document.createElement('div');
      warnEl.className = 'cl-warning';
      warnEl.style.cssText = 'color:#f59e0b;background:#292524;padding:8px 12px;border-radius:6px;margin-bottom:10px;font-size:13px;';
      warnEl.textContent = '⚠ ' + data.data_warning;
      industryGrid.parentElement?.insertBefore(warnEl, industryGrid);
    }

    loadWeekHistory().then(() => _addPersistenceLabels());
  } catch (e) {
    industryGrid.innerHTML = `<div class="cl-empty">加载失败: ${e.message}</div>`;
    conceptGrid.innerHTML = '';
  }
}

// ── 手动刷新 ─────────────────────────────────────────
async function refreshCluster() {
  const btn = document.getElementById('cluster-refresh');
  btn.disabled = true;
  btn.textContent = '刷新中...';

  try {
    await apiFetch('/api/cluster/refresh');
    showToast('聚类刷新成功');
    await loadCluster();
  } catch (e) {
    showToast('刷新失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '刷新';
  }
}

// ── 渲染聚类卡片 ─────────────────────────────────────
function _renderClusterCards(clusters, container) {
  if (!clusters || clusters.length === 0) {
    container.innerHTML = '<div class="empty-state">暂无聚类数据</div>';
    return;
  }

  container.innerHTML = clusters.map((c, i) => {
    const gainCls = c.avg_gain >= 0 ? 'positive' : 'negative';
    const gainSign = c.avg_gain >= 0 ? '+' : '';
    const breadthPct = (c.avg_breadth * 100).toFixed(0);

    // 成员列表
    const memberRows = c.members.map(m => {
      const mCls = m.gain_pct >= 0 ? 'up' : 'down';
      const mSign = m.gain_pct >= 0 ? '+' : '';
      const leaderInfo = m.leader ? ` <span class="member-leader">${m.leader}</span>` : '';
      const typeInfo = m.type ? ` <span class="member-type">${m.type}</span>` : '';
      return `<div class="member-row">
        <span class="member-name">${m.name}${leaderInfo}${typeInfo}</span>
        <span class="member-gain ${mCls}">${mSign}${m.gain_pct}%</span>
      </div>`;
    }).join('');

    return `<div class="cluster-card" data-rank="${i + 1}">
      <div class="cluster-card-header">
        <div style="display:flex;align-items:center;gap:10px">
          <span class="cluster-rank">#${i + 1}</span>
          <span class="cluster-label">${c.label}</span>
        </div>
        <span class="cluster-score">综合 ${(c.score * 100).toFixed(0)}</span>
      </div>
      <div class="cluster-metrics">
        <div class="cluster-metric"><span class="value ${gainCls}">${gainSign}${c.avg_gain}%</span> 均涨幅</div>
        <div class="cluster-metric"><span class="value">${breadthPct}%</span> 广度</div>
        <div class="cluster-metric"><span class="value">${c.avg_turnover}%</span> 换手</div>
        <div class="cluster-metric"><span class="value">${c.size}</span> 板块</div>
      </div>
      <details class="cluster-members">
        <summary>成员板块 (${c.members.length})</summary>
        <div class="member-list">${memberRows}</div>
      </details>
    </div>`;
  }).join('');
}

// ── 渲染元信息（含市场状态）─────────────────────────
function _renderClusterMeta(meta, marketStatus, el) {
  if (!meta) return;

  // 数据日期 + 星期
  const dateStr = meta.date || '—';
  let weekday = '';
  try {
    const d = new Date(dateStr + 'T00:00:00');
    weekday = ['周日','周一','周二','周三','周四','周五','周六'][d.getDay()];
  } catch(e) {}

  // 市场精细状态
  const ms = marketStatus || {};
  const mkts = ms.markets || {};

  // 当前时间
  const now = new Date();
  const nowStr = `${now.getMonth()+1}-${now.getDate()} ${String(now.getHours()).padStart(2,'0')}:${String(now.getMinutes()).padStart(2,'0')}`;
  const nowWeekday = ['周日','周一','周二','周三','周四','周五','周六'][now.getDay()];

  // 数据源
  const source = meta.source || '—';
  const boards = meta.total_boards || meta.deduped_boards || '—';

  // 渲染单个市场状态 pill
  function _mkt(label, key) {
    const m = mkts[key];
    if (!m) return `<span class="mkt-pill mkt-off">${label} 🔴未知</span>`;
    const detail = m.detail ? `<span class="mkt-detail">${m.detail}</span>` : '';
    const cls = m.icon === '🟢' ? 'mkt-on' : m.icon === '🟠' ? 'mkt-night' : m.icon === '🔵' ? 'mkt-pre' : m.icon === '🟡' ? 'mkt-pause' : 'mkt-off';
    return `<span class="mkt-pill ${cls}">${m.icon} ${label} <b>${m.status}</b> ${detail}</span>`;
  }

  el.innerHTML = `
    <div class="info-grid">
      <div class="info-cell info-date">
        <div class="info-label">最后交易日</div>
        <div class="info-value"><strong>${dateStr}</strong> ${weekday}</div>
      </div>
      <div class="info-cell info-now">
        <div class="info-label">当前时间</div>
        <div class="info-value">${nowStr} ${nowWeekday}</div>
      </div>
      <div class="info-cell info-src">
        <div class="info-label">数据源</div>
        <div class="info-value">${source}</div>
      </div>
      <div class="info-cell info-count">
        <div class="info-label">板块数</div>
        <div class="info-value">${boards}</div>
      </div>
    </div>
    <div class="mkt-status-grid">
      <div class="mkt-group">
        <div class="mkt-group-label">股票</div>
        <div class="mkt-group-pills">
          ${_mkt('A股', 'a_stock')}
          ${_mkt('港股', 'hk_stock')}
          ${_mkt('美股', 'us_stock')}
        </div>
      </div>
      <div class="mkt-group">
        <div class="mkt-group-label">期货</div>
        <div class="mkt-group-pills">
          ${_mkt('股指', 'a_index_futures')}
          ${_mkt('商品', 'a_commodity_futures')}
          ${_mkt('恒指', 'hk_futures')}
          ${_mkt('美期', 'us_futures')}
        </div>
      </div>
      <div class="mkt-group">
        <div class="mkt-group-label">期权</div>
        <div class="mkt-group-pills">
          ${_mkt('A股期权', 'a_options')}
          ${_mkt('美股期权', 'us_options')}
        </div>
      </div>
    </div>
  `;
}

// ── 本周历史 ─────────────────────────────────────────
async function loadWeekHistory() {
  const container = document.getElementById('cluster-history');
  if (!container) return;

  try {
    const data = await apiFetch('/api/cluster/history');

    const week = data.week || [];
    if (week.length === 0) {
      container.innerHTML = '<div class="empty-state">暂无本周历史</div>';
      return;
    }

    let html = `<table class="cl-history-table">
      <thead><tr>
        <th>日期</th><th>#1 主题</th><th>涨幅</th><th>#2 主题</th><th>涨幅</th><th>#3 主题</th><th>涨幅</th>
      </tr></thead><tbody>`;

    for (const day of week) {
      html += '<tr>';
      html += `<td><b>${day.date}</b></td>`;

      const top3 = day.result?.top || [];
      for (let i = 0; i < 3; i++) {
        if (top3[i]) {
          const gainCls = top3[i].avg_gain >= 0 ? 'up' : 'down';
          const sign = top3[i].avg_gain >= 0 ? '+' : '';
          html += `<td>${top3[i].label}</td>`;
          html += `<td class="${gainCls}">${sign}${top3[i].avg_gain}%</td>`;
        } else {
          html += '<td>—</td><td>—</td>';
        }
      }
      html += '</tr>';
    }

    html += '</tbody></table>';
    container.innerHTML = html;
  } catch (e) {
    container.innerHTML = '';
  }
}

// ── 持续性标签（主线 vs 一日游）────────────────────
function _addPersistenceLabels() {
  // 从本周历史表中统计每个板块/概念出现的天数
  const table = document.querySelector('.cl-history-table');
  if (!table) return;

  const rows = table.querySelectorAll('tbody tr');
  const freq = {};  // {板块名: 出现天数}
  rows.forEach(row => {
    const cells = row.querySelectorAll('td');
    // cells: 日期, #1名, #1涨幅, #2名, #2涨幅, #3名, #3涨幅
    for (let i = 1; i < cells.length; i += 2) {
      const name = cells[i]?.textContent?.trim();
      if (name && name !== '—') {
        freq[name] = (freq[name] || 0) + 1;
      }
    }
  });

  // 给当前聚类卡片的成员添加标签
  document.querySelectorAll('.member-name').forEach(el => {
    const name = el.textContent.trim().split(/\s/)[0];
    const count = freq[name] || 0;
    // 移除旧标签
    el.querySelectorAll('.persist-tag').forEach(t => t.remove());
    if (count >= 3) {
      el.insertAdjacentHTML('beforeend', ' <span class="persist-tag persist-main">连续${count}天</span>');
    } else if (count === 0) {
      // 首次出现（本周历史中没有）
      el.insertAdjacentHTML('beforeend', ' <span class="persist-tag persist-new">新</span>');
    }
  });

  // 也给聚类卡片的 label 加标签
  document.querySelectorAll('.cluster-label').forEach(el => {
    const name = el.textContent.trim();
    const count = freq[name] || 0;
    el.querySelectorAll('.persist-tag').forEach(t => t.remove());
    if (count >= 3) {
      el.insertAdjacentHTML('beforeend', ` <span class="persist-tag persist-main">连续${count}天</span>`);
    }
  });
}

// ── 观察池扫描 ───────────────────────────────────────
async function scanWatchlist() {
  const direction = document.getElementById('watchlist-direction')?.value?.trim();
  const mode = document.getElementById('watchlist-mode')?.value || 'belief';
  const container = document.getElementById('watchlist-results');
  const btn = document.getElementById('watchlist-scan');

  if (!direction) {
    showToast('请输入关注方向');
    return;
  }

  btn.disabled = true;
  btn.textContent = '扫描中...';
  container.innerHTML = '<div class="cl-loading">扫描中，每只股票约1-3秒...</div>';

  try {
    const data = await apiFetch(`/api/cluster/watchlist?direction=${encodeURIComponent(direction)}&mode=${mode}&top=30`, 180000);

    if (data.error && !data.results?.length) {
      container.innerHTML = `<div class="cl-empty">${data.error}</div>`;
      return;
    }

    _renderWatchlistResults(data, container, mode);
  } catch (e) {
    container.innerHTML = `<div class="cl-empty">扫描失败: ${e.message}</div>`;
  } finally {
    btn.disabled = false;
    btn.textContent = '扫描';
  }
}

function _renderWatchlistResults(data, container, mode) {
  const results = data.results || [];
  if (results.length === 0) {
    container.innerHTML = '<div class="cl-empty">无结果</div>';
    return;
  }

  const modeLabel = mode === 'panic' ? '恐慌抄底' : '信念方向';
  const gradeA = results.filter(r => r.grade === 'A');
  const gradeB = results.filter(r => r.grade === 'B');
  const gradeC = results.filter(r => r.grade === 'C');

  let html = `<div class="wl-summary">
    ${modeLabel}: ${data.direction} | 共${data.total}只 |
    <span class="wl-grade-a">A级 ${data.grade_a}</span>
    <span class="wl-grade-b">B级 ${data.grade_b}</span>
    <span class="wl-grade-c">C级 ${data.grade_c}</span>
  </div>`;

  if (gradeA.length > 0) {
    html += '<div class="wl-section-title">推荐关注</div>';
    html += _renderWatchlistTable(gradeA, mode);
  }

  if (gradeB.length > 0) {
    html += '<div class="wl-section-title">可观察</div>';
    html += _renderWatchlistTable(gradeB, mode);
  }

  if (gradeC.length > 0) {
    html += `<div class="wl-section-title">暂无信号 (${gradeC.length}只)</div>`;
    if (gradeC.length <= 10) {
      html += _renderWatchlistTable(gradeC, mode);
    } else {
      html += _renderWatchlistTable(gradeC.slice(0, 5), mode);
      html += `<div class="cl-empty">... 还有 ${gradeC.length - 5} 只</div>`;
    }
  }

  container.innerHTML = html;

  // 绑定跳转回测事件
  container.querySelectorAll('.wl-goto-bt').forEach(btn => {
    btn.addEventListener('click', () => {
      const code = btn.dataset.code;
      document.getElementById('bt-code').value = code;
      switchPage('backtest');
      // 自动触发分析
      document.getElementById('bt-analyze')?.click();
    });
  });
}

function _renderWatchlistTable(items, mode) {
  let html = '<table class="wl-table"><thead><tr>';
  html += '<th>代码</th><th>涨跌%</th><th>MA位置</th><th>缠论信号</th><th>量能</th>';
  if (mode === 'panic') html += '<th>割肉</th><th>兑现目标</th>';
  html += '<th></th></tr></thead><tbody>';

  for (const r of items) {
    const cls = r.change_pct >= 0 ? 'up' : 'down';
    const sign = r.change_pct >= 0 ? '+' : '';
    const signals = r.czsc_signals?.join(', ') || '—';
    const code = r.symbol.replace('SH.', '').replace('SZ.', '');
    const gradeCls = r.grade === 'A' ? 'wl-row-a' : r.grade === 'B' ? 'wl-row-b' : '';

    html += `<tr class="${gradeCls}">`;
    html += `<td><b>${r.symbol}</b></td>`;
    html += `<td class="${cls}">${sign}${r.change_pct}%</td>`;
    html += `<td>${r.ma_status || '—'}</td>`;
    html += `<td>${signals}</td>`;
    html += `<td>${r.volume_status || '—'}</td>`;
    if (mode === 'panic') {
      html += `<td>${r.cap_score >= 60 ? '<b>' + r.cap_score + '</b>' : r.cap_score}</td>`;
      html += `<td>${r.target_price > 0 ? r.target_price : '—'}</td>`;
    }
    html += `<td><button class="btn btn-xs wl-goto-bt" data-code="${code}">回测</button></td>`;
    html += '</tr>';
  }

  html += '</tbody></table>';
  return html;
}

// ── 板块成分股展开（点击聚类卡片的成员板块名） ──────
// 成员板块名点击 → 获取成分股列表
document.addEventListener('click', async (e) => {
  const memberName = e.target.closest('.member-name');
  if (!memberName) return;

  const name = memberName.textContent.trim().split(/\s/)[0]; // 取板块名（去掉领涨股等后缀）
  const row = memberName.closest('.member-row');
  if (!row) return;

  // 检查是否已展开
  const existing = row.nextElementSibling;
  if (existing && existing.classList.contains('member-stocks')) {
    existing.remove();
    return;
  }

  // 移除其他展开
  document.querySelectorAll('.member-stocks').forEach(el => el.remove());

  // 插入加载占位
  const stocksDiv = document.createElement('div');
  stocksDiv.className = 'member-stocks';
  stocksDiv.innerHTML = '<span class="cl-loading">加载成分股...</span>';
  row.after(stocksDiv);

  try {
    const data = await apiFetch(`/api/cluster/stocks?board=${encodeURIComponent(name)}`);
    if (data.stocks && data.stocks.length > 0) {
      let html = `<div class="member-stocks-header">${name} 成分股 (${data.total}只, 显示前${data.showing})</div>`;
      html += data.stocks.map(s => {
        const code = s.code || s.symbol.replace('SH.', '').replace('SZ.', '');
        return `<span class="stock-chip" data-code="${code}" title="点击去回测">${code}</span>`;
      }).join('');
      stocksDiv.innerHTML = html;

      // 点击个股代码 → 跳转回测
      stocksDiv.querySelectorAll('.stock-chip').forEach(chip => {
        chip.addEventListener('click', () => {
          document.getElementById('bt-code').value = chip.dataset.code;
          switchPage('backtest');
          document.getElementById('bt-analyze')?.click();
        });
      });
    } else {
      stocksDiv.innerHTML = `<span class="cl-empty">未找到成分股</span>`;
    }
  } catch (e) {
    stocksDiv.innerHTML = `<span class="cl-empty">加载失败</span>`;
  }
});
