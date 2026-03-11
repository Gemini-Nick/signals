/**
 * 交易日志页面
 */

window.loadTradePage = function() {
  _loadTradeList();
  _loadTradeSummary();
};

// ── 视图切换 ──
document.querySelectorAll('.trade-view-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.trade-view-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.trade-view').forEach(v => v.classList.remove('active'));
    tab.classList.add('active');
    const view = tab.dataset.view;
    const el = document.getElementById('trade-view-' + view);
    if (el) el.classList.add('active');
    if (view === 'stats') _loadTradeStats();
    if (view === 'missed') _loadMissedSignals();
  });
});

// ── 按钮事件 ──
document.getElementById('trade-add-btn')?.addEventListener('click', () => _showTradeForm());
document.getElementById('trade-refresh-btn')?.addEventListener('click', () => {
  _loadTradeList();
  _loadTradeSummary();
});
document.getElementById('trade-filter')?.addEventListener('change', () => _loadTradeList());

// ── 交易列表 ──
async function _loadTradeList() {
  const filter = document.getElementById('trade-filter')?.value || 'all';
  try {
    const r = await fetch(`/api/trade/list?status=${filter}&limit=50`);
    const data = await r.json();
    _renderTradeList(data.trades || []);
  } catch (e) {
    document.getElementById('trade-list').innerHTML = '<div class="empty-state">加载失败</div>';
  }
}

function _renderTradeList(trades) {
  const el = document.getElementById('trade-list');
  if (!trades.length) {
    el.innerHTML = '<div class="empty-state">暂无交易记录，点击"+ 新增交易"添加</div>';
    return;
  }

  let html = '<table class="trade-table"><thead><tr>' +
    '<th>标的</th><th>方向</th><th>入场</th><th>出场</th>' +
    '<th>盈亏%</th><th>评分</th><th>错误</th><th>操作</th>' +
    '</tr></thead><tbody>';

  for (const t of trades) {
    const pnlClass = t.pnl_pct > 0 ? 'pnl-positive' : t.pnl_pct < 0 ? 'pnl-negative' : '';
    const statusBadge = t.is_open
      ? '<span class="trade-badge open">持仓</span>'
      : '<span class="trade-badge closed">已平</span>';

    html += `<tr>
      <td><strong>${t.name || t.symbol}</strong><br><small>${t.symbol}</small></td>
      <td>${t.direction === 'long' ? '做多' : '做空'}</td>
      <td>${t.entry_date}<br><small>@${t.entry_price}</small></td>
      <td>${t.exit_date ? t.exit_date + '<br><small>@' + t.exit_price + '</small>' : statusBadge}</td>
      <td class="${pnlClass}">${t.is_open ? '-' : (t.pnl_pct > 0 ? '+' : '') + t.pnl_pct.toFixed(2) + '%'}</td>
      <td>${t.total_score > 0 ? t.total_score.toFixed(1) : '-'}</td>
      <td>${t.error_type ? '<span class="error-badge error-' + t.error_type + '">' + t.error_type + '</span>' : '-'}</td>
      <td>
        ${t.is_open ? '<button class="trade-action-btn" onclick="window._closeTrade(' + t.id + ')">平仓</button>' : ''}
        <button class="trade-action-btn" onclick="window._scoreTrade(${t.id})">评分</button>
        <button class="trade-action-btn del" onclick="window._deleteTrade(${t.id})">删</button>
      </td>
    </tr>`;
  }
  html += '</tbody></table>';
  el.innerHTML = html;
}

// ── 摘要栏 ──
async function _loadTradeSummary() {
  try {
    const r = await fetch('/api/trade/summary');
    const s = await r.json();
    const el = document.getElementById('trade-summary-bar');
    if (!el) return;
    if (s.total_trades === 0) {
      el.innerHTML = '<div class="trade-summary-item">暂无已平仓交易数据</div>';
      return;
    }
    el.innerHTML = `
      <div class="trade-summary-item"><strong>${s.total_trades}</strong><br>总交易</div>
      <div class="trade-summary-item"><strong>${s.win_rate.toFixed(1)}%</strong><br>胜率</div>
      <div class="trade-summary-item"><strong>${s.avg_pnl_pct > 0 ? '+' : ''}${s.avg_pnl_pct.toFixed(2)}%</strong><br>平均盈亏</div>
      <div class="trade-summary-item"><strong>${s.avg_score > 0 ? s.avg_score.toFixed(1) : '-'}</strong><br>平均评分</div>
      <div class="trade-summary-item"><strong>${s.avg_holding_days.toFixed(0)}天</strong><br>平均持仓</div>
    `;
  } catch (e) {}
}

