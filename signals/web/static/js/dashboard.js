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
  if (ctx.divergence_score != null && ctx.divergence_score > 0) {
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
      // 优先用小级别快照价，fallback 到日线收盘
      const snapPrice = r.snapshot_price || r.latest_price;
      const priceStr = snapPrice ? snapPrice.toFixed(2) : '';

      // 盘中涨跌幅（vs 前日收盘）— 替代之前误导性的 5 日收益
      let changeHtml = '';
      if (r.intraday_change !== null && r.intraday_change !== undefined) {
        const chgCls = r.intraday_change > 0 ? 'up' : r.intraday_change < 0 ? 'down' : 'flat';
        changeHtml = `<span class="card-return ${chgCls}">${r.intraday_change > 0 ? '+' : ''}${r.intraday_change.toFixed(2)}%</span>`;
      }

      // 快照时间标签（如 "15M 14:30" 或 "日线 03-09"）
      let snapTimeHtml = '';
      if (r.snapshot_dt) {
        let timeLabel = '';
        if (r.snapshot_freq === '15M' || r.snapshot_freq === '30M') {
          // 分钟级别：显示 HH:MM
          const parts = r.snapshot_dt.split(' ');
          timeLabel = parts.length > 1 ? parts[1] : r.snapshot_dt;
        } else {
          // 日线级别：显示 MM-DD
          const parts = r.snapshot_dt.split(' ')[0].split('-');
          timeLabel = parts.length >= 3 ? `${parts[1]}-${parts[2]}` : r.snapshot_dt;
        }
        snapTimeHtml = `<div class="card-snap-time">${r.snapshot_freq || ''} ${timeLabel}</div>`;
      }

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
            const lvVal = (lv.value != null) ? lv.value.toFixed(0) : '—';
            maHtml += `<span class="card-level ${cls}">${arrow}${lv.name} ${lvVal}</span>`;
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
          const lvPrice = (s.level_price != null) ? s.level_price.toFixed(0) : '—';
          const expanded = urgent.length > 0 ? 'true' : 'false';
          scenarioHtml = `<div class="card-scenarios" data-expanded="${expanded}">
            <div class="scenario-toggle">\u{1F500} <span class="urgency-dot urgent"></span></div>
            <div class="scenario-body">
              <div class="scenario-branch support">IF \u5B88\u4F4F ${lvPrice} (${s.level_name}) \u2192 ${s.hold}</div>
              <div class="scenario-branch break-branch">IF \u8DCC\u7834 ${lvPrice} (${s.level_name}) \u2192 ${s['break']}</div>
            </div>
          </div>`;
        }
      }

      // 近5日收益率 — 移到卡片底部，加明确标签
      let ret5dHtml = '';
      if (r.recent_5d_return !== null && r.recent_5d_return !== undefined) {
        const retCls = r.recent_5d_return > 0 ? 'up' : r.recent_5d_return < 0 ? 'down' : 'flat';
        ret5dHtml = `<div class="card-5d-return"><span class="label-5d">近5日</span> <span class="${retCls}">${r.recent_5d_return > 0 ? '+' : ''}${r.recent_5d_return.toFixed(1)}%</span></div>`;
      }

      const alignBadge = r.three_level_aligned
        ? '<span class="card-align-badge">三级共振</span>'
        : '';

      card.innerHTML = `
        <div class="card-header">
          <div class="card-name">${name} ${changeHtml}</div>
          ${alignBadge}
        </div>
        <div class="card-trend ${trend.cls}">${trend.arrow} ${trend.label}</div>
        ${signalHtml}
        <div class="card-price">${priceStr}</div>
        ${snapTimeHtml}
        ${maHtml}
        ${scenarioHtml}
        ${ret5dHtml}`;

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

// ── 概念板块排行（主视图）──────────────────────────────
let _cachedIndustryData = null;
let _cachedConceptData = null;

