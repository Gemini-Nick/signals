/**
 * 隆小侠 LONG CLAW — 市况总览页
 * 建议条 + 指数卡片 + 行业双榜(含成分股) + 情绪仓位 + 操作建议 + 信号列表
 */

// A股指数名称（保持顺序）
const A_INDICES = ['上证50', '沪深300', '创业板指', '科创50', '超大盘', '中证500', '中证1000'];
const HK_INDICES = ['恒生科技'];
const US_INDICES = ['标普500', '纳斯达克', '道琼斯'];

// 趋势箭头映射
const TREND_MAP = {
  '上涨趋势': { arrow: '\u2191', cls: 'up', label: '偏多' },
  '下跌趋势': { arrow: '\u2193', cls: 'down', label: '偏空' },
  '中枢震荡': { arrow: '\u2192', cls: 'flat', label: '震荡' },
  '结构未成型': { arrow: '?', cls: 'flat', label: '未成型' },
  '数据不足': { arrow: '-', cls: 'flat', label: '无数据' },
  '未知': { arrow: '-', cls: 'flat', label: '未知' },
};

function trendInfo(trend) {
  return TREND_MAP[trend] || TREND_MAP['未知'];
}

// ── P3-7: 决策简报 ──────────────────────────────────
function renderDecisionBrief(brief) {
  const container = document.getElementById('decision-brief');
  if (!brief) { container.style.display = 'none'; return; }

  const dirCls = brief.direction === '偏多' ? 'bullish'
    : brief.direction === '偏空' ? 'bearish' : 'neutral';
  const tagCls = dirCls;

  let html = `<div class="brief-header">
    <span class="brief-title">决策简报</span>
    <span class="brief-date">${brief.date || ''}</span>
    <span class="brief-tag ${tagCls}">${brief.direction || ''}${brief.sentiment ? ' \u00B7 ' + brief.sentiment : ''}</span>
  </div><div class="brief-body">`;

  // 关键情景
  if (brief.key_scenarios && brief.key_scenarios.length > 0) {
    html += '<div class="brief-section"><div class="brief-label">关键判断</div>';
    brief.key_scenarios.forEach(s => {
      html += `<div class="scenario-line hold">IF 守住 ${s.level_price} (${s.level_name}) \u2192 ${s.hold}</div>`;
      html += `<div class="scenario-line break-line">IF 跌破 ${s.level_price} (${s.level_name}) \u2192 ${s.break_text || s['break'] || ''}</div>`;
    });
    html += '</div>';
  }

  // 风格切换 + 轮动 (并排)
  const ss = brief.style_switch;
  const rs = brief.rotation_status;
  if (ss || rs) {
    html += '<div class="brief-row">';
    if (ss && ss.detected) {
      html += `<div class="brief-section brief-style">\u{1F504} ${ss.direction} \u2014 ${ss.evidence}</div>`;
    }
    if (rs) {
      const warn = rs.peak_warning ? ' \u26A0\uFE0F' : '';
      html += `<div class="brief-section brief-rotation">\u{1F4C5} ${rs.stage || ''} ${rs.duration || 0}\u5929 (${rs.velocity || ''})${warn}</div>`;
    }
    html += '</div>';
  }

  // 节奏预警 + 历史匹配 (并排)
  if ((brief.rhythm_alerts && brief.rhythm_alerts.length > 0) || brief.analog_ref) {
    html += '<div class="brief-row">';
    if (brief.rhythm_alerts && brief.rhythm_alerts.length > 0) {
      const items = brief.rhythm_alerts.map(r => `${r.name}(${r.phase}${r.score})`).join(' ');
      html += `<div class="brief-section brief-rhythm">\u23F0 ${items}</div>`;
    }
    if (brief.analog_ref) {
      // 取第一个指数的第一个匹配
      const firstKey = Object.keys(brief.analog_ref)[0];
      if (firstKey && brief.analog_ref[firstKey] && brief.analog_ref[firstKey][0]) {
        const a = brief.analog_ref[firstKey][0];
        html += `<div class="brief-section brief-analog">\u{1F4CA} \u4E0E${a.match_start}~${a.match_end}\u76F8\u4F3C${(a.similarity * 100).toFixed(0)}%, \u540E30\u65E5${a.next_30d_return > 0 ? '+' : ''}${a.next_30d_return}%</div>`;
      }
    }
    html += '</div>';
  }

  // 操作建议
  if (brief.action_items && brief.action_items.length > 0) {
    html += '<div class="brief-section"><div class="brief-label">操作</div><ol class="brief-actions">';
    brief.action_items.forEach(a => { html += `<li>${a}</li>`; });
    html += '</ol></div>';
  }

  html += '</div>';
  container.innerHTML = html;
  container.style.display = 'block';
}

