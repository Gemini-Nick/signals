/**
 * 盘后复盘页 — review.js
 * 日期选择 → 运行复盘 → 轮询进度 → 渲染结果
 */

let _reviewLoaded = false;
let _pollTimer = null;

window.loadReviewPage = function () {
  if (!_reviewLoaded) {
    _loadPresets();
    // 绑定按钮
    document.getElementById('review-run-btn').addEventListener('click', window.runReview);
    _reviewLoaded = true;
  }
  // 如果有缓存结果，检查是否正在运行
  _checkRunningStatus();
};

/* ── 加载日期预设 ── */
async function _loadPresets() {
  try {
    const presets = await apiFetch('/api/review/presets');
    const sel = document.getElementById('review-date-select');
    sel.innerHTML = '<option value="">-- 选择预设日期 --</option>';
    presets.forEach(p => {
      const opt = document.createElement('option');
      opt.value = p.key;
      opt.textContent = `${p.label} (${p.date})`;
      sel.appendChild(opt);
    });
  } catch (e) {
    console.error('加载预设失败', e);
  }
}

/* ── 检查是否有运行中的任务 ── */
async function _checkRunningStatus() {
  try {
    const st = await apiFetch('/api/review/status');
    if (st.is_running) {
      _showLoading(st.phase);
      _startPoll();
    } else if (st.completed) {
      _loadResults();
    }
  } catch (_) { /* ignore */ }
}

/* ── 运行复盘 ── */
window.runReview = async function () {
  const sel = document.getElementById('review-date-select');
  const custom = document.getElementById('review-date-custom');
  const dateVal = custom.value.trim() || sel.value;
  if (!dateVal) {
    alert('请选择或输入日期');
    return;
  }

  // 禁用按钮
  const btn = document.getElementById('review-run-btn');
  btn.disabled = true;

  try {
    const res = await fetch(API_BASE + '/api/review/run', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ start_date: dateVal }),
    });
    const data = await res.json();
    if (!data.ok) {
      alert(data.message || '启动失败');
      btn.disabled = false;
      return;
    }
    _reviewStartTime = Date.now();
    _showLoading('L1');
    _startPoll();
  } catch (e) {
    alert('请求失败: ' + e.message);
    btn.disabled = false;
  }
};

/* ── Loading 状态 ── */
let _reviewStartTime = 0;

function _showLoading(phase, detail, timing) {
  document.getElementById('review-loading').style.display = 'block';
  document.getElementById('review-results').style.display = 'none';
  const phaseMap = {
    L1: '正在分析指数结构 (L1)',
    L2: '正在分析行业排行 (L2)',
    L3: '正在分析个股信号 (L3)',
  };
  const elapsed = _reviewStartTime ? Math.round((Date.now() - _reviewStartTime) / 1000) : 0;
  let text = (phaseMap[phase] || '分析中') + '...';
  if (elapsed > 0) text += `  [已用 ${elapsed}s]`;
  // 已完成阶段的耗时
  if (timing) {
    const parts = [];
    if (timing.L1 && phase !== 'L1') parts.push(`L1:${timing.L1}s`);
    if (timing.L2 && phase !== 'L2' && phase !== 'L1') parts.push(`L2:${timing.L2}s`);
    if (parts.length) text += `  (${parts.join(' ')})`;
  }
  if (detail) text += `\n${detail}`;
  document.getElementById('review-loading-phase').textContent = text;
}

function _hideLoading() {
  document.getElementById('review-loading').style.display = 'none';
  document.getElementById('review-run-btn').disabled = false;
}

/* ── 轮询 ── */
function _startPoll() {
  if (_pollTimer) clearInterval(_pollTimer);
  _pollTimer = setInterval(async () => {
    try {
      const st = await apiFetch('/api/review/status');
      if (st.is_running) {
        _showLoading(st.phase, st.phase_detail, st.timing);
      } else {
        clearInterval(_pollTimer);
        _pollTimer = null;
        _hideLoading();
        if (st.error) {
          alert('复盘失败: ' + st.error);
        } else if (st.completed) {
          _loadResults();
        }
      }
    } catch (_) { /* retry */ }
  }, 2000);
}

/* ── 加载结果 ── */
async function _loadResults() {
  try {
    const data = await apiFetch('/api/review/results');
    const container = document.getElementById('review-results');
    container.style.display = 'block';
    _renderTimingSummary(data.timing);
    _renderBanner(data);
    _renderIndexCards(data.index_reports);
    _renderIndustry(data);
    _renderRotation(data.rotation);
    _renderConcepts(data.concepts);
    _renderSignals(data.scored_symbols);
    _renderReplayTimeline(data.replay_timelines, data.scored_symbols);
  } catch (e) {
    console.error('加载结果失败', e);
  }
}

