/**
 * 隆小侠 LONG CLAW — 个股分析页
 * 基于 StockDeepDive: 评分+多级别结构+量价+支撑阻力+完全分类+风控
 */

// ── 搜索分析 ────────────────────────────────────────
async function analyzeStock() {
  const input = document.getElementById('stock-input');
  const raw = (input.value || '').trim();
  if (!raw) {
    showToast('请输入股票代码或主题关键词');
    return;
  }

  const container = document.getElementById('stock-result');
  const startTime = Date.now();
  let timer = null;

  const updateTimer = (label) => {
    const elapsed = ((Date.now() - startTime) / 1000).toFixed(0);
    container.innerHTML = `<div class="empty-state"><div class="loading-spinner" style="width:32px;height:32px;border-width:2px;margin:0 auto 12px;"></div>${label || '分析中'}... ${elapsed}s</div>`;
  };

  // 判断: 代码 vs 主题关键词
  const isCode = /^\d{6}$/.test(raw) || raw.includes('.');
  if (isCode) {
    updateTimer('个股分析');
    timer = setInterval(() => updateTimer('个股分析'), 1000);
    try {
      const data = await apiFetch(`/api/stock/analyze/${encodeURIComponent(raw)}`);
      clearInterval(timer);
      renderStockResult(data);
    } catch (e) {
      clearInterval(timer);
      container.innerHTML = `<div class="empty-state">分析失败: ${e.message}</div>`;
    }
  } else {
    // 主题发现模式
    updateTimer('主题发现');
    timer = setInterval(() => updateTimer('主题发现'), 1000);
    try {
      const data = await apiFetch(`/api/social/discover/${encodeURIComponent(raw)}`);
      clearInterval(timer);
      renderThemeDiscovery(data);
    } catch (e) {
      clearInterval(timer);
      container.innerHTML = `<div class="empty-state">主题发现失败: ${e.message}</div>`;
    }
  }
}