// ── 建议条 ──────────────────────────────────────────
function renderBanner(ctx) {
  const banner = document.getElementById('banner');
  const dirEl = document.getElementById('banner-direction');
  const sugEl = document.getElementById('banner-suggestion');

  banner.className = 'banner';
  if (ctx.overall_direction === '偏多') {
    banner.classList.add('bullish');
  } else if (ctx.overall_direction === '偏空') {
    banner.classList.add('bearish');
  } else {
    banner.classList.add('neutral');
  }

  const dirEmoji = { '偏多': '\u2191', '偏空': '\u2193', '分化': '\u2194' };
  let dirText = `大盘方向: ${ctx.overall_direction} ${dirEmoji[ctx.overall_direction] || ''}`;
  if (ctx.sentiment_phase && ctx.sentiment_phase !== '未知') {
    dirText += `  |  情绪: ${ctx.sentiment_phase}`;
  }
  if (ctx.divergence_score > 0) {
    dirText += `  |  分歧: ${ctx.divergence_score.toFixed(0)}`;
  }
  dirEl.textContent = dirText;

  let suggestion = ctx.position_suggestion || ctx.summary || '';
  if (ctx.recommended_style && ctx.recommended_style !== '未知') {
    suggestion += `  [推荐风格: ${ctx.recommended_style}]`;
  }
  sugEl.textContent = suggestion;

  // P3-5: 风格切换提示
  const ssEl = document.getElementById('style-switch-alert');
  if (ctx.style_switch && ctx.style_switch.detected) {
    const sw = ctx.style_switch;
    ssEl.innerHTML = `\u{1F504} 风格切换: ${sw.direction} \u2014 ${sw.evidence} <span class="switch-suggestion">\u2192 ${sw.suggestion}</span>`;
    ssEl.style.display = 'block';
  } else {
    ssEl.style.display = 'none';
  }
}

// ── 指数卡片 ────────────────────────────────────────
function renderCards(reports) {
  const reportMap = {};
  reports.forEach(r => { reportMap[r.name] = r; });

  const groups = [
    { names: A_INDICES, containerId: 'cards-a' },
    { names: HK_INDICES, containerId: 'cards-hk' },
    { names: US_INDICES, containerId: 'cards-us' },
  ];

  groups.forEach(({ names, containerId }) => {
    const container = document.getElementById(containerId);
    container.innerHTML = '';

    names.forEach(name => {
      const r = reportMap[name];
      if (!r) return;

      const card = document.createElement('div');
      card.className = 'index-card';
      card.onclick = () => navigateToChart(name);

      if (!r.data_available) {
        card.innerHTML = `
          <div class="card-name">${name}</div>
          <div class="card-trend flat">数据不可用</div>`;
        container.appendChild(card);
        return;
      }

      const trend = trendInfo(r.daily_trend);
      const priceStr = r.latest_price ? r.latest_price.toFixed(2) : '';

      const signals = [];
      if (r.daily_latest_signal !== '无') signals.push(r.daily_latest_signal);
      if (r.f30_latest_signal !== '无') signals.push(r.f30_latest_signal);
      if (r.f15_latest_signal !== '无') signals.push(r.f15_latest_signal);
      const uniqueSignals = [...new Set(signals)];

      let signalHtml = '';
      if (uniqueSignals.length > 0) {
        signalHtml = '<div class="card-signals">' + uniqueSignals.map(s =>
          `<span class="card-signal">${s}</span>`
        ).join(' ') + '</div>';
      }

      let maHtml = '';
      if (r.ma_context) {
        const maTrend = r.ma_context.trend_summary;
        const maCls = maTrend === '多头排列' ? 'up' : maTrend === '空头排列' ? 'down' : 'flat';
        maHtml += `<div class="card-ma-trend ${maCls}">MA ${maTrend}</div>`;

        if (r.ma_context.key_levels && r.ma_context.key_levels.length > 0) {
          maHtml += '<div class="card-key-levels">';
          r.ma_context.key_levels.forEach(lv => {
            const arrow = lv.position === '上方' ? '\u25B2' : '\u25BC';
            const cls = lv.position === '上方' ? 'resistance' : 'support';
            maHtml += `<span class="card-level ${cls}">${arrow}${lv.name} ${lv.value.toFixed(0)}</span>`;
          });
          maHtml += '</div>';
        }
      }

      // P3-2: 情景分叉
      let scenarioHtml = '';
      if (r.scenarios && r.scenarios.length > 0) {
        const urgent = r.scenarios.filter(s => s.urgency === '接近');
        if (urgent.length > 0) {
          const s = urgent[0];
          const expanded = urgent.length > 0 ? 'true' : 'false';
          scenarioHtml = `<div class="card-scenarios" data-expanded="${expanded}">
            <div class="scenario-toggle">\u{1F500} <span class="urgency-dot urgent"></span></div>
            <div class="scenario-body">
              <div class="scenario-branch support">IF \u5B88\u4F4F ${s.level_price.toFixed(0)} (${s.level_name}) \u2192 ${s.hold}</div>
              <div class="scenario-branch break-branch">IF \u8DCC\u7834 ${s.level_price.toFixed(0)} (${s.level_name}) \u2192 ${s['break']}</div>
            </div>
          </div>`;
        }
      }

      // P3-5: 近5日收益率
      let retHtml = '';
      if (r.recent_5d_return !== null && r.recent_5d_return !== undefined) {
        const retCls = r.recent_5d_return > 0 ? 'up' : r.recent_5d_return < 0 ? 'down' : 'flat';
        retHtml = `<span class="card-return ${retCls}">${r.recent_5d_return > 0 ? '+' : ''}${r.recent_5d_return.toFixed(1)}%</span>`;
      }

      const alignBadge = r.three_level_aligned
        ? '<span class="card-align-badge">三级共振</span>'
        : '';

      card.innerHTML = `
        <div class="card-header">
          <div class="card-name">${name} ${retHtml}</div>
          ${alignBadge}
        </div>
        <div class="card-trend ${trend.cls}">${trend.arrow} ${trend.label}</div>
        ${signalHtml}
        <div class="card-price">${priceStr}</div>
        ${maHtml}
        ${scenarioHtml}`;

      container.appendChild(card);
    });
  });
}