/* ── 耗时摘要 ── */
function _renderTimingSummary(timing) {
  const el = document.getElementById('review-timing');
  if (!el) return;
  if (!timing || !timing.total) { el.innerHTML = ''; return; }
  const parts = [];
  if (timing.L1) parts.push(`L1 指数: ${timing.L1}s`);
  if (timing.L2) parts.push(`L2 行业: ${timing.L2}s`);
  if (timing.L3) parts.push(`L3 个股: ${timing.L3}s`);
  el.innerHTML = `<div class="review-timing-bar">` +
    `总耗时 ${timing.total}s — ${parts.join(' | ')}` +
    `</div>`;
}

/* ── A: 大盘方向横幅 ── */
function _renderBanner(data) {
  const el = document.getElementById('review-banner');
  const b = data.banner;
  if (!b) { el.innerHTML = ''; return; }

  const dirClass = b.overall_direction === '偏多' ? 'bull' :
    b.overall_direction === '偏空' ? 'bear' : 'neutral';

  el.innerHTML = `
    <div class="review-banner-inner ${dirClass}">
      <span class="review-banner-dir">${b.overall_direction}</span>
      <span class="review-banner-sep">·</span>
      <span>${b.sentiment_phase || ''}</span>
      <span class="review-banner-sep">·</span>
      <span>建议仓位 ${b.position_suggestion || '—'}</span>
      <span class="review-banner-sep">·</span>
      <span>风格: ${b.recommended_style || '—'}</span>
    </div>
  `;
  // 日期标签
  const label = data.start_label ? `（${data.start_label}）` : '';
  const info = document.getElementById('review-info');
  if (info) info.textContent = `复盘结果: ${data.start_date}${label}`;
}

/* ── B: 指数结构卡片 ── */
function _renderIndexCards(reports) {
  const containers = {
    a: document.getElementById('review-cards-a'),
    hk: document.getElementById('review-cards-hk'),
    us: document.getElementById('review-cards-us'),
  };
  containers.a.innerHTML = '';
  containers.hk.innerHTML = '';
  containers.us.innerHTML = '';

  const usNames = ['标普500', '纳斯达克', '道琼斯'];
  const hkNames = ['恒生科技'];

  reports.forEach(r => {
    if (!r.data_available) return;
    const card = _buildIndexCard(r);
    if (usNames.includes(r.name)) {
      containers.us.appendChild(card);
    } else if (hkNames.includes(r.name)) {
      containers.hk.appendChild(card);
    } else {
      containers.a.appendChild(card);
    }
  });
}

function _buildIndexCard(r) {
  const card = document.createElement('div');
  card.className = 'index-card';
  card.onclick = () => navigateToChart(r.name, 'daily');

  const dirClass = r.is_bullish ? 'bull' : r.has_sell_signal ? 'bear' : '';
  const aligned = r.three_level_aligned ? '<span class="aligned-badge">三线共振</span>' : '';

  // 信号徽章
  function sigBadge(sig) {
    if (!sig || sig === '无') return '<span class="sig-none">—</span>';
    const cls = sig.includes('买') ? 'sig-buy' : sig.includes('卖') ? 'sig-sell' : 'sig-none';
    return `<span class="${cls}">${sig}</span>`;
  }

  card.innerHTML = `
    <div class="card-name ${dirClass}">${r.name} ${aligned}</div>
    <div class="card-price">${r.snapshot_price ? r.snapshot_price.toFixed(2) : r.latest_price ? r.latest_price.toFixed(2) : ''}</div>
    <div class="card-trend">${r.daily_trend || ''}</div>
    <div class="card-signals">
      <div>日 ${sigBadge(r.daily_latest_signal)}</div>
      <div>30M ${sigBadge(r.f30_latest_signal)}</div>
      <div>15M ${sigBadge(r.f15_latest_signal)}</div>
    </div>
  `;
  return card;
}

