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
      _renderClusterMeta(industry.meta, status);
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

// ── 渲染元信息 ───────────────────────────────────────
function _renderClusterMeta(meta, el) {
  if (!meta) return;
  const parts = [];
  if (meta.date) parts.push(meta.date);
  if (meta.source) parts.push(`数据源: ${meta.source}`);
  if (meta.total_boards) parts.push(`${meta.total_boards} 板块`);
  if (meta.n_clusters) parts.push(`${meta.n_clusters} 簇`);
  if (meta.features) parts.push(`${meta.features.length}D特征`);
  el.textContent = parts.join(' | ');
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