// ── 候选股预览（行内简短显示）──────────────────────
function _stockPreview(candidates) {
  if (!candidates || candidates.length === 0) return '';
  const tags = candidates.slice(0, 3).map(c => {
    const roleClass = c.role === '涨停' ? 'zt' : c.role === '龙头' ? 'leader' : 'strong';
    return `<span class="stock-tag ${roleClass}">${c.role}:${c.name}</span>`;
  });
  return `<div class="industry-stocks-preview">${tags.join('')}</div>`;
}

// ── 候选股展开面板 ──────────────────────────────────
function _stockPanel(candidates, panelId) {
  if (!candidates || candidates.length === 0) {
    return `<div class="industry-candidates" id="${panelId}">
      <div style="color:var(--text-muted);font-size:12px;">暂无成分股数据</div>
    </div>`;
  }
  let rows = '';
  candidates.forEach(c => {
    const roleCls = c.role === '涨停' ? 'role-zt'
      : c.role === '龙头' ? 'role-leader' : 'role-strong';
    rows += `<tr>
      <td>${c.name}</td>
      <td><span class="${roleCls}">${c.role}</span></td>
      <td>${c.code}</td>
      <td>${c.detail || ''}</td>
    </tr>`;
  });
  return `<div class="industry-candidates" id="${panelId}">
    <table><tr><th>名称</th><th>角色</th><th>代码</th><th>详情</th></tr>${rows}</table>
  </div>`;
}