// ── 统计页 ──
async function _loadTradeStats() {
  const el = document.getElementById('trade-stats');
  try {
    const r = await fetch('/api/trade/summary');
    const s = await r.json();

    let errHtml = '';
    if (s.error_counts && Object.keys(s.error_counts).length > 0) {
      errHtml = '<div class="trade-error-breakdown">';
      const labels = { A: '系统方差', B: '执行偏差', C: '情绪交易' };
      for (const [k, v] of Object.entries(s.error_counts)) {
        errHtml += `<div class="error-item"><span class="error-badge error-${k}">${k}</span> ${labels[k] || k}: ${v}次</div>`;
      }
      errHtml += '</div>';
    }

    el.innerHTML = `
      <div class="trade-stats-grid">
        <div class="trade-stat-card">
          <div class="stat-label">总交易</div>
          <div class="stat-value">${s.total_trades}</div>
        </div>
        <div class="trade-stat-card">
          <div class="stat-label">胜率</div>
          <div class="stat-value">${s.win_rate.toFixed(1)}%</div>
          <div class="stat-detail">${s.win_count}胜 / ${s.loss_count}负</div>
        </div>
        <div class="trade-stat-card">
          <div class="stat-label">最大盈利</div>
          <div class="stat-value pnl-positive">+${s.max_win_pct.toFixed(2)}%</div>
        </div>
        <div class="trade-stat-card">
          <div class="stat-label">最大亏损</div>
          <div class="stat-value pnl-negative">${s.max_loss_pct.toFixed(2)}%</div>
        </div>
        <div class="trade-stat-card">
          <div class="stat-label">平均评分</div>
          <div class="stat-value">${s.avg_score > 0 ? s.avg_score.toFixed(1) + '/5' : '-'}</div>
        </div>
        <div class="trade-stat-card">
          <div class="stat-label">平均持仓</div>
          <div class="stat-value">${s.avg_holding_days.toFixed(0)}天</div>
        </div>
      </div>
      ${errHtml}
    `;
  } catch (e) {
    el.innerHTML = '<div class="empty-state">加载失败</div>';
  }
}

// ── 遗漏信号 ──
async function _loadMissedSignals() {
  const el = document.getElementById('trade-missed');
  try {
    const r = await fetch('/api/trade/missed');
    const data = await r.json();
    const missed = data.missed || [];
    if (!missed.length) {
      el.innerHTML = '<div class="empty-state">暂无遗漏信号记录</div>';
      return;
    }

    let html = '<table class="trade-table"><thead><tr>' +
      '<th>标的</th><th>信号</th><th>信号日</th><th>信号价</th><th>最高价</th><th>潜在收益</th>' +
      '</tr></thead><tbody>';
    for (const m of missed) {
      html += `<tr>
        <td><strong>${m.name || m.symbol}</strong></td>
        <td>${m.signal_type}</td>
        <td>${m.signal_date}</td>
        <td>${m.signal_price.toFixed(2)}</td>
        <td>${m.max_price_after.toFixed(2)}</td>
        <td class="pnl-positive">+${m.potential_pnl_pct.toFixed(2)}%</td>
      </tr>`;
    }
    html += '</tbody></table>';
    el.innerHTML = html;
  } catch (e) {
    el.innerHTML = '<div class="empty-state">加载失败</div>';
  }
}