function renderConceptRanking(data) {
  const container = document.getElementById('industry-section');
  if (!data || !data.concept_list || data.concept_list.length === 0) {
    _renderIndustryTabs('concept', '<div class="empty-state">概念数据未加载</div>');
    return;
  }

  let html = '';
  html += '<div class="industry-panorama"><table>';
  html += '<tr><th>#</th><th>概念</th><th>综合</th><th>涨幅</th><th>涨/跌</th><th>换手</th><th>领涨</th><th>属性</th></tr>';
  data.concept_list.forEach((c, i) => {
    const pctCls = c.gain_pct >= 0 ? 'up' : 'down';
    const scoreCls = c.composite_score >= 70 ? 'score-high' : '';
    const leadCls = c.leading_gain >= 5 ? 'up' : c.leading_gain >= 0 ? '' : 'down';
    const total = c.up_count + c.down_count;
    const upPct = total > 0 ? (c.up_count / total * 100) : 50;

    // 属性标签
    let attrHtml = `<span class="tag tag-${c.sector_type}">${c.sector_type}</span>`;
    if (c.related_industries && c.related_industries.length > 0) {
      attrHtml += c.related_industries.slice(0, 2).map(r =>
        `<span class="tag tag-rotation" onclick="event.stopPropagation();navigateToIndustryChart('${r}')" style="cursor:pointer">${r}</span>`
      ).join('');
    }

    // 涨跌比例条
    const udBar = total > 0
      ? `<div class="ud-bar"><div class="ud-up" style="width:${upPct}%"></div></div><small>${c.up_count}\u2191 ${c.down_count}\u2193</small>`
      : '<small>-</small>';

    html += `<tr class="industry-row ${scoreCls}" onclick="navigateToTheme('${c.name}')">
      <td>${i + 1}</td>
      <td class="industry-name-cell">${c.name}</td>
      <td class="score-cell">${c.composite_score.toFixed(0)}</td>
      <td class="${pctCls}">${c.gain_pct > 0 ? '+' : ''}${c.gain_pct.toFixed(2)}%</td>
      <td class="ud-cell">${udBar}</td>
      <td>${c.turnover_rate.toFixed(1)}%</td>
      <td class="${leadCls}">${c.leading_stock} ${c.leading_gain > 0 ? '+' : ''}${c.leading_gain.toFixed(1)}%</td>
      <td>${attrHtml}</td>
    </tr>`;
  });
  html += '</table></div>';
  _renderIndustryTabs('concept', html);
}

function _renderIndustryTabs(activeTab, contentHtml) {
  const container = document.getElementById('industry-section');
  const cActive = activeTab === 'concept' ? 'active' : '';
  const iActive = activeTab === 'industry' ? 'active' : '';
  const tabs = `<div class="ranking-tabs">
    <span class="ranking-tab ${cActive}" onclick="switchRankingTab('concept')">概念热点</span>
    <span class="ranking-tab ${iActive}" onclick="switchRankingTab('industry')">行业全景</span>
  </div>`;
  container.innerHTML = tabs + contentHtml;
}

function switchRankingTab(tab) {
  if (tab === 'concept' && _cachedConceptData) {
    renderConceptRanking(_cachedConceptData);
  } else if (tab === 'industry' && _cachedIndustryData) {
    renderIndustryTable(_cachedIndustryData);
  }
}
window.switchRankingTab = switchRankingTab;

// ── 行业全景排行（合并单表 + 统计摘要）──────────────
function renderIndustry(data) {
  _cachedIndustryData = data;
  // 默认显示概念排行（如果已有数据），否则先显示行业
  if (_cachedConceptData) {
    renderConceptRanking(_cachedConceptData);
    return;
  }
  renderIndustryTable(data);
}