// ── 行业全景排行（合并单表 + 统计摘要）──────────────
function renderIndustry(data) {
  const container = document.getElementById('industry-section');
  const { composite_list, concepts, oversold_list, stats } = data;

  if (!composite_list || composite_list.length === 0) {
    container.innerHTML = '<div class="empty-state">行业数据未加载</div>';
    return;
  }

  let html = '';

  // P3-3: 兑现提醒横幅
  const rhythmAlerts = composite_list.slice(0, 10).filter(
    ind => ind.rhythm_phase && (ind.rhythm_phase === '衰竭' || ind.rhythm_phase === '高潮')
  );
  if (rhythmAlerts.length > 0) {
    html += '<div class="rhythm-alert">\u23F0 兑现提醒: ';
    html += rhythmAlerts.map(ind =>
      `${ind.display_name || ind.name}(${ind.rhythm_phase}${ind.rhythm_score || ''})`
    ).join(' \u00B7 ');
    html += ' \u2014 注意板块节奏</div>';
  }

  // 行业全景表（合并综合+涨幅维度 + 节奏列）
  html += '<div class="industry-panorama"><div class="table-title">行业全景 TOP10</div><table>';
  html += '<tr><th>#</th><th>行业</th><th>综合</th><th>涨幅</th><th>涨停</th><th>净流入</th><th>节奏</th><th>属性</th><th>候选股</th></tr>';
  composite_list.slice(0, 10).forEach((ind, i) => {
    const panelId = `pano-panel-${i}`;
    const pctCls = ind.gain_pct >= 0 ? 'up' : 'down';
    const scoreCls = ind.composite_score >= 70 ? 'score-high' : '';
    const ztBold = ind.zt_count >= 3 ? 'zt-hot' : '';
    const inflowBold = Math.abs(ind.net_inflow) >= 1 ? 'inflow-hot' : '';

    // P3-3: 节奏列
    const rhythmPhaseMap = {
      '启动': 'rhythm-start', '加速': 'rhythm-accel', '高潮': 'rhythm-peak',
      '衰竭': 'rhythm-exhaust', '休整': 'rhythm-rest',
    };
    let rhythmCell = '';
    if (ind.rhythm_phase) {
      const rCls = rhythmPhaseMap[ind.rhythm_phase] || '';
      rhythmCell = `<span class="rhythm ${rCls}">${ind.rhythm_phase} ${ind.rhythm_score || ''}</span>`;
    }

    // 候选股简要
    let stockBrief = '';
    if (ind.candidates && ind.candidates.length > 0) {
      stockBrief = ind.candidates.slice(0, 2).map(c => `${c.role}:${c.name}`).join(' ');
    }

    html += `<tr class="industry-row ${scoreCls}" data-panel="${panelId}" data-industry="${ind.name}">
      <td>${i + 1}</td>
      <td class="industry-name-cell">${ind.display_name || ind.name}</td>
      <td class="score-cell">${ind.composite_score.toFixed(0)}</td>
      <td class="${pctCls}">${ind.gain_pct > 0 ? '+' : ''}${ind.gain_pct.toFixed(2)}%</td>
      <td class="${ztBold}">${ind.zt_count}</td>
      <td class="${inflowBold}">${ind.net_inflow > 0 ? '+' : ''}${ind.net_inflow.toFixed(1)}亿</td>
      <td>${rhythmCell}</td>
      <td><span class="tag tag-${ind.sector_type}">${ind.sector_type}</span>${ind.rotation_line ? ` <span class="tag tag-rotation">${ind.rotation_line}</span>` : ''}</td>
      <td class="stock-brief">${stockBrief}</td>
    </tr>`;
    html += `<tr><td colspan="9" style="padding:0;">${_stockPanel(ind.candidates, panelId)}</td></tr>`;
  });
  html += '</table>';

  // 统计摘要行
  if (stats) {
    html += '<div class="industry-stats-bar">';
    if (stats.zt_total) html += `<span>今日涨停 <b>${stats.zt_total}</b> 只</span>`;
    if (stats.dt_total) html += `<span>跌停 <b>${stats.dt_total}</b> 只</span>`;
    if (stats.lianban_max) html += `<span>连板最高 <b>${stats.lianban_max}</b> 板</span>`;
    if (stats.red_pct) html += `<span>红盘行业 <b>${stats.red_pct}%</b></span>`;
    html += '</div>';
  }
  html += '</div>';

  // 超跌行业
  if (oversold_list && oversold_list.length > 0) {
    html += '<div class="oversold-section"><span class="table-title">超跌反弹: </span>';
    html += oversold_list.slice(0, 5).map(ind =>
      `<span class="tag tag-oversold">${ind.display_name || ind.name} (${ind.oversold_detail})</span>`
    ).join(' ');
    html += '</div>';
  }

  // 热门概念
  if (concepts && concepts.length > 0) {
    html += '<div class="concept-section"><span class="table-title">热门概念: </span>';
    html += concepts.slice(0, 10).map(c => {
      const pctCls = c.gain_pct >= 0 ? 'up' : 'down';
      const hasData = c.gain_pct !== 0 || (c.tag !== 'static');
      if (hasData && c.gain_pct !== 0) {
        let label = `${c.name} ${c.gain_pct > 0 ? '+' : ''}${c.gain_pct.toFixed(1)}%`;
        if (c.leading_stock) label += ` 领涨:${c.leading_stock}`;
        return `<span class="tag tag-concept ${pctCls}">${label}</span>`;
      } else {
        return `<span class="tag tag-concept">${c.name}</span>`;
      }
    }).join(' ');
    html += '</div>';
  } else {
    html += '<div class="concept-section"><span class="table-title">热门概念: </span><span class="tag tag-concept">数据源暂不可用</span></div>';
  }

  container.innerHTML = html;

  // 行业行点击 → 展开/收起候选股
  container.querySelectorAll('.industry-row').forEach(row => {
    row.style.cursor = 'pointer';
    row.addEventListener('click', () => {
      const panelId = row.dataset.panel;
      if (panelId) {
        const panel = document.getElementById(panelId);
        if (panel) {
          panel.classList.toggle('open');
        }
      }
    });
  });
}