// ── 新增交易表单 ──
function _showTradeForm(trade) {
  const isEdit = !!trade;
  document.getElementById('trade-form-title').textContent = isEdit ? '编辑交易' : '新增交易';

  const body = document.getElementById('trade-form-body');
  body.innerHTML = `
    <form id="trade-form" class="trade-form">
      <input type="hidden" id="tf-id" value="${trade?.id || ''}" />
      <div class="tf-row">
        <label>标的代码</label>
        <input type="text" id="tf-symbol" value="${trade?.symbol || ''}" placeholder="SZ.002261" required />
      </div>
      <div class="tf-row">
        <label>名称</label>
        <input type="text" id="tf-name" value="${trade?.name || ''}" placeholder="拓维信息" />
      </div>
      <div class="tf-row">
        <label>方向</label>
        <select id="tf-direction">
          <option value="long" ${(!trade || trade.direction === 'long') ? 'selected' : ''}>做多</option>
          <option value="short" ${trade?.direction === 'short' ? 'selected' : ''}>做空</option>
        </select>
      </div>
      <div class="tf-row">
        <label>入场日期</label>
        <input type="date" id="tf-entry-date" value="${trade?.entry_date || new Date().toISOString().slice(0, 10)}" required />
      </div>
      <div class="tf-row">
        <label>入场价</label>
        <input type="number" id="tf-entry-price" value="${trade?.entry_price || ''}" step="0.01" required />
      </div>
      <div class="tf-row">
        <label>入场理由</label>
        <input type="text" id="tf-entry-reason" value="${trade?.entry_reason || ''}" placeholder="一买信号 + 板块强势" />
      </div>
      <div class="tf-row">
        <label>信号类型</label>
        <select id="tf-entry-signal">
          <option value="">手动</option>
          <option value="一买" ${trade?.entry_signal === '一买' ? 'selected' : ''}>一买</option>
          <option value="二买" ${trade?.entry_signal === '二买' ? 'selected' : ''}>二买</option>
          <option value="三买" ${trade?.entry_signal === '三买' ? 'selected' : ''}>三买</option>
          <option value="背驰买" ${trade?.entry_signal === '背驰买' ? 'selected' : ''}>背驰买</option>
          <option value="趋势买" ${trade?.entry_signal === '趋势买' ? 'selected' : ''}>趋势买</option>
        </select>
      </div>
      <div class="tf-row">
        <label>仓位%</label>
        <input type="number" id="tf-position" value="${trade?.position_pct || ''}" step="1" min="0" max="100" />
      </div>
      <div class="tf-row">
        <label>备注</label>
        <textarea id="tf-notes" rows="2">${trade?.notes || ''}</textarea>
      </div>
      <div class="tf-actions">
        <button type="submit" class="stock-search-btn">${isEdit ? '保存' : '添加'}</button>
        <button type="button" class="stock-search-btn" onclick="window._closeTradeForm()">取消</button>
      </div>
    </form>
  `;

  document.getElementById('trade-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const payload = {
      symbol: document.getElementById('tf-symbol').value,
      name: document.getElementById('tf-name').value,
      direction: document.getElementById('tf-direction').value,
      entry_date: document.getElementById('tf-entry-date').value,
      entry_price: parseFloat(document.getElementById('tf-entry-price').value) || 0,
      entry_reason: document.getElementById('tf-entry-reason').value,
      entry_signal: document.getElementById('tf-entry-signal').value,
      position_pct: parseFloat(document.getElementById('tf-position').value) || 0,
      notes: document.getElementById('tf-notes').value,
    };

    const id = document.getElementById('tf-id').value;
    const url = id ? `/api/trade/${id}` : '/api/trade/add';
    const method = id ? 'PUT' : 'POST';

    try {
      await fetch(url, {
        method, headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      });
      window._closeTradeForm();
      _loadTradeList();
      _loadTradeSummary();
    } catch (e) {
      alert('保存失败');
    }
  });

  document.getElementById('trade-form-modal').style.display = 'flex';
}

window._closeTradeForm = function() {
  document.getElementById('trade-form-modal').style.display = 'none';
};

// ── 平仓 ──
window._closeTrade = async function(id) {
  const price = prompt('平仓价格:');
  if (!price) return;
  const reason = prompt('平仓原因 (止盈/止损/信号消失):') || '';
  try {
    await fetch(`/api/trade/${id}/close`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        exit_price: parseFloat(price),
        exit_reason: reason,
      }),
    });
    _loadTradeList();
    _loadTradeSummary();
  } catch (e) {
    alert('平仓失败');
  }
};

// ── 评分 ──
window._scoreTrade = async function(id) {
  const timing = prompt('入场时机评分 (1-5):');
  const position = prompt('仓位管理评分 (1-5):');
  const exit = prompt('出场时机评分 (1-5):');
  const errorType = prompt('错误分类 (A=系统方差, B=执行偏差, C=情绪交易, 留空=无):') || '';

  try {
    await fetch(`/api/trade/${id}/score`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        timing: parseInt(timing) || 0,
        position: parseInt(position) || 0,
        exit: parseInt(exit) || 0,
        error_type: errorType.toUpperCase(),
      }),
    });
    _loadTradeList();
    _loadTradeSummary();
  } catch (e) {
    alert('评分失败');
  }
};

// ── 删除 ──
window._deleteTrade = async function(id) {
  if (!confirm('确认删除此交易记录?')) return;
  try {
    await fetch(`/api/trade/${id}`, { method: 'DELETE' });
    _loadTradeList();
    _loadTradeSummary();
  } catch (e) {
    alert('删除失败');
  }
};
