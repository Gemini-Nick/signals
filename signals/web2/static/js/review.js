/* ═══════════════════════════════════════════════════
   🐲 盘后复盘页面 — review.js (web2)
   异步触发 → 轮询进度 → 三层结果展示
   ═══════════════════════════════════════════════════ */

let _rvLoaded = false;
let _rvPollTimer = null;

onPageLoad('review', () => {
  if (!_rvLoaded) {
    _rvLoaded = true;
    _initReviewEvents();
    _loadReviewPresets();
  }
});

function _initReviewEvents() {
  document.getElementById('rv-run')?.addEventListener('click', _runReview);
}

// ── 日期预设 ──────────────────────────────────────
async function _loadReviewPresets() {
  try {
    const presets = await apiFetch('/api/review/presets');
    const container = document.getElementById('rv-presets');
    container.innerHTML = presets.map(p =>
      `<span class="preset-chip" data-key="${p.key}" title="${p.date}">${p.label}</span>`
    ).join('');
    container.querySelectorAll('.preset-chip').forEach(chip => {
      chip.addEventListener('click', () => {
        document.getElementById('rv-date').value = chip.dataset.key;
        _runReview();
      });
    });
  } catch (e) { /* ignore */ }
}

// ── 触发复盘 ──────────────────────────────────────
async function _runReview() {
  const dateInput = document.getElementById('rv-date').value.trim();
  if (!dateInput) { showToast('请输入日期或选择预设'); return; }

  const btn = document.getElementById('rv-run');
  btn.disabled = true;
  btn.textContent = '提交中...';

  try {
    const res = await apiFetch('/api/review/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_date: dateInput }),
    });
    // 用自定义 fetch 因为需要 POST
  } catch (e) {
    // 用原生 fetch
  }

  // 直接用原生 fetch POST
  try {
    const resp = await fetch('/api/review/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_date: dateInput }),
    });
    const data = await resp.json();
    if (!data.ok) { showToast(data.message || '提交失败'); btn.disabled = false; btn.textContent = '开始复盘'; return; }
    showToast(`复盘已提交: ${data.start_date}`);
    _startPolling();
  } catch (e) {
    showToast('提交失败: ' + e.message);
    btn.disabled = false;
    btn.textContent = '开始复盘';
  }
}

// ── 轮询进度 ──────────────────────────────────────
function _startPolling() {
  const progress = document.getElementById('rv-progress');
  const results = document.getElementById('rv-results');
  progress.style.display = 'block';
  results.innerHTML = '';

  if (_rvPollTimer) clearInterval(_rvPollTimer);
  _rvPollTimer = setInterval(async () => {
    try {
      const status = await apiFetch('/api/review/status');
      const phaseEl = document.getElementById('rv-phase');

      if (status.completed) {
        clearInterval(_rvPollTimer);
        _rvPollTimer = null;
        phaseEl.textContent = '完成';
        progress.style.display = 'none';
        document.getElementById('rv-run').disabled = false;
        document.getElementById('rv-run').textContent = '开始复盘';
        _loadReviewResults();
        return;
      }

      if (status.error) {
        clearInterval(_rvPollTimer);
        phaseEl.textContent = '错误: ' + status.error;
        document.getElementById('rv-run').disabled = false;
        document.getElementById('rv-run').textContent = '开始复盘';
        return;
      }

      const phases = { L1: '指数分析', L2: '行业分析', L3: '标的筛选' };
      const label = phases[status.phase] || status.phase || '准备中';
      phaseEl.textContent = `${label}... ${status.phase_detail || ''}`;

    } catch (e) { /* continue polling */ }
  }, 2000);
}

// ── 加载结果 ──────────────────────────────────────
async function _loadReviewResults() {
  try {
    const data = await apiFetch('/api/review/results');
    _renderReviewResults(data);
  } catch (e) {
    showToast('加载结果失败: ' + e.message);
  }
}