// ── 情绪仓位面板 (删除底部买卖信号栏) ────────────────
function renderSentimentPanel(ctx) {
  const container = document.getElementById('rotation-section');

  if (!ctx.rotation_stage && !ctx.allocation_suggestion && !ctx.sentiment_phase) {
    container.innerHTML = '<div class="empty-state">数据未加载</div>';
    return;
  }

  let html = '<div class="sentiment-panel">';

  // 卡片行: 情绪 + 轮动阶段 + 方向强度
  html += '<div class="sentiment-row">';
  if (ctx.sentiment_phase && ctx.sentiment_phase !== '未知') {
    const phaseColors = {
      '恐惧': 'down', '犹豫': 'flat', '乐观': 'up', '亢奋': 'up',
      '疯狂': 'up', '焦虑': 'down', '绝望': 'down',
    };
    const pCls = phaseColors[ctx.sentiment_phase] || 'flat';
    html += `<div class="sentiment-card">
      <div class="sentiment-label">市场情绪</div>
      <div class="sentiment-value ${pCls}">${ctx.sentiment_phase}</div>
    </div>`;
  }
  if (ctx.rotation_stage) {
    // P3-4: 轮动持续时间 + 速度
    const velMap = { '加速': 'accel', '减速': 'decel', '稳定': 'stable' };
    const velCls = velMap[ctx.rotation_velocity] || 'stable';
    const durText = ctx.rotation_duration ? ` ${ctx.rotation_duration}天` : '';
    const velText = ctx.rotation_velocity && ctx.rotation_velocity !== '稳定'
      ? ` ${ctx.rotation_velocity}` : '';
    html += `<div class="sentiment-card">
      <div class="sentiment-label">轮动阶段</div>
      <div class="sentiment-value">${ctx.rotation_stage}</div>
      ${durText ? `<div class="rotation-meta"><span class="rotation-duration">${durText}</span><span class="rotation-velocity ${velCls}">${velText}</span></div>` : ''}
    </div>`;
  }
  if (ctx.direction_strength !== undefined) {
    const strPct = (ctx.direction_strength * 100).toFixed(0);
    html += `<div class="sentiment-card">
      <div class="sentiment-label">方向强度</div>
      <div class="sentiment-value">${strPct}%</div>
    </div>`;
  }
  html += '</div>';

  // 仓位建议
  if (ctx.allocation_suggestion) {
    html += `<div class="allocation-bar">
      <span class="allocation-label">配置建议:</span>
      <span class="allocation-value">${ctx.allocation_suggestion}</span>
    </div>`;
  }

  // P3-4: 轮动峰值警告
  if (ctx.rotation_peak_warning && ctx.rotation_peak_detail) {
    html += `<div class="rotation-warning">\u26A0\uFE0F ${ctx.rotation_peak_detail}</div>`;
  }

  // 攻防板块
  if ((ctx.shield_sectors && ctx.shield_sectors.length > 0) ||
      (ctx.sword_sectors && ctx.sword_sectors.length > 0)) {
    html += '<div class="sector-bar">';
    if (ctx.sword_sectors && ctx.sword_sectors.length > 0) {
      html += '<span class="sector-group"><span class="sector-label">进攻:</span> ';
      html += ctx.sword_sectors.map(s => `<span class="tag tag-sword">${s}</span>`).join(' ');
      html += '</span>';
    }
    if (ctx.shield_sectors && ctx.shield_sectors.length > 0) {
      html += '<span class="sector-group"><span class="sector-label">防守:</span> ';
      html += ctx.shield_sectors.map(s => `<span class="tag tag-shield">${s}</span>`).join(' ');
      html += '</span>';
    }
    html += '</div>';
  }

  // 底部买入/卖出信号栏已删除（无意义）

  html += '</div>';
  container.innerHTML = html;
}