/* ── C: 行业双榜 ── */
function _renderIndustry(data) {
  const el = document.getElementById('review-industry');

  // 涨停密度排行
  let gainHtml = '<h4>涨停密度排行</h4>';
  if (data.gain_list.length) {
    gainHtml += '<table class="review-ind-table"><thead><tr>' +
      '<th>排名</th><th>行业</th><th>涨停</th><th>代表股</th></tr></thead><tbody>';
    data.gain_list.forEach((ind, i) => {
      const reps = ind.candidates.map(c => c.name).join('、');
      gainHtml += `<tr>
        <td>${i + 1}</td>
        <td>${ind.display_name}</td>
        <td>${ind.zt_count}只</td>
        <td class="review-reps">${reps}</td>
      </tr>`;
    });
    gainHtml += '</tbody></table>';
  } else {
    gainHtml += '<p class="review-empty">暂无涨停数据</p>';
  }

  // 综合强度排行
  let compHtml = '<h4>综合强度排行</h4>';
  if (data.composite_list.length) {
    compHtml += '<table class="review-ind-table"><thead><tr>' +
      '<th>排名</th><th>行业</th><th>综合分</th><th>涨停</th><th>强势</th><th>代表股</th></tr></thead><tbody>';
    data.composite_list.forEach(ind => {
      const reps = ind.candidates.map(c => c.name).join('、');
      const tag = ind.source === 'both' ? ' ★' : '';
      compHtml += `<tr>
        <td>${ind.composite_rank}${tag}</td>
        <td>${ind.display_name}</td>
        <td>${ind.composite_score}</td>
        <td>${ind.zt_count}</td>
        <td>${ind.strong_count}</td>
        <td class="review-reps">${reps}</td>
      </tr>`;
    });
    compHtml += '</tbody></table>';
  } else {
    compHtml += '<p class="review-empty">暂无行业数据</p>';
  }

  el.innerHTML = `<div class="review-industry-dual">
    <div class="review-ind-col">${gainHtml}</div>
    <div class="review-ind-col">${compHtml}</div>
  </div>`;
}

/* ── D: 轮动 & 配置建议 ── */
function _renderRotation(rot) {
  const el = document.getElementById('review-rotation');
  if (!rot || !rot.stage) {
    el.innerHTML = '';
    return;
  }
  el.innerHTML = `<div class="review-rotation-card">
    <div class="review-rotation-stage">${rot.detail || rot.stage}</div>
    ${rot.allocation ? `<div class="review-rotation-alloc">${rot.allocation}</div>` : ''}
  </div>`;
}

/* ── E: 概念热度 ── */
function _renderConcepts(concepts) {
  const el = document.getElementById('review-concepts');
  if (!concepts || !concepts.length) {
    el.innerHTML = '';
    return;
  }

  const typeIcon = { '防守': '🛡', '进攻': '⚔', '周期': '🔄', '中性': '' };
  let html = '<h4>概念热度排行</h4><table class="review-ind-table"><thead><tr>' +
    '<th>排名</th><th>概念</th><th>涨幅</th><th>属性</th></tr></thead><tbody>';
  concepts.forEach((cp, i) => {
    const sign = cp.gain_pct >= 0 ? '+' : '';
    const cls = cp.gain_pct >= 0 ? 'bull' : 'bear';
    html += `<tr>
      <td>${i + 1}</td>
      <td>${cp.name}</td>
      <td class="${cls}">${sign}${cp.gain_pct.toFixed(2)}%</td>
      <td>${cp.sector_type} ${typeIcon[cp.sector_type] || ''}</td>
    </tr>`;
  });
  html += '</tbody></table>';
  el.innerHTML = html;
}

/* ── F: 个股信号列表 ── */
function _renderSignals(scored) {
  const el = document.getElementById('review-signals');
  if (!scored || !scored.length) {
    el.innerHTML = '<p class="review-empty">暂无个股信号</p>';
    return;
  }

  // 按融合分(优先)或缠论原始分降序
  scored.sort((a, b) => (b.fused_total || b.total_score) - (a.fused_total || a.total_score));

  let html = '<h4>个股信号列表</h4>' +
    '<table class="review-signal-table"><thead><tr>' +
    '<th>代码</th><th>名称</th><th>评分</th><th>方向</th><th>信号</th><th>共振</th><th>异常</th><th>置信</th>' +
    '</tr></thead><tbody>';

  scored.forEach(s => {
    const dirCls = s.direction === '偏多' ? 'bull' : s.direction === '偏空' ? 'bear' : '';
    const displayScore = s.fused_total || s.total_score;
    const sign = displayScore >= 0 ? '+' : '';
    // 构建信号简报
    const sigMap = {};
    (s.signals || []).forEach(sig => {
      const abbr = _freqAbbr(sig.freq);
      if (!sigMap[sig.type]) sigMap[sig.type] = [];
      sigMap[sig.type].push(abbr);
    });
    const sigBrief = Object.entries(sigMap)
      .map(([t, fs]) => `${t}(${fs.join('+')})`)
      .slice(0, 4).join(' ');

    // 共振判断
    const buyFreqs = new Set(
      (s.signals || []).filter(sig => sig.type.includes('买')).map(sig => sig.freq));
    const resonance = buyFreqs.size > 1 ? '★' : '';

    // 异常信号数
    const anomalyBadge = s.anomaly && s.anomaly.anomaly_count > 0
      ? `<span class="badge anomaly-badge">${s.anomaly.anomaly_count}项</span>` : '—';

    // 置信度
    const conf = s.confidence_level || '';
    const confCls = conf === 'A' ? 'conf-a' : conf === 'B' ? 'conf-b' : '';

    html += `<tr class="${dirCls}" onclick="window._reviewGotoStock && window._reviewGotoStock('${s.symbol}')">
      <td>${s.symbol}</td>
      <td>${s.name}</td>
      <td>${sign}${displayScore}</td>
      <td>${s.direction}</td>
      <td>${sigBrief}</td>
      <td>${resonance}</td>
      <td>${anomalyBadge}</td>
      <td><span class="${confCls}">${conf}</span></td>
    </tr>`;
  });

  html += '</tbody></table>';
  el.innerHTML = html;
}

