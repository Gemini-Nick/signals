/* ═══════════════════════════════════════════════════
   🐲 个股深度分析页面 — stock.js (web2)
   ═══════════════════════════════════════════════════ */

let _stockLoaded = false;

onPageLoad('stock', () => {
  if (!_stockLoaded) {
    _stockLoaded = true;
    _initStockEvents();
  }
});

function _initStockEvents() {
  document.getElementById('stock-run')?.addEventListener('click', _runStockAnalysis);
  document.getElementById('stock-code')?.addEventListener('keydown', e => {
    if (e.key === 'Enter') _runStockAnalysis();
  });
}

async function _runStockAnalysis() {
  const code = document.getElementById('stock-code').value.trim();
  if (!code) return;

  const btn = document.getElementById('stock-run');
  btn.disabled = true;
  btn.textContent = '分析中...';

  try {
    const data = await apiFetch(`/api/stock/analyze/${encodeURIComponent(code)}`);
    _renderStockResult(data);
  } catch (e) {
    showToast('分析失败: ' + e.message);
  } finally {
    btn.disabled = false;
    btn.textContent = '深度分析';
  }
}

function _renderStockResult(data) {
  const container = document.getElementById('stock-results');
  let html = '';

  // 标题
  html += `<div class="stock-header">
    <h3 class="display-text">${data.name || data.symbol}</h3>
    <span class="meta-text">${data.symbol}</span>
  </div>`;

  // 评分卡
  if (data.scored) {
    const s = data.scored;
    const dirCls = s.direction === '偏多' ? 'up' : s.direction === '偏空' ? 'down' : '';
    html += `<div class="stock-score-card">
      <div class="stock-score-main">
        <div class="stock-score-value ${dirCls}">${s.total_score}</div>
        <div class="stock-score-label">CZSC 评分</div>
      </div>
      ${s.fused_total ? `<div class="stock-score-main">
        <div class="stock-score-value ${dirCls}">${s.fused_total}</div>
        <div class="stock-score-label">融合评分</div>
      </div>` : ''}
      <div class="stock-score-meta">
        <div>方向: <span class="${dirCls}">${s.direction}</span></div>
        <div>信号数: ${s.signal_count}</div>
        ${s.ma_confirmation ? `<div>MA确认: ${s.ma_confirmation}</div>` : ''}
        ${s.confidence_level ? `<div>置信度: <span class="source-badge">${s.confidence_level}</span></div>` : ''}
      </div>
    </div>`;
  }

  // 异常检测
  if (data.anomaly) {
    const a = data.anomaly;
    html += `<div class="section-title">异常检测</div>
      <div class="stock-anomaly">
        <div>异常指标: ${a.anomaly_count}个 | 收敛度: ${a.convergence} | 投降分: ${a.capitulation_score}</div>
        ${a.summary ? `<div class="meta-text">${a.summary}</div>` : ''}
      </div>`;
    if (a.items?.length) {
      html += '<div class="stock-anomaly-items">';
      a.items.filter(it => it.is_anomaly).forEach(it => {
        html += `<span class="stock-anomaly-badge">${it.name}: ${it.label} (z=${it.z_score})</span>`;
      });
      html += '</div>';
    }
  }

  // 融合评分明细
  if (data.fused) {
    const f = data.fused;
    html += `<div class="section-title">融合评分明细</div>
      <div class="stock-fused">
        <div>CZSC基础: ${f.raw_czsc_score} | 异常加成: +${f.anomaly_boost} | 收敛奖励: +${f.convergence_bonus}</div>
        <div>投降奖励: +${f.capitulation_bonus} | 维度: ${f.dimension_count}D | 置信度: ${f.confidence_level}</div>
        ${f.detail ? `<div class="meta-text">${f.detail}</div>` : ''}
      </div>`;
  }

  // MA 均线
  if (data.ma_context?.trend_summary) {
    const ma = data.ma_context;
    const maCls = ma.trend_summary === '多头排列' ? 'up' : ma.trend_summary === '空头排列' ? 'down' : 'flat';
    html += `<div class="section-title">均线状态</div>
      <div class="trend-chip ${maCls}">${ma.trend_summary}</div>`;
    if (ma.key_levels?.length) {
      html += '<div class="stock-levels">';
      ma.key_levels.forEach(lv => {
        const cls = lv.position === '下方' ? 'support' : 'resistance';
        html += `<span class="key-level ${cls}">${lv.name}: ${lv.value} (${lv.distance_pct > 0 ? '+' : ''}${lv.distance_pct}%)</span>`;
      });
      html += '</div>';
    }
  }

  // 多级别分析
  if (data.tf_analyses) {
    html += '<div class="section-title">多级别缠论分析</div>';
    for (const [freq, tf] of Object.entries(data.tf_analyses)) {
      const tCls = tf.trend === '上涨趋势' ? 'up' : tf.trend === '下跌趋势' ? 'down' : 'flat';
      html += `<div class="stock-tf">
        <div class="stock-tf-header">
          <span class="stock-tf-freq">${tf.freq}</span>
          <span class="trend-chip ${tCls}">${tf.trend}</span>
          <span class="meta-text">${tf.bi_count}笔 | ${tf.signal_count}信号</span>
        </div>`;
      if (tf.signals?.length) {
        tf.signals.forEach(s => {
          const sCls = s.signal_type.includes('买') ? 'buy' : 'sell';
          html += `<div class="stock-tf-signal ${sCls}">
            ${s.signal_type} <span class="conf">conf ${(s.confidence).toFixed(0)}%</span>
          </div>`;
        });
      }
      html += '</div>';
    }
  }

  // 完全分类场景
  if (data.scenarios?.length) {
    html += '<div class="section-title">完全分类</div>';
    data.scenarios.forEach(sc => {
      html += `<div class="stock-scenario">
        <div class="stock-scenario-name">${sc.name}</div>
        <div>触发: ${sc.trigger} | 概率: ${sc.probability_hint}</div>
        <div>操作: <b>${sc.action}</b></div>
        ${sc.rationale ? `<div class="meta-text">${sc.rationale}</div>` : ''}
      </div>`;
    });
  }

  // 风控
  if (data.risk?.stop_loss) {
    const r = data.risk;
    html += `<div class="section-title">风控建议</div>
      <div class="stock-risk">
        止损: ${r.stop_loss} | 盈亏比: ${r.risk_reward || '—'} | 仓位: ${r.position_pct || '—'}%
        ${r.description ? `<div class="meta-text">${r.description}</div>` : ''}
      </div>`;
  }

  // 分层仓位
  if (data.layered_position?.base_pct) {
    const lp = data.layered_position;
    html += `<div class="section-title">分层仓位</div>
      <div class="stock-layered">
        底仓: ${lp.base_pct}% | 灵活仓: ${lp.flex_pct}%
        <div class="meta-text">${lp.rationale || ''}</div>
      </div>`;
  }

  if (data.errors?.length) {
    html += `<div class="meta-text" style="margin-top:12px;color:var(--color-up)">
      警告: ${data.errors.join('; ')}
    </div>`;
  }

  container.innerHTML = html;
}