// ── 操作建议渲染 (道长策略 — 始终有内容) ────────────
function renderActionSummary(summary) {
  const container = document.getElementById('action-section');

  if (!summary) {
    container.innerHTML = '<div class="empty-state">操作建议数据未加载</div>';
    return;
  }

  let html = '';

  // 1. 恐慌评估（始终显示）
  const panic = summary.panic;
  if (panic) {
    const score = panic.score || 0;
    let cls, emoji;
    if (score >= 60) { cls = 'high'; emoji = '\uD83D\uDD34'; }
    else if (score >= 40) { cls = 'mid'; emoji = '\uD83D\uDFE1'; }
    else if (score >= 20) { cls = 'low'; emoji = '\uD83D\uDCCA'; }
    else { cls = 'safe'; emoji = '\u2705'; }
    html += `<div class="panic-banner ${cls}">
      <div>
        <div>${emoji} 恐慌指数 <span class="panic-score">${score.toFixed(0)}</span>/100 — ${panic.level}</div>
        ${panic.detail ? `<div class="panic-detail">${panic.detail}</div>` : ''}
        ${panic.action_hint ? `<div class="panic-hint">${panic.action_hint}</div>` : ''}
      </div>
    </div>`;

    // 抄底候选
    if (summary.bottom_candidates && summary.bottom_candidates.length > 0) {
      html += '<div class="action-subsection"><div class="action-subsection-title">抄底候选板块</div>';
      html += '<div style="display:flex;gap:6px;flex-wrap:wrap;">';
      summary.bottom_candidates.forEach(b => {
        const urgLabel = b.urgency ? ` [${b.urgency}]` : '';
        html += `<span class="tag tag-oversold">${b.name} ${b.gain_pct > 0 ? '+' : ''}${b.gain_pct.toFixed(1)}% 超跌${b.oversold_score.toFixed(0)}${urgLabel}</span>`;
      });
      html += '</div></div>';
    }
  }

  // 2. L1 指数策略（不依赖 L3，永远有内容）
  if (summary.l1_guidance && summary.l1_guidance.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">指数策略指引</div>';
    html += '<table class="action-table"><tr><th>指数</th><th>趋势</th><th>信号</th><th>操作建议</th></tr>';
    summary.l1_guidance.forEach(g => {
      const tInfo = trendInfo(g.trend);
      const actionCls = g.action === '可关注' ? 'action-buy' : g.action === '需回避' ? 'action-sell' : 'action-wait';
      html += `<tr>
        <td>${g.name}${g.aligned ? ' <span class="card-align-badge">共振</span>' : ''}</td>
        <td class="${tInfo.cls}">${tInfo.arrow} ${tInfo.label}</td>
        <td>${g.signals.join(' ')}</td>
        <td class="${actionCls}">${g.action}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // 3. L2 行业策略（不依赖 L3）
  if (summary.l2_actions && summary.l2_actions.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">行业策略指引</div>';
    html += '<table class="action-table"><tr><th>行业</th><th>综合分</th><th>涨停</th><th>建议</th><th>头部个股</th></tr>';
    summary.l2_actions.forEach(a => {
      const verdictCls = a.verdict === '关注' ? 'action-buy' : a.verdict === '回避' ? 'action-sell' : 'action-wait';
      html += `<tr>
        <td>${a.name}</td>
        <td class="score-cell">${a.score.toFixed(0)}</td>
        <td>${a.zt}</td>
        <td class="${verdictCls}">${a.verdict}</td>
        <td>${a.top_stock || '—'}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // 4. 买入机会（L3）
  if (summary.buy_opportunities && summary.buy_opportunities.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">买入机会 (' + summary.buy_opportunities.length + '只)</div>';
    html += '<table class="action-table"><tr><th>代码</th><th>名称</th><th>分数</th><th>信号</th><th>共振</th></tr>';
    summary.buy_opportunities.forEach(b => {
      html += `<tr>
        <td style="font-family:var(--font-mono);">${b.symbol}</td>
        <td>${b.name}</td>
        <td class="score-cell">${b.score.toFixed(0)}</td>
        <td>${b.signals_brief}</td>
        <td class="resonance">${b.resonance_tag}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // 5. 风险警示
  if (summary.risk_alerts && summary.risk_alerts.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">风险警示 (' + summary.risk_alerts.length + '只)</div>';
    html += '<table class="action-table"><tr><th>代码</th><th>名称</th><th>分数</th><th>方向</th></tr>';
    summary.risk_alerts.forEach(r => {
      html += `<tr>
        <td style="font-family:var(--font-mono);">${r.symbol}</td>
        <td>${r.name}</td>
        <td class="score-cell">${r.score.toFixed(0)}</td>
        <td style="color:var(--color-down);">${r.direction}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // 6. 重点关注
  if (summary.focus_list && summary.focus_list.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">重点关注</div>';
    summary.focus_list.forEach(f => {
      html += `<div class="action-text" style="margin-bottom:4px;">${f.symbol} ${f.name} 分=${f.score.toFixed(0)} [${f.tags.join(' | ')}]</div>`;
    });
    html += '</div>';
  }

  // 7. 行业研判
  const verdict = summary.industry_verdict;
  if (verdict) {
    html += '<div class="action-subsection"><div class="action-subsection-title">行业研判</div>';
    if (verdict.strong && verdict.strong.length > 0) {
      html += `<div class="action-text"><span class="verdict-strong">强势:</span> ${verdict.strong.join(' | ')}</div>`;
    }
    if (verdict.weak && verdict.weak.length > 0) {
      html += `<div class="action-text"><span class="verdict-weak">弱势:</span> ${verdict.weak.join(' | ')}</div>`;
    }
    if (verdict.note) {
      html += `<div class="verdict-note">${verdict.note}</div>`;
    }
    html += '</div>';
  }

  // 8. 概念归纳 + 主题追踪
  if (summary.concept_digest) {
    html += `<div class="action-subsection"><div class="action-subsection-title">概念归纳</div>
      <div class="action-text">${summary.concept_digest}</div></div>`;
  }
  if (summary.theme_summary) {
    html += `<div class="action-subsection"><div class="action-subsection-title">主题追踪</div>
      <div class="action-text">${summary.theme_summary}</div></div>`;
  }

  // 9. 结论
  if (summary.conclusion) {
    html += `<div class="action-conclusion">${summary.conclusion}</div>`;
  }

  if (!html) {
    html = '<div class="empty-state">暂无操作建议</div>';
  }

  container.innerHTML = html;
}

// ── 信号列表 (三段式: 指数→行业→个股) ─────────────
function renderSignalList(reports, scoredData, industryData) {
  const container = document.getElementById('signal-list');
  container.innerHTML = '';

  // Part 1: L1 指数信号
  const withSignals = reports
    .filter(r => r.data_available && (r.has_buy_signal || r.has_sell_signal))
    .sort((a, b) => {
      if (a.three_level_aligned && !b.three_level_aligned) return -1;
      if (!a.three_level_aligned && b.three_level_aligned) return 1;
      if (a.has_buy_signal && !b.has_buy_signal) return -1;
      if (!a.has_buy_signal && b.has_buy_signal) return 1;
      return 0;
    });

  if (withSignals.length > 0) {
    const subtitle = document.createElement('div');
    subtitle.className = 'signal-section-title';
    subtitle.textContent = `指数信号 (${withSignals.length})`;
    container.appendChild(subtitle);

    withSignals.forEach((r, idx) => {
      const sigs = [];
      [r.daily_latest_signal, r.f30_latest_signal, r.f15_latest_signal].forEach(s => {
        if (s !== '无') sigs.push(s);
      });
      const direction = r.is_bullish ? '偏多' : '偏空';
      const dirCls = r.is_bullish ? 'bullish' : 'bearish';

      const row = document.createElement('div');
      row.className = 'signal-row';
      row.onclick = () => navigateToChart(r.name);

      let sigBadges = sigs.map(s => {
        const sb = s.includes('买') ? 'buy' : 'sell';
        return `<span class="signal-type ${sb}">${s}</span>`;
      }).join('');

      row.innerHTML = `
        <span class="signal-rank">${idx + 1}</span>
        <div class="signal-symbol">
          <div class="signal-symbol-name">${r.name}</div>
          <div class="signal-symbol-code">${r.symbol}</div>
        </div>
        ${sigBadges}
        <span class="signal-direction ${dirCls}">${direction}</span>
        ${r.three_level_aligned ? '<span class="signal-align">共振</span>' : ''}
        <span class="signal-arrow">\u203A</span>`;

      container.appendChild(row);
    });
  }

  // Part 2: 行业热点 (综合 top5 + 候选股简要)
  if (industryData && industryData.composite_list) {
    const topIndustries = industryData.composite_list.slice(0, 5)
      .filter(ind => ind.zt_count > 0 || ind.composite_score >= 60);
    if (topIndustries.length > 0) {
      const subtitle = document.createElement('div');
      subtitle.className = 'signal-section-title';
      subtitle.textContent = `行业热点 (${topIndustries.length})`;
      container.appendChild(subtitle);

      topIndustries.forEach((ind, i) => {
        const row = document.createElement('div');
        row.className = 'signal-row';
        let stockInfo = '';
        if (ind.candidates && ind.candidates.length > 0) {
          stockInfo = ind.candidates.slice(0, 3).map(c => `${c.role}:${c.name}`).join(' ');
        }
        row.innerHTML = `
          <span class="signal-rank">${i + 1}</span>
          <div class="signal-symbol">
            <div class="signal-symbol-name">${ind.name}</div>
            <div class="signal-symbol-code">综合${ind.composite_score.toFixed(0)} 涨停${ind.zt_count} ${stockInfo}</div>
          </div>
          <span class="signal-score">${ind.composite_score.toFixed(0)}</span>
          <span class="signal-arrow">\u203A</span>`;
        container.appendChild(row);
      });
    }
  }

  // Part 3: L3 个股信号（达标 + 未达标两层）
  const scored = scoredData ? (scoredData.results || scoredData) : [];
  const threshold = scoredData ? (scoredData.threshold || 50) : 50;
  const results = Array.isArray(scored) ? scored : [];

  if (results.length > 0) {
    const above = results.filter(s => s.total_score >= threshold && s.signal_count > 0);
    const below = results.filter(s => s.total_score < threshold || s.signal_count === 0);

    if (above.length > 0) {
      const subtitle = document.createElement('div');
      subtitle.className = 'signal-section-title';
      subtitle.textContent = `达标标的 (${above.length}) 阈值≥${threshold}`;
      container.appendChild(subtitle);

      above.slice(0, 10).forEach((s, i) => {
        const isBuy = s.direction === '偏多';
        // P3-1: 情绪标签
        let sentimentBadge = '';
        if (s.sentiment_tag) {
          const isBoost = s.sentiment_tag.includes('1.25') || s.sentiment_tag.includes('1.10');
          const badgeCls = isBoost ? 'sentiment-boost' : 'sentiment-penalty';
          sentimentBadge = `<span class="badge ${badgeCls}">${s.sentiment_tag}</span>`;
        }
        const row = document.createElement('div');
        row.className = 'signal-row';
        row.innerHTML = `
          <span class="signal-rank">${i + 1}</span>
          <div class="signal-symbol">
            <div class="signal-symbol-name">${s.symbol}</div>
            <div class="signal-symbol-code">${s.signal_count}个信号 ${s.ma_confirmation || ''}</div>
          </div>
          <span class="signal-score">${s.total_score.toFixed(1)}</span>
          ${sentimentBadge}
          <span class="signal-type ${isBuy ? 'buy' : 'sell'}">${s.direction}</span>
          <span class="signal-arrow">\u203A</span>`;
        container.appendChild(row);
      });
    }

    if (below.length > 0) {
      const subtitle = document.createElement('div');
      subtitle.className = 'signal-section-title';
      subtitle.textContent = `未达标 (${below.length}) 最高${below.length > 0 ? below[0].total_score.toFixed(0) : 0}分`;
      container.appendChild(subtitle);

      below.slice(0, 5).forEach((s, i) => {
        const row = document.createElement('div');
        row.className = 'signal-row below-threshold';
        row.innerHTML = `
          <span class="signal-rank">${i + 1}</span>
          <div class="signal-symbol">
            <div class="signal-symbol-name">${s.symbol}</div>
            <div class="signal-symbol-code">${s.signal_count}个信号</div>
          </div>
          <span class="signal-score">${s.total_score.toFixed(1)}</span>
          <span class="signal-type">${s.direction || '—'}</span>`;
        container.appendChild(row);
      });
    }
  }

  if (withSignals.length === 0 && results.length === 0) {
    container.innerHTML = '<div class="empty-state">当前无明确信号</div>';
  }
}

// ── Toast 提示 ──────────────────────────────────────
function showToast(msg) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), 2000);
}

// ── 加载首页 ─────────────────────────────────────────
async function loadDashboard() {
  try {
    // L1: 指数数据（必须，先渲染）
    const [ctx, reports] = await Promise.all([
      apiFetch('/api/index/context'),
      apiFetch('/api/index/reports'),
    ]);
    renderBanner(ctx);
    renderCards(reports);
    renderSentimentPanel(ctx);

    // P3-7: 决策简报（异步，不阻塞）
    apiFetch('/api/index/brief').then(brief => {
      renderDecisionBrief(brief);
    }).catch(e => {
      console.warn('决策简报加载失败:', e);
      document.getElementById('decision-brief').style.display = 'none';
    });

    // L2 + L3 + 操作建议: 异步加载，不阻塞 L1 渲染
    let industryData = null;
    apiFetch('/api/industry/ranking').then(data => {
      industryData = data;
      renderIndustry(data);
    }).catch(e => {
      console.warn('L2 行业加载失败:', e);
      document.getElementById('industry-section').innerHTML =
        '<div class="empty-state">行业数据暂不可用</div>';
    });

    // 操作建议
    apiFetch('/api/index/summary').then(renderActionSummary).catch(e => {
      console.warn('操作建议加载失败:', e);
      document.getElementById('action-section').innerHTML =
        '<div class="empty-state">操作建议暂不可用</div>';
    });

    const scoredData = await apiFetch('/api/screener/results').catch(e => {
      console.warn('L3 标的加载失败:', e);
      return { threshold: 50, results: [] };
    });

    // 合并渲染信号列表（等 industry 数据到达）
    // 用短延时确保 industryData 可能已到达
    setTimeout(() => {
      renderSignalList(reports, scoredData, industryData);
    }, 500);

  } catch (err) {
    console.error('Dashboard load failed:', err);
    document.getElementById('banner-direction').textContent =
      '数据加载失败: ' + err.message;
  }
}

window.loadDashboard = loadDashboard;