function _freqAbbr(freq) {
  const map = { '15分钟': '15M', '30分钟': '30M', '60分钟': '60M', '日线': '日', '周线': '周' };
  return map[freq] || freq;
}

/* ── G: 信号回放时间线 ── */
function _renderReplayTimeline(timelines, scored) {
  const el = document.getElementById('review-replay-timeline');
  if (!el) return;

  if (!timelines || !Object.keys(timelines).length) {
    el.innerHTML = '';
    return;
  }

  // 构建 symbol → name 映射
  const nameMap = {};
  (scored || []).forEach(s => { nameMap[s.symbol] = s.name || s.symbol; });

  const symbols = Object.keys(timelines);

  let html = '<div class="replay-timeline-section">' +
    '<button class="replay-timeline-toggle" onclick="_toggleReplayTimeline(this)">' +
    '<span class="arrow">&#9660;</span> 信号回放时间线 (' + symbols.length + ' 标的)</button>' +
    '<div class="replay-timeline-body">';

  // 标的 tabs
  html += '<div class="replay-stock-tabs">';
  symbols.forEach((sym, i) => {
    const name = nameMap[sym] || sym;
    const cls = i === 0 ? 'replay-stock-tab active' : 'replay-stock-tab';
    html += `<button class="${cls}" data-symbol="${sym}" onclick="_switchReplayTab(this, '${sym}')">${name}</button>`;
  });
  html += '</div>';

  // 每个标的的事件列表
  symbols.forEach((sym, i) => {
    const events = timelines[sym];
    const display = i === 0 ? '' : 'style="display:none"';
    html += `<div class="replay-events" id="replay-events-${sym.replace('.', '_')}" ${display}>`;
    if (!events.length) {
      html += '<p class="replay-empty">该标的回放期间无信号变化</p>';
    } else {
      events.forEach(ev => {
        const actionCls = ev.action === 'appear' ? 'replay-event-appear' : 'replay-event-disappear';
        const actionText = ev.action === 'appear' ? '出现' : '消失';
        const confText = ev.action === 'appear' && ev.confidence > 0
          ? `(conf=${ev.confidence.toFixed(2)})` : '';
        html += `<div class="replay-event">
          <span class="replay-event-dt">${ev.dt_str}</span>
          <span class="replay-event-type ${actionCls}">${ev.signal_type} ${actionText}</span>
          <span class="replay-event-price">@ ${ev.price.toFixed(2)}</span>
          <span class="replay-event-conf">${confText}</span>
        </div>`;
      });
    }
    html += '</div>';
  });

  html += '</div></div>';
  el.innerHTML = html;
}

window._toggleReplayTimeline = function (btn) {
  btn.classList.toggle('open');
  const body = btn.nextElementSibling;
  body.classList.toggle('open');
};

window._switchReplayTab = function (tab, symbol) {
  // 切换 tab 高亮
  tab.parentElement.querySelectorAll('.replay-stock-tab').forEach(t => t.classList.remove('active'));
  tab.classList.add('active');
  // 切换事件列表
  const body = tab.closest('.replay-timeline-body');
  body.querySelectorAll('.replay-events').forEach(ev => ev.style.display = 'none');
  const target = document.getElementById('replay-events-' + symbol.replace('.', '_'));
  if (target) target.style.display = '';
};

/* ── 跳转到个股分析 ── */
window._reviewGotoStock = function (symbol) {
  if (window.loadStockAnalysis) {
    switchPage('stock');
    window.loadStockAnalysis(symbol);
  }
};