// ── 渲染分析结果 ────────────────────────────────────
function renderStockResult(data) {
  const container = document.getElementById('stock-result');
  let html = '';

  // 错误提示（过滤掉已有数据的级别错误）
  if (data.errors && data.errors.length > 0) {
    const tfKeys = Object.keys(data.tf_analyses || {});
    const filtered = data.errors.filter(e => {
      // 如果某级别有数据，跳过该级别的错误
      for (const k of tfKeys) {
        if (e.includes(k) && data.tf_analyses[k]) return false;
      }
      return true;
    });
    if (filtered.length > 0) {
      html += '<div class="stock-errors">';
      filtered.forEach(e => { html += `<div class="stock-error-item">${e}</div>`; });
      html += '</div>';
    }
  }

  // 0. 公司标头
  const displayName = data.name || data.symbol;
  const codeShort = data.symbol.includes('.') ? data.symbol.split('.')[1] : data.symbol;
  html += `<div class="stock-header-bar"><span class="stock-code">${codeShort}</span> <span class="stock-name">${displayName}</span></div>`;

  // 1. 评分卡片（双分数：缠论 + 融合）
  if (data.scored) {
    const s = data.scored;
    const dirCls = s.direction === '偏多' ? 'up' : s.direction === '偏空' ? 'down' : 'flat';
    const hasFused = s.fused_total != null && s.fused_total > 0;
    const mainScore = hasFused ? s.fused_total : s.total_score;
    const mainLabel = hasFused ? '融合评分' : '综合评分';

    // 置信度标签
    let confBadge = '';
    if (s.confidence_level) {
      const confCls = s.confidence_level === '高' ? 'conf-high' :
                      s.confidence_level === '中' ? 'conf-mid' : 'conf-low';
      confBadge = `<span class="confidence-badge ${confCls}">${s.confidence_level}置信</span>`;
    }

    html += `<div class="stock-score-card">
      <div class="stock-score-main">
        <span class="stock-score-number">${mainScore.toFixed(1)}</span>
        <span class="stock-score-label">${mainLabel}</span>
      </div>
      ${hasFused ? `<div class="stock-score-sub">
        <span class="stock-score-sub-number">${s.total_score.toFixed(1)}</span>
        <span class="stock-score-label">缠论</span>
      </div>` : ''}
      <div class="stock-score-meta">
        <div class="${dirCls}">方向: ${s.direction}</div>
        <div>信号数: ${s.signal_count}</div>
        ${s.ma_confirmation ? `<div>MA确认: ${s.ma_confirmation}</div>` : ''}
        ${confBadge}
      </div>
      ${data.social ? (() => {
        const soc = data.social;
        const hCls = { '\u7206\u70ED': 'heat-fire', '\u70ED\u95E8': 'heat-hot', '\u6E29\u548C': 'heat-warm' }[soc.heat_grade] || '';
        let socHtml = `<div class="stock-score-social">`;
        if (hCls) socHtml += `<span class="social-heat-badge badge ${hCls}">${soc.heat_grade} ${soc.heat_score.toFixed(0)}</span>`;
        socHtml += `</div>`;
        // 社交meta
        const parts = [];
        if (soc.comment_rank) parts.push('千评#' + soc.comment_rank);
        if (soc.comment_score) parts.push('综合' + soc.comment_score.toFixed(0));
        if (soc.focus_index) parts.push('关注' + soc.focus_index.toFixed(0));
        if (parts.length > 0) socHtml += `<div class="stock-social-meta">${parts.join(' | ')}</div>`;
        // 关联概念
        if (soc.concepts && soc.concepts.length > 0) {
          socHtml += `<div class="stock-keywords">${soc.concepts.slice(0, 5).map(c => '<span class="tag tag-theme">' + c + '</span>').join('')}</div>`;
        }
        return socHtml;
      })() : ''}
    </div>`;
  }

  // 1.5 异常检测雷达
  if (data.anomaly) {
    html += renderAnomalyRadar(data.anomaly, data.fused);
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
        const lvVal = lv.value != null ? lv.value.toFixed(2) : '—';
        const distPct = lv.distance_pct != null ? `${lv.distance_pct > 0 ? '+' : ''}${lv.distance_pct.toFixed(1)}%` : '';
        html += `<span class="key-level ${cls}">${arrow}${lv.name} ${lvVal} (${distPct})</span>`;
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

// ── 异常检测雷达卡片 ──────────────────────────────────
function renderAnomalyRadar(anomaly, fused) {
  const dimNames = {volume: '量能', range: '振幅', gap: '跳空', body: '实体', vol_price_div: '背离'};
  let html = `<div class="stock-section anomaly-radar"><div class="stock-section-title">异常检测</div>`;

  // 各维度 z-score 行
  if (anomaly.items && anomaly.items.length > 0) {
    anomaly.items.forEach(item => {
      const label = dimNames[item.name] || item.name;
      const absZ = Math.abs(item.z_score);
      const pct = Math.min(absZ / 3.0 * 100, 100);
      const barCls = item.is_anomaly ? 'anomaly' : absZ > 1.5 ? 'warning' : 'normal';
      const tagCls = item.is_anomaly ? 'fired' : 'normal';
      const zStr = item.z_score !== 0 ? `z=${item.z_score.toFixed(1)}` : '—';

      html += `<div class="anomaly-row">
        <span class="anomaly-label">${label}</span>
        <div class="anomaly-bar"><div class="anomaly-bar-fill ${barCls}" style="width:${pct}%"></div></div>
        <span class="anomaly-zscore">${zStr}</span>
        <span class="anomaly-tag ${tagCls}">${item.label}</span>
      </div>`;
    });
  }

  // 收敛提示
  if (anomaly.convergence && anomaly.anomaly_count >= 2) {
    const firedNames = anomaly.items.filter(i => i.is_anomaly).map(i => dimNames[i.name] || i.name).join('+');
    html += `<div class="anomaly-convergence">${anomaly.anomaly_count}维收敛 — ${firedNames}同时异常，信号可信度提升</div>`;
  }

  // 割肉指标
  if (anomaly.capitulation_score >= 40) {
    const capPct = Math.min(anomaly.capitulation_score, 100);
    const capLabel = capPct >= 80 ? '极度恐慌出清' : capPct >= 60 ? '恐慌出清' : '偏弱';
    html += `<div class="capitulation-box">
      <div class="capitulation-header">
        <span class="capitulation-title">割肉指标</span>
        <span class="capitulation-score">${anomaly.capitulation_score.toFixed(0)}/100</span>
        <span class="capitulation-label">${capLabel}</span>
      </div>
      <div class="capitulation-bar"><div class="capitulation-bar-fill" style="width:${capPct}%"></div></div>
      ${anomaly.capitulation_detail ? `<div class="capitulation-detail">${anomaly.capitulation_detail}</div>` : ''}
    </div>`;
  }

  // 融合明细
  if (fused) {
    html += `<div class="fusion-detail">
      <span>缠论${fused.raw_czsc_score.toFixed(0)}</span>
      ${fused.anomaly_boost !== 0 ? `<span class="${fused.anomaly_boost > 0 ? 'up' : 'down'}"> ${fused.anomaly_boost > 0 ? '+' : ''}${fused.anomaly_boost.toFixed(0)}异常</span>` : ''}
      ${fused.convergence_bonus > 0 ? `<span class="up">+${fused.convergence_bonus.toFixed(0)}收敛</span>` : ''}
      ${fused.capitulation_bonus > 0 ? `<span class="up">+${fused.capitulation_bonus.toFixed(0)}割肉</span>` : ''}
      <span>= ${fused.fused_total.toFixed(1)}</span>
    </div>`;
  }

  html += '</div>';
  return html;
}

// ── 主题发现结果渲染 ────────────────────────────────
function renderThemeDiscovery(data) {
  const container = document.getElementById('stock-result');
  let html = '';

  html += `<div class="theme-discovery-header">主题: ${data.theme}</div>`;
  html += `<div class="theme-discovery-meta">匹配概念: ${data.matched_concepts.join(', ')} | 共 ${data.total_stocks} 只标的 | ${data.sentiment_summary || '—'}</div>`;

  if (data.stocks && data.stocks.length > 0) {
    data.stocks.forEach((s, i) => {
      const pctCls = s.change_pct >= 0 ? 'up' : 'down';
      const heatCls = { '\u7206\u70ED': 'heat-fire', '\u70ED\u95E8': 'heat-hot', '\u6E29\u548C': 'heat-warm' }[s.heat_grade] || '';
      const concepts = s.concepts.map(c => `<span class="tag tag-theme" style="font-size:10px;padding:1px 4px;">${c}</span>`).join('');

      html += `<div class="theme-stock-row" onclick="document.getElementById('stock-input').value='${s.symbol}';analyzeStock();">
        <span class="signal-rank">${i + 1}</span>
        <span class="theme-stock-name">${s.name}</span>
        <span class="theme-stock-code">${s.code}</span>
        <span class="theme-stock-score">${s.relevance_score.toFixed(0)}</span>
        ${heatCls ? `<span class="badge ${heatCls}">${s.heat_grade}</span>` : ''}
        <span class="${pctCls}">${s.change_pct > 0 ? '+' : ''}${s.change_pct.toFixed(2)}%</span>
        <span class="theme-stock-concepts">${concepts}</span>
      </div>`;
    });
  } else {
    html += '<div class="empty-state">未发现关联标的</div>';
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
