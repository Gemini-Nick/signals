/**
 * 隆小侠 LONG CLAW — 个股分析页
 * 基于 StockDeepDive: 评分+多级别结构+量价+支撑阻力+完全分类+风控
 */

// ── 搜索分析 ────────────────────────────────────────
async function analyzeStock() {
  const input = document.getElementById('stock-input');
  const symbol = (input.value || '').trim();
  if (!symbol) {
    showToast('请输入股票代码');
    return;
  }

  const container = document.getElementById('stock-result');
  container.innerHTML = '<div class="empty-state">分析中，请稍候...</div>';

  try {
    const data = await apiFetch(`/api/stock/analyze/${encodeURIComponent(symbol)}`);
    renderStockResult(data);
  } catch (e) {
    container.innerHTML = `<div class="empty-state">分析失败: ${e.message}</div>`;
  }
}

// ── 渲染分析结果 ────────────────────────────────────
function renderStockResult(data) {
  const container = document.getElementById('stock-result');
  let html = '';

  // 错误提示
  if (data.errors && data.errors.length > 0) {
    html += '<div class="stock-errors">';
    data.errors.forEach(e => { html += `<div class="stock-error-item">${e}</div>`; });
    html += '</div>';
  }

  // 1. 评分卡片
  if (data.scored) {
    const s = data.scored;
    const dirCls = s.direction === '偏多' ? 'up' : s.direction === '偏空' ? 'down' : 'flat';
    html += `<div class="stock-score-card">
      <div class="stock-score-main">
        <span class="stock-score-number">${s.total_score.toFixed(1)}</span>
        <span class="stock-score-label">综合评分</span>
      </div>
      <div class="stock-score-meta">
        <div class="${dirCls}">方向: ${s.direction}</div>
        <div>信号数: ${s.signal_count}</div>
        ${s.ma_confirmation ? `<div>MA确认: ${s.ma_confirmation}</div>` : ''}
      </div>
    </div>`;
  }

  // 2. MA 均线
  if (data.ma_context && data.ma_context.trend_summary) {
    const ma = data.ma_context;
    const maCls = ma.trend_summary === '多头排列' ? 'up' : ma.trend_summary === '空头排列' ? 'down' : 'flat';
    html += `<div class="stock-section"><div class="stock-section-title">均线趋势</div>`;
    html += `<div class="stock-ma-trend ${maCls}">${ma.trend_summary}</div>`;
    if (ma.key_levels && ma.key_levels.length > 0) {
      html += '<div class="stock-levels">';
      ma.key_levels.forEach(lv => {
        const cls = lv.position === '上方' ? 'resistance' : 'support';
        const arrow = lv.position === '上方' ? '\u25B2' : '\u25BC';
        html += `<span class="key-level ${cls}">${arrow}${lv.name} ${lv.value.toFixed(2)} (${lv.distance_pct > 0 ? '+' : ''}${lv.distance_pct.toFixed(1)}%)</span>`;
      });
      html += '</div>';
    }
    html += '</div>';
  }

  // 3. 多级别结构
  const tfOrder = ['日线', '30分钟', '15分钟'];
  const tfEntries = tfOrder.map(k => [k, data.tf_analyses[k]]).filter(([_, v]) => v);
  if (tfEntries.length > 0) {
    html += `<div class="stock-section"><div class="stock-section-title">多级别缠论结构</div>`;
    html += '<div class="stock-tf-grid">';
    tfEntries.forEach(([label, tf]) => {
      const trendCls = tf.trend === '上涨趋势' ? 'up' : tf.trend === '下跌趋势' ? 'down' : 'flat';
      html += `<div class="stock-tf-card">
        <div class="stock-tf-freq">${label}</div>
        <div class="stock-tf-trend ${trendCls}">${tf.trend}</div>
        <div class="stock-tf-detail">笔数: ${tf.bi_count} | 方向: ${tf.last_direction}</div>
        ${tf.zs_range ? `<div class="stock-tf-detail">中枢: ${tf.zs_range[0].toFixed(2)} ~ ${tf.zs_range[1].toFixed(2)}</div>` : ''}
        <div class="stock-tf-detail">信号: ${tf.signal_count}个</div>`;
      // 信号列表
      if (tf.signals && tf.signals.length > 0) {
        html += '<div class="stock-tf-signals">';
        tf.signals.forEach(sig => {
          const sb = sig.signal_type.includes('买') ? 'buy' : 'sell';
          html += `<span class="signal-type ${sb}">${sig.signal_type}</span>`;
        });
        html += '</div>';
      }
      html += '</div>';
    });
    html += '</div></div>';
  }

  // 4. 量价分析
  if (data.volume && data.volume.trend) {
    const v = data.volume;
    html += `<div class="stock-section"><div class="stock-section-title">量价分析</div>`;
    html += `<div class="stock-volume-grid">
      <div class="stock-vol-item"><span class="stock-vol-label">量比</span><span class="stock-vol-value">${v.ratio.toFixed(2)}</span></div>
      <div class="stock-vol-item"><span class="stock-vol-label">趋势</span><span class="stock-vol-value">${v.trend}</span></div>
      <div class="stock-vol-item"><span class="stock-vol-label">配合</span><span class="stock-vol-value">${v.price_vol_match}</span></div>
    </div>`;
    html += `<div class="stock-vol-detail">${v.detail}</div>`;
    html += '</div>';
  }

  // 5. 完全分类 3 情景
  if (data.scenarios && data.scenarios.length > 0) {
    html += `<div class="stock-section"><div class="stock-section-title">完全分类（3 情景）</div>`;
    html += '<div class="stock-scenarios">';
    data.scenarios.forEach(sc => {
      const probCls = sc.probability_hint === '偏高' ? 'prob-high' :
                      sc.probability_hint === '偏低' ? 'prob-low' : 'prob-mid';
      html += `<div class="stock-scenario-card">
        <div class="stock-scenario-name">${sc.name}</div>
        <div class="stock-scenario-prob ${probCls}">概率: ${sc.probability_hint}</div>
        <div class="stock-scenario-row"><span class="stock-scenario-label">触发:</span> ${sc.trigger}</div>
        <div class="stock-scenario-row"><span class="stock-scenario-label">操作:</span> ${sc.action}</div>
        ${sc.target_prices.length > 0 ? `<div class="stock-scenario-row"><span class="stock-scenario-label">目标:</span> ${sc.target_prices.map(p => p.toFixed(2)).join(' / ')}</div>` : ''}
        <div class="stock-scenario-rationale">${sc.rationale}</div>
      </div>`;
    });
    html += '</div></div>';
  }

  // 6. 支撑阻力（关键高低点）
  if (data.pivots && data.pivots.length > 0) {
    html += `<div class="stock-section"><div class="stock-section-title">历史关键位</div>`;
    html += '<div class="stock-levels">';
    data.pivots.forEach(p => {
      const cls = p.role === '支撑' ? 'support' : p.role === '阻力' ? 'resistance' : '';
      html += `<span class="key-level ${cls}">${p.role} ${p.price.toFixed(2)} (${p.dt})</span>`;
    });
    html += '</div></div>';
  }

  // 7. 风控仓位
  if ((data.risk && data.risk.stop_loss) || (data.layered_position && data.layered_position.base_pct)) {
    html += `<div class="stock-section"><div class="stock-section-title">风控 & 仓位</div>`;
    html += '<div class="stock-risk-grid">';
    if (data.risk && data.risk.stop_loss) {
      const r = data.risk;
      html += `<div class="stock-risk-item"><span class="stock-risk-label">止损位</span><span class="stock-risk-value">${r.stop_loss.toFixed(2)}</span></div>`;
      if (r.risk_reward) html += `<div class="stock-risk-item"><span class="stock-risk-label">风险回报</span><span class="stock-risk-value">${r.risk_reward.toFixed(2)}</span></div>`;
      if (r.position_pct) html += `<div class="stock-risk-item"><span class="stock-risk-label">建议仓位</span><span class="stock-risk-value">${r.position_pct.toFixed(0)}%</span></div>`;
    }
    html += '</div>';
    if (data.layered_position && data.layered_position.base_pct) {
      const lp = data.layered_position;
      html += `<div class="stock-layered">
        <div><b>底仓</b> ${lp.base_pct.toFixed(0)}%</div>
        <div><b>弹性仓</b> ${lp.flex_pct.toFixed(0)}%</div>
        ${lp.flex_buy_ref ? `<div>买入参考: ${lp.flex_buy_ref}</div>` : ''}
        ${lp.flex_sell_ref ? `<div>卖出参考: ${lp.flex_sell_ref}</div>` : ''}
        ${lp.rationale ? `<div class="stock-layered-reason">${lp.rationale}</div>` : ''}
      </div>`;
    }
    html += '</div>';
  }

  if (!html) {
    html = '<div class="empty-state">无分析数据</div>';
  }

  container.innerHTML = html;
}

// ── 回车触发 ────────────────────────────────────────
function initStockPage() {
  const input = document.getElementById('stock-input');
  if (input) {
    input.addEventListener('keydown', (e) => {
      if (e.key === 'Enter') analyzeStock();
    });
  }
}

window.analyzeStock = analyzeStock;
window.initStockPage = initStockPage;
