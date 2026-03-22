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

    loadWeekHistory();
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