function renderIndustryTable(data) {
  const { composite_list, oversold_list, stats } = data;

  if (!composite_list || composite_list.length === 0) {
    _renderIndustryTabs('industry', '<div class="empty-state">行业数据未加载</div>');
    return;
  }

  let html = '';

  // 兑现提醒横幅
  const rhythmAlerts = composite_list.slice(0, 10).filter(
    ind => ind.phase && (ind.phase === '衰竭' || ind.phase === '高潮')
  );
  if (rhythmAlerts.length > 0) {
    html += '<div class="rhythm-alert">\u23F0 兑现提醒: ';
    html += rhythmAlerts.map(ind =>
      `${ind.display_name || ind.name}(${ind.phase})`
    ).join(' \u00B7 ');
    html += ' \u2014 注意板块节奏</div>';
  }

  // 行业全景表
  html += '<div class="industry-panorama"><table>';
  html += '<tr><th>#</th><th>行业</th><th>综合</th><th>涨幅</th><th>涨停</th><th>净流入</th><th>阶段</th><th>属性</th><th>候选股</th></tr>';
  composite_list.slice(0, 10).forEach((ind, i) => {
    const panelId = `pano-panel-${i}`;
    const pctCls = ind.gain_pct >= 0 ? 'up' : 'down';
    const scoreCls = ind.composite_score >= 70 ? 'score-high' : '';
    const ztBold = ind.zt_count >= 3 ? 'zt-hot' : '';
    const inflowBold = Math.abs(ind.net_inflow) >= 1 ? 'inflow-hot' : '';

    // 阶段标签（启动/加速/高潮/衰竭/休整）
    const phaseClsMap = {
      '启动': 'phase-startup', '加速': 'phase-accel', '高潮': 'phase-peak',
      '衰竭': 'phase-exhaust', '休整': 'phase-rest',
    };
    let phaseCell = '';
    if (ind.phase) {
      const pCls = phaseClsMap[ind.phase] || '';
      const hint = ind.phase_hint ? ` ${ind.phase_hint}` : '';
      phaseCell = `<span class="tag tag-phase ${pCls}" title="${ind.phase_hint || ''}">${ind.phase}${hint}</span>`;
    }

    // 候选股简要
    let stockBrief = '';
    if (ind.candidates && ind.candidates.length > 0) {
      stockBrief = ind.candidates.slice(0, 2).map(c => `${c.role}:${c.name}`).join(' ');
    }

    html += `<tr class="industry-row ${scoreCls}" data-panel="${panelId}" data-industry="${ind.name}">
      <td>${i + 1}</td>
      <td class="industry-name-cell"><span class="ind-chart-btn" onclick="event.stopPropagation();navigateToIndustryChart('${ind.name}')" title="查看K线">&#9632;</span> ${ind.display_name || ind.name}</td>
      <td class="score-cell">${ind.composite_score.toFixed(0)}</td>
      <td class="${pctCls}">${ind.gain_pct > 0 ? '+' : ''}${ind.gain_pct.toFixed(2)}%</td>
      <td class="${ztBold}">${ind.zt_count}</td>
      <td class="${inflowBold}">${ind.net_inflow > 0 ? '+' : ''}${ind.net_inflow.toFixed(1)}亿</td>
      <td>${phaseCell}</td>
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

  _renderIndustryTabs('industry', html);

  // 行业行点击 → 展开/收起候选股
  const container = document.getElementById('industry-section');
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

  // 1. 恐慌评估（始终显示，含市场状态）
  const panic = summary.panic;
  if (panic) {
    const score = panic.score || 0;
    const ms = panic.market_state || '平稳';
    let cls, emoji;
    if (score >= 60) { cls = 'high'; emoji = '\uD83D\uDD34'; }
    else if (score >= 40) { cls = 'mid'; emoji = '\uD83D\uDFE1'; }
    else if (score >= 20) { cls = 'low'; emoji = '\uD83D\uDCCA'; }
    else { cls = 'safe'; emoji = '\u2705'; }
    const stateEmoji = {'急跌': '\uD83D\uDD34', '缓跌': '\uD83D\uDFE1', '企稳': '\uD83D\uDFE2', '反弹': '\uD83D\uDCC8', '平稳': '\u26AA'};
    const msTag = `<span class="market-state-tag state-${ms}">${stateEmoji[ms] || ''}[${ms}]</span>`;
    html += `<div class="panic-banner ${cls}">
      <div>
        <div>${emoji} 恐慌指数 <span class="panic-score">${score.toFixed(0)}</span>/100 — ${panic.level} ${msTag}</div>
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

  // 2. L1 指数策略（基于 market_state 多维度判断）
  if (summary.l1_guidance && summary.l1_guidance.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">指数策略指引</div>';
    html += '<table class="action-table"><tr><th>指数</th><th>趋势</th><th>信号</th><th>操作建议</th></tr>';
    summary.l1_guidance.forEach(g => {
      const tInfo = trendInfo(g.trend);
      // 多维度 action 着色
      const buyActions = ['恐慌抄底窗口', '确认买入', '积极关注', '逢低关注', '持仓待涨', '可关注', '轻仓跟随'];
      const sellActions = ['回避，勿追跌', '减仓观望', '反弹减仓', '需回避'];
      const actionCls = buyActions.includes(g.action) ? 'action-buy'
        : sellActions.includes(g.action) ? 'action-sell' : 'action-wait';
      html += `<tr>
        <td>${g.name}${g.aligned ? ' <span class="card-align-badge">共振</span>' : ''}</td>
        <td class="${tInfo.cls}">${tInfo.arrow} ${tInfo.label}</td>
        <td>${g.signals.join(' ')}</td>
        <td class="${actionCls}">${g.action}</td>
      </tr>`;
    });
    html += '</table></div>';
  }

  // 3. L2 行业策略（基于 rhythm + 资金 + market_state）
  if (summary.l2_actions && summary.l2_actions.length > 0) {
    html += '<div class="action-subsection"><div class="action-subsection-title">行业策略指引</div>';
    html += '<table class="action-table"><tr><th>行业</th><th>综合</th><th>涨幅</th><th>涨停</th><th>阶段</th><th>建议</th><th>头部个股</th></tr>';
    summary.l2_actions.forEach(a => {
      const buyVerdicts = ['刚进攻', '追强', '关注', '关注启动'];
      const sellVerdicts = ['高抛兑现', '回避'];
      const verdictCls = buyVerdicts.some(v => a.verdict.includes(v)) ? 'action-buy'
        : sellVerdicts.some(v => a.verdict.includes(v)) ? 'action-sell' : 'action-wait';
      const pctCls = (a.gain_pct || 0) >= 0 ? 'up' : 'down';
      const rhythmPhaseMap = {
        '启动': 'rhythm-start', '加速': 'rhythm-accel', '高潮': 'rhythm-peak',
        '衰竭': 'rhythm-exhaust', '休整': 'rhythm-rest',
      };
      const rCls = rhythmPhaseMap[a.rhythm] || '';
      html += `<tr>
        <td>${a.name}</td>
        <td class="score-cell">${a.score.toFixed(0)}</td>
        <td class="${pctCls}">${(a.gain_pct || 0) > 0 ? '+' : ''}${(a.gain_pct || 0).toFixed(1)}%</td>
        <td>${a.zt}</td>
        <td><span class="rhythm ${rCls}">${a.rhythm || '—'}</span></td>
        <td class="${verdictCls}" title="${a.verdict_detail || ''}">${a.verdict}</td>
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

  // 8. 结论
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
    const getScore = s => (s.fused_total && s.fused_total > 0) ? s.fused_total : s.total_score;
    const above = results.filter(s => getScore(s) >= threshold && s.signal_count > 0);
    const below = results.filter(s => getScore(s) < threshold || s.signal_count === 0);

    if (above.length > 0) {
      const subtitle = document.createElement('div');
      subtitle.className = 'signal-section-title';
      subtitle.textContent = `达标标的 (${above.length}) 阈值≥${threshold}`;
      container.appendChild(subtitle);

      above.slice(0, 10).forEach((s, i) => {
        const isBuy = s.direction === '偏多';
        const displayName = s.name || s.symbol;
        // P3-1: 情绪标签
        let sentimentBadge = '';
        if (s.sentiment_tag) {
          const isBoost = s.sentiment_tag.includes('1.25') || s.sentiment_tag.includes('1.10');
          const badgeCls = isBoost ? 'sentiment-boost' : 'sentiment-penalty';
          sentimentBadge = `<span class="badge ${badgeCls}">${s.sentiment_tag}</span>`;
        }
        // 社交热度徽章
        let heatBadge = '';
        if (s.social_heat && s.social_heat !== '冷门') {
          const heatCls = { '\u7206\u70ED': 'heat-fire', '\u70ED\u95E8': 'heat-hot', '\u6E29\u548C': 'heat-warm' }[s.social_heat] || '';
          heatBadge = `<span class="badge ${heatCls}">${s.social_heat}</span>`;
        }
        // 关联主题tags
        let themeTags = '';
        if (s.theme_tags && s.theme_tags.length > 0) {
          themeTags = s.theme_tags.slice(0, 2).map(t => `<span class="tag tag-theme" style="font-size:10px;padding:1px 4px;">${t}</span>`).join('');
        }
        // 主分数：优先融合分
        const mainScore = (s.fused_total && s.fused_total > 0) ? s.fused_total : s.total_score;
        const czscSub = (s.fused_total && s.fused_total > 0) ? `<span class="signal-czsc-sub">(缠论${s.total_score.toFixed(0)})</span>` : '';

        // 异常指示器
        let anomalyStrip = '';
        if (s.anomaly && s.anomaly.items && s.anomaly.items.length > 0) {
          const dimNames = {volume: '量能', range: '振幅', gap: '跳空', body: '实体', vol_price_div: '背离'};
          let dots = s.anomaly.items.map(item => {
            const label = dimNames[item.name] || item.name;
            const cls = item.is_anomaly ? 'fired' : 'normal';
            return `<span class="signal-anomaly-dot ${cls}">${item.is_anomaly ? label + 'z' + Math.abs(item.z_score).toFixed(1) : ''}</span>`;
          }).filter(d => d.includes('fired')).join('');
          let extras = '';
          if (s.anomaly.convergence && s.anomaly.anomaly_count >= 2) {
            extras += `<span class="signal-anomaly-dot conv">${s.anomaly.anomaly_count}维收敛</span>`;
          }
          if (s.anomaly.capitulation_score >= 40) {
            extras += `<span class="signal-anomaly-dot cap">割肉${s.anomaly.capitulation_score.toFixed(0)}</span>`;
          }
          if (dots || extras) {
            anomalyStrip = `<div class="signal-anomaly-strip">${dots}${extras}</div>`;
          }
        }

        const row = document.createElement('div');
        row.className = 'signal-row';
        row.innerHTML = `
          <span class="signal-rank">${i + 1}</span>
          <div class="signal-symbol">
            <div class="signal-symbol-name">${displayName}</div>
            <div class="signal-symbol-code">${s.symbol} | ${s.signal_count}个信号 ${s.ma_confirmation || ''} ${czscSub}</div>
            ${anomalyStrip}
          </div>
          <span class="signal-score">${mainScore.toFixed(1)}</span>
          ${sentimentBadge}
          ${heatBadge}
          <span class="signal-type ${isBuy ? 'buy' : 'sell'}">${s.direction}</span>
          ${themeTags}
          <span class="signal-arrow">\u203A</span>`;
        container.appendChild(row);
      });
    }

    if (below.length > 0) {
      const topScore = below.length > 0 ? getScore(below[0]).toFixed(0) : 0;
      const subtitle = document.createElement('div');
      subtitle.className = 'signal-section-title';
      subtitle.textContent = `未达标 (${below.length}) 最高${topScore}分`;
      container.appendChild(subtitle);

      below.slice(0, 5).forEach((s, i) => {
        const displayName = s.name || s.symbol;
        const score = getScore(s);
        const row = document.createElement('div');
        row.className = 'signal-row below-threshold';
        row.innerHTML = `
          <span class="signal-rank">${i + 1}</span>
          <div class="signal-symbol">
            <div class="signal-symbol-name">${displayName}</div>
            <div class="signal-symbol-code">${s.symbol} | ${s.signal_count}个信号</div>
          </div>
          <span class="signal-score">${score.toFixed(1)}</span>
          <span class="signal-type">${s.direction || '—'}</span>`;
        container.appendChild(row);
      });
    }
  }

  if (withSignals.length === 0 && results.length === 0) {
    container.innerHTML = '<div class="empty-state">当前无明确信号</div>';
  }
}

// ── 今日舆情 section ─────────────────────────────────
function renderSocialSection(data) {
  const container = document.getElementById('social-section');
  if (!container) return;
  let html = '';

  // 热门主题词
  if (data.hot_themes && data.hot_themes.length > 0) {
    html += '<div class="social-themes-row">';
    data.hot_themes.forEach(t => {
      const pctCls = t.change_pct >= 0 ? 'up' : 'down';
      html += `<span class="tag tag-theme" onclick="navigateToTheme('${t.name}')">${t.name} <small class="${pctCls}">${t.change_pct > 0 ? '+' : ''}${t.change_pct.toFixed(1)}%</small></span>`;
    });
    html += '</div>';
  }

  // 飙升标的
  if (data.surge_stocks && data.surge_stocks.length > 0) {
    html += '<div class="social-surge-row">';
    data.surge_stocks.forEach(s => {
      const pctCls = s.change_pct >= 0 ? 'up' : 'down';
      html += `<span class="surge-chip" onclick="navigateToStock('${s.symbol}')">
        ${s.name} <span class="badge heat-fire">千评${s.score.toFixed(0)}</span>
        <small class="${pctCls}">${s.change_pct > 0 ? '+' : ''}${s.change_pct.toFixed(1)}%</small>
      </span>`;
    });
    html += '</div>';
  }

  container.innerHTML = html || '<div class="empty-state">暂无舆情数据</div>';
}

// 导航到主题发现（切到个股页搜索主题）
function navigateToTheme(theme) {
  switchPage('stock');
  const input = document.getElementById('stock-input');
  if (input) {
    input.value = theme;
    analyzeStock();
  }
}

// 导航到个股分析
function navigateToStock(symbol) {
  switchPage('stock');
  const input = document.getElementById('stock-input');
  if (input) {
    input.value = symbol;
    analyzeStock();
  }
}

window.navigateToTheme = navigateToTheme;
window.navigateToStock = navigateToStock;

// ── Toast 提示 ──────────────────────────────────────
// showToast() 已移至 app.js 全局工具

// ── 加载首页 ─────────────────────────────────────────
// ── 加载进度指示 ────────────────────────────────────
const PHASE_LABELS = {
  'L1': '指数数据加载中...',
  'L2': '行业数据加载中...',
  'L3': '标的筛选中...',
  '': '完成',
};

function showLoadingOverlay(phase) {
  let overlay = document.getElementById('loading-overlay');
  if (!overlay) {
    overlay = document.createElement('div');
    overlay.id = 'loading-overlay';
    overlay.innerHTML = `
      <div class="loading-card">
        <div class="loading-spinner"></div>
        <div class="loading-title">隆小侠正在分析市场</div>
        <div class="loading-phase" id="loading-phase-text"></div>
        <div class="loading-hint">服务器已启动，数据后台加载中</div>
      </div>`;
    document.body.appendChild(overlay);
  }
  const phaseEl = document.getElementById('loading-phase-text');
  if (phaseEl) phaseEl.textContent = PHASE_LABELS[phase] || phase;
  overlay.style.display = 'flex';
}

function hideLoadingOverlay() {
  const overlay = document.getElementById('loading-overlay');
  if (overlay) overlay.style.display = 'none';
}

async function waitForEngine() {
  /**
   * 轮询 /api/index/status 直到 ready=true。
   * 显示加载进度。返回 true 当就绪。
   */
  let attempts = 0;
  const MAX_ATTEMPTS = 180;  // 最多等 6 分钟
  while (attempts < MAX_ATTEMPTS) {
    try {
      const status = await apiFetch('/api/index/status');
      if (status.ready) {
        hideLoadingOverlay();
        return true;
      }
      if (status.error) {
        showLoadingOverlay('错误: ' + status.error);
        return false;
      }
      showLoadingOverlay(status.loading_phase || 'L1');
    } catch (e) {
      showLoadingOverlay('连接中...');
    }
    await new Promise(r => setTimeout(r, 2000));
    attempts++;
  }
  hideLoadingOverlay();
  return false;
}

async function loadDashboard() {
  try {
    // 先检查引擎是否就绪
    let status;
    try {
      status = await apiFetch('/api/index/status');
    } catch (e) {
      status = { ready: false };
    }

    if (!status.ready) {
      // 引擎还在加载 → 显示进度，轮询等待
      const ok = await waitForEngine();
      if (!ok) {
        document.getElementById('banner-direction').textContent = '数据加载超时，请刷新重试';
        return;
      }
    }

    // 时段指示器（盘中/盘后/盘前标识）
    initSessionIndicator();

    // 预测数据（置顶，优先加载）
    loadPredictionSection();

    // 引擎就绪(L1完成) → 立即渲染 L1 部分
    const [ctx, reports] = await Promise.all([
      apiFetch('/api/index/context'),
      apiFetch('/api/index/reports'),
    ]);
    renderBanner(ctx);
    renderCards(reports);
    renderSentimentPanel(ctx);

    // 社交舆情（并行加载）
    apiFetch('/api/social/brief').then(renderSocialSection).catch(e => {
      console.warn('社交舆情加载失败:', e);
      const sc = document.getElementById('social-section');
      if (sc) sc.innerHTML = '<div class="empty-state">舆情数据暂不可用</div>';
    });

    // L2 部分：轮询直到行业数据就绪
    let industryData = null;
    const loadL2Data = async () => {
      const indSection = document.getElementById('industry-section');
      const actSection = document.getElementById('action-section');
      let retries = 0;
      while (retries < 60) {
        try {
          const data = await apiFetch('/api/industry/ranking');
          if (data.loading) {
            indSection.innerHTML = '<div class="empty-state"><div class="loading-spinner" style="width:24px;height:24px;border-width:2px;margin:0 auto 8px;"></div>行业数据加载中...</div>';
            await new Promise(r => setTimeout(r, 2000));
            retries++;
            continue;
          }
          industryData = data;
          renderIndustry(data);
          // L2 就绪后并行加载概念排行
          apiFetch('/api/industry/concept-ranking').then(cData => {
            if (!cData.loading && cData.concept_list) {
              _cachedConceptData = cData;
              renderConceptRanking(cData);  // 切换到概念视图
            }
          }).catch(e => console.warn('概念排行加载失败:', e));
          break;
        } catch (e) {
          console.warn('L2 行业加载失败:', e);
          indSection.innerHTML = '<div class="empty-state">行业数据暂不可用</div>';
          break;
        }
      }
      // L2 就绪后加载操作建议
      apiFetch('/api/index/summary').then(renderActionSummary).catch(e => {
        console.warn('操作建议加载失败:', e);
        actSection.innerHTML = '<div class="empty-state">操作建议暂不可用</div>';
      });
    };
    loadL2Data();

    // L3 部分：轮询直到标的数据就绪
    const loadL3Data = async () => {
      let retries = 0;
      while (retries < 90) {
        try {
          const st = await apiFetch('/api/index/status');
          if (st.loading_phase) {
            await new Promise(r => setTimeout(r, 3000));
            retries++;
            continue;
          }
          break;
        } catch (e) { break; }
      }
      const scoredData = await apiFetch('/api/screener/results').catch(e => {
        console.warn('L3 标的加载失败:', e);
        return { threshold: 50, results: [] };
      });
      // 等 industry 数据就绪再渲染信号列表
      const waitAndRender = () => {
        if (industryData !== null) {
          renderSignalList(reports, scoredData, industryData);
        } else {
          setTimeout(waitAndRender, 500);
        }
      };
      waitAndRender();
    };
    loadL3Data();

  } catch (err) {
    console.error('Dashboard load failed:', err);
    document.getElementById('banner-direction').textContent =
      '数据加载失败: ' + err.message;
  }
}

// ═══════════════════════════════════════════════════
// 预测模块渲染
// ═══════════════════════════════════════════════════

async function loadPredictionSection() {
  const container = document.getElementById('prediction-section');
  const regimeEl = document.getElementById('regime-section');
  if (!container) return;

  // 轮询等待预测数据就绪
  let retries = 0;
  while (retries < 60) {
    try {
      const data = await apiFetch('/api/prediction/overview');
      if (data.loading) {
        container.innerHTML = '<div class="empty-state"><div class="loading-spinner" style="width:24px;height:24px;border-width:2px;margin:0 auto 8px;"></div>预测数据加载中...</div>';
        await new Promise(r => setTimeout(r, 3000));
        retries++;
        continue;
      }
      renderPrediction(container, data);
      if (regimeEl && data.market_regime) {
        renderRegime(regimeEl, data.market_regime);
      }
      return;
    } catch (e) {
      console.warn('预测数据加载失败:', e);
      await new Promise(r => setTimeout(r, 3000));
      retries++;
    }
  }
  container.innerHTML = '<div class="empty-state">预测数据暂不可用</div>';
}

function renderPrediction(container, data) {
  let html = '';

  // ── 行业预测 ──
  html += '<div class="prediction-block">';
  html += '<div class="prediction-header">行业预测</div>';

  // 买入预测
  if (data.sector_buy && data.sector_buy.length > 0) {
    html += '<div class="prediction-sub-header prediction-buy-header">买入预测 · 板块启动信号</div>';
    html += '<div class="prediction-list">';
    data.sector_buy.forEach((s, i) => {
      const levelCls = s.signal_level === '强' ? 'level-strong' : s.signal_level === '中' ? 'level-mid' : 'level-weak';
      const movers = (s.top_movers || []).slice(0, 3).map(m =>
        `<span class="mover-tag" onclick="navigateToStock('${m.code}')">${m.name}(${m.momentum_days}d)</span>`
      ).join('');
      html += `<div class="prediction-row ${levelCls}">
        <span class="pred-rank">#${i + 1}</span>
        <span class="pred-name">${s.concept_name}</span>
        <span class="pred-badge badge-${levelCls}">${s.signal_level}</span>
        <span class="pred-score">动量${s.momentum_score.toFixed(0)}</span>
        <span class="pred-detail">${s.detail || ''}</span>
        <div class="pred-movers">${movers}</div>
      </div>`;
    });
    html += '</div>';
  }

  // 卖出预警
  if (data.sector_sell && data.sector_sell.length > 0) {
    html += '<div class="prediction-sub-header prediction-sell-header">卖出预警 · 板块见顶信号</div>';
    html += '<div class="prediction-list">';
    data.sector_sell.forEach((s, i) => {
      html += `<div class="prediction-row level-warn">
        <span class="pred-rank">#${i + 1}</span>
        <span class="pred-name">${s.concept_name}</span>
        <span class="pred-badge badge-warn">⚠</span>
        <span class="pred-detail">${s.sell_signal || ''} 分化${(s.bearish_ratio * 100).toFixed(0)}%</span>
      </div>`;
    });
    html += '</div>';
  }

  if ((!data.sector_buy || data.sector_buy.length === 0) && (!data.sector_sell || data.sector_sell.length === 0)) {
    html += '<div class="empty-state">暂无板块动量信号</div>';
  }
  html += '</div>';

  // ── 个股预测 ──
  html += '<div class="prediction-block">';
  html += '<div class="prediction-header">个股预测</div>';

  // 买入预测
  if (data.stock_buy && data.stock_buy.length > 0) {
    html += '<div class="prediction-sub-header prediction-buy-header">买入预测 · 个股启动/抄底</div>';
    html += '<div class="prediction-list">';
    data.stock_buy.slice(0, 10).forEach((s, i) => {
      const dyn = s.dynamics || {};
      const dynDetail = dyn.signal ? `${dyn.signal}` : '';
      const fDetail = s.fusion_detail || {};
      const scoreCls = s.dynamics_merged_score > 50 ? 'score-high' : s.dynamics_merged_score > 20 ? 'score-mid' : 'score-low';
      html += `<div class="prediction-row stock-row" onclick="navigateToStock('${s.symbol}')">
        <span class="pred-rank">#${i + 1}</span>
        <span class="pred-name">${s.name || s.symbol}</span>
        <span class="pred-score ${scoreCls}">+${s.dynamics_merged_score.toFixed(0)}</span>
        <span class="pred-detail">${dynDetail}</span>
        <span class="pred-conf">${fDetail.confidence || ''}</span>
      </div>`;
    });
    html += '</div>';
  }

  // 卖出预警
  if (data.stock_sell && data.stock_sell.length > 0) {
    html += '<div class="prediction-sub-header prediction-sell-header">卖出预警 · 个股见顶/衰竭</div>';
    html += '<div class="prediction-list">';
    data.stock_sell.slice(0, 5).forEach((s, i) => {
      const sw = s.sell_warning || {};
      html += `<div class="prediction-row level-warn stock-row" onclick="navigateToStock('${s.symbol}')">
        <span class="pred-rank">#${i + 1}</span>
        <span class="pred-name">${s.name || s.symbol}</span>
        <span class="pred-badge badge-warn">⚠${sw.score || 0}</span>
        <span class="pred-detail">${sw.warning || ''}</span>
      </div>`;
    });
    html += '</div>';
  }

  if ((!data.stock_buy || data.stock_buy.length === 0) && (!data.stock_sell || data.stock_sell.length === 0)) {
    html += '<div class="empty-state">暂无个股预测信号</div>';
  }
  html += '</div>';

  container.innerHTML = html;
}

function renderRegime(el, regime) {
  if (!regime) { el.style.display = 'none'; return; }
  el.style.display = '';
  const mult = regime.regime_mult || 1.0;
  const barWidth = Math.round(mult * 100);
  const colorCls = mult > 0.8 ? 'regime-bull' : mult > 0.5 ? 'regime-neutral' : 'regime-bear';
  el.innerHTML = `<div class="regime-bar ${colorCls}">
    <span class="regime-label">市场环境</span>
    <span class="regime-stats">涨停${regime.zt_total} 跌停${regime.dt_total} 连板${regime.lianban_max}</span>
    <span class="regime-tag">${regime.label}</span>
    <div class="regime-fill" style="width:${barWidth}%"></div>
  </div>`;
}

// ═══════════════════════════════════════════════════
// 时段指示器 + 自动刷新
// ═══════════════════════════════════════════════════

const SESSION_CONFIG = {
  'pre_market':   { cls: 'pre',     icon: '🌅' },
  'ah_intraday':  { cls: 'live',    icon: '🟢' },
  'hk_tail':      { cls: 'partial', icon: '🔶' },
  'ah_post':      { cls: 'review',  icon: '📊' },
  'us_intraday':  { cls: 'live',    icon: '🇺🇸' },
  'overnight':    { cls: 'off',     icon: '🌙' },
};

function renderSessionIndicator(status) {
  const badge = document.getElementById('session-badge');
  const timeEl = document.getElementById('session-time');
  if (!badge || !status.session_mode) return;

  const cfg = SESSION_CONFIG[status.session_mode] || { cls: '', icon: '' };
  badge.textContent = `${cfg.icon} ${status.session_label || ''}`;
  badge.className = `session-badge ${cfg.cls}`;

  if (timeEl && status.data_as_of) {
    timeEl.textContent = `数据截至 ${status.data_as_of}`;
  }

  // 动态信号区标题
  const titleEl = document.getElementById('signal-section-title');
  if (titleEl) {
    if (status.session_mode === 'ah_post' || status.session_mode === 'pre_market' || status.session_mode === 'overnight') {
      titleEl.textContent = '次日预判';
    } else if (status.session_mode === 'ah_intraday' || status.session_mode === 'hk_tail') {
      titleEl.textContent = '实时信号';
    } else {
      titleEl.textContent = '今日信号';
    }
  }
}

let _autoRefreshTimer = null;
let _lastUpdate = 0;

function startAutoRefresh(intervalMs) {
  stopAutoRefresh();
  if (intervalMs <= 0) return;

  _autoRefreshTimer = setInterval(async () => {
    try {
      const st = await apiFetch('/api/index/status');
      renderSessionIndicator(st);

      // 时段变化（如收盘了）→ 停止刷新
      if (!st.refresh_interval || st.refresh_interval === 0) {
        stopAutoRefresh();
        return;
      }

      // 有新数据 → 重新加载 dashboard
      if (st.ready && !st.running && st.last_update > _lastUpdate) {
        _lastUpdate = st.last_update;
        loadDashboard();
      }
    } catch (e) {
      console.warn('自动刷新检查失败:', e);
    }
  }, intervalMs);
}

function stopAutoRefresh() {
  if (_autoRefreshTimer) {
    clearInterval(_autoRefreshTimer);
    _autoRefreshTimer = null;
  }
}

// 在 loadDashboard 完成后初始化时段指示器
async function initSessionIndicator() {
  try {
    const st = await apiFetch('/api/index/status');
    renderSessionIndicator(st);
    _lastUpdate = st.last_update || 0;
    if (st.refresh_interval && st.refresh_interval > 0) {
      startAutoRefresh(st.refresh_interval * 1000);
    }
  } catch (e) {
    console.warn('时段信息获取失败:', e);
  }
}

window.loadDashboard = loadDashboard;