function _renderReviewResults(data) {
  const container = document.getElementById('rv-results');
  let html = '';

  // 时间信息
  const timing = data.timing || {};
  const totalTime = Object.values(timing).reduce((a, b) => a + b, 0);
  html += `<div class="meta-text" style="margin-bottom:16px">
    ${data.start_label || ''} ${data.start_date} | 总耗时 ${totalTime.toFixed(1)}s
    (L1: ${(timing.L1||0).toFixed(1)}s, L2: ${(timing.L2||0).toFixed(1)}s, L3: ${(timing.L3||0).toFixed(1)}s)
  </div>`;

  // L1 大盘方向
  if (data.banner) {
    const b = data.banner;
    const dirCls = b.overall_direction?.includes('多') ? 'up' : b.overall_direction?.includes('空') ? 'down' : 'flat';
    html += `<div class="rv-banner ${dirCls}">
      <div class="rv-banner-dir">${b.overall_direction || '—'}</div>
      <div class="rv-banner-meta">
        情绪: ${b.sentiment_phase || '—'} | 仓位: ${b.position_suggestion || '—'} | 轮动: ${b.rotation_stage || '—'}
      </div>
      ${b.summary ? `<div class="rv-banner-summary">${b.summary}</div>` : ''}
    </div>`;
  }

  // 指数卡片
  if (data.index_reports?.length) {
    html += '<div class="section-title">指数分析</div><div class="rv-index-cards stagger-in">';
    data.index_reports.forEach(r => {
      if (!r.data_available) return;
      const priceCls = (r.intraday_change || 0) >= 0 ? 'up' : 'down';
      html += `<div class="rv-index-card">
        <div class="rv-index-name">${r.name}</div>
        <div class="rv-index-price ${priceCls}">${r.latest_price}</div>
        <div class="rv-index-trend">${r.daily_trend || '—'}</div>
        ${r.has_buy_signal ? '<span class="badge badge-macd">买</span>' : ''}
        ${r.has_sell_signal ? '<span class="badge badge-czsc">卖</span>' : ''}
      </div>`;
    });
    html += '</div>';
  }

  // L2 行业榜
  if (data.gain_list?.length) {
    html += '<div class="section-title">行业涨幅榜</div><div class="rv-industry-list">';
    data.gain_list.slice(0, 10).forEach((ind, i) => {
      const gainCls = ind.gain_pct >= 0 ? 'up' : 'down';
      html += `<div class="rv-industry-row">
        <span class="rv-ind-rank">${i + 1}</span>
        <span class="rv-ind-name">${ind.display_name || ind.name}</span>
        <span class="${gainCls} mono">${ind.gain_pct >= 0 ? '+' : ''}${ind.gain_pct}%</span>
        <span class="meta-text">综合 ${ind.composite_score}</span>
        ${ind.rhythm_phase ? `<span class="source-badge">${ind.rhythm_phase}</span>` : ''}
      </div>`;
    });
    html += '</div>';
  }

  // 轮动
  if (data.rotation?.stage) {
    html += `<div class="section-title">轮动研判</div>
      <div class="rv-rotation">
        <span class="rv-rotation-stage">${data.rotation.stage}</span>
        <span>${data.rotation.detail || ''}</span>
        <div class="rv-rotation-alloc">${data.rotation.allocation || ''}</div>
      </div>`;
  }

  // L3 个股信号
  if (data.scored_symbols?.length) {
    html += `<div class="section-title">个股信号 (${data.scored_symbols.length})</div>`;
    html += '<div class="rv-stock-list">';
    data.scored_symbols.forEach(s => {
      const dirCls = s.direction === '偏多' ? 'up' : s.direction === '偏空' ? 'down' : '';
      html += `<div class="rv-stock-row">
        <span class="rv-stock-name">${s.name || s.symbol}</span>
        <span class="mono ${dirCls}">${s.total_score}</span>
        <span class="${dirCls}">${s.direction}</span>
        <span class="meta-text">${s.signal_count}信号</span>
        ${s.confidence_level ? `<span class="source-badge">${s.confidence_level}</span>` : ''}
      </div>`;
    });
    html += '</div>';
  }

  container.innerHTML = html;
  // 触发 stagger
  container.querySelectorAll('.stagger-in').forEach(el => triggerStagger(el));
}
