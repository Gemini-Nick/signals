/**
 * 隆小侠 LONG CLAW — 市况总览页
 * 建议条 + 指数卡片 + 信号列表
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

// ── 建议条 ───────────────────────────────────────────
function renderBanner(ctx) {
  const banner = document.getElementById('banner');
  const dirEl = document.getElementById('banner-direction');
  const sugEl = document.getElementById('banner-suggestion');

  // 方向背景色
  banner.className = 'banner';
  if (ctx.overall_direction === '偏多') {
    banner.classList.add('bullish');
  } else if (ctx.overall_direction === '偏空') {
    banner.classList.add('bearish');
  } else {
    banner.classList.add('neutral');
  }

  // 方向 + 情绪
  const dirEmoji = { '偏多': '\u2191', '偏空': '\u2193', '分化': '\u2194' };
  const phaseStr = ctx.sentiment_phase !== '未知' ? `    情绪: ${ctx.sentiment_phase}` : '';
  dirEl.textContent = `大盘方向: ${ctx.overall_direction} ${dirEmoji[ctx.overall_direction] || ''}${phaseStr}`;

  // 仓位建议
  sugEl.textContent = ctx.position_suggestion || ctx.summary || '';
}

// ── 指数卡片 ─────────────────────────────────────────
function renderCards(reports, containerIds) {
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

      // 收集信号
      const signals = [];
      if (r.daily_latest_signal !== '无') signals.push(r.daily_latest_signal);
      if (r.f30_latest_signal !== '无') signals.push(r.f30_latest_signal);
      if (r.f15_latest_signal !== '无') signals.push(r.f15_latest_signal);
      const uniqueSignals = [...new Set(signals)];

      let signalHtml = '';
      if (uniqueSignals.length > 0) {
        signalHtml = uniqueSignals.map(s =>
          `<span class="card-signal">${s}</span>`
        ).join(' ');
      }

      card.innerHTML = `
        <div class="card-name">${name}</div>
        <div class="card-trend ${trend.cls}">${trend.arrow} ${trend.label}</div>
        ${signalHtml}
        <div style="font-size:11px; color:var(--text-muted); margin-top:4px;">${priceStr}</div>`;

      container.appendChild(card);
    });
  });
}

// ── 信号列表 ─────────────────────────────────────────
function renderSignalList(reports) {
  const container = document.getElementById('signal-list');

  // 收集所有有信号的指数，按"买信号优先 + 三级共振"排序
  const withSignals = reports
    .filter(r => r.data_available && (r.has_buy_signal || r.has_sell_signal))
    .sort((a, b) => {
      // 三级共振最优先
      if (a.three_level_aligned && !b.three_level_aligned) return -1;
      if (!a.three_level_aligned && b.three_level_aligned) return 1;
      // 买信号优先于卖信号
      if (a.has_buy_signal && !b.has_buy_signal) return -1;
      if (!a.has_buy_signal && b.has_buy_signal) return 1;
      return 0;
    });

  if (withSignals.length === 0) {
    container.innerHTML = '<div class="empty-state">当前无明确信号</div>';
    return;
  }

  container.innerHTML = '';
  withSignals.forEach((r, idx) => {
    // 收集信号
    const sigs = [];
    [r.daily_latest_signal, r.f30_latest_signal, r.f15_latest_signal].forEach(s => {
      if (s !== '无') sigs.push(s);
    });
    const mainSignal = sigs[0] || '';
    const isBuy = mainSignal.includes('买');
    const direction = r.is_bullish ? '偏多' : '偏空';
    const dirCls = r.is_bullish ? 'bullish' : 'bearish';

    const row = document.createElement('div');
    row.className = 'signal-row';
    row.onclick = () => navigateToChart(r.name);
    row.innerHTML = `
      <span class="signal-rank">${idx + 1}</span>
      <div class="signal-symbol">
        <div class="signal-symbol-name">${r.name}</div>
        <div class="signal-symbol-code">${r.symbol}</div>
      </div>
      <span class="signal-type ${isBuy ? 'buy' : 'sell'}">${isBuy ? '\u25B2' : '\u25BC'} ${mainSignal}</span>
      <span class="signal-direction ${dirCls}">${direction}</span>
      <span class="signal-arrow">\u203A</span>`;

    container.appendChild(row);
  });
}

// ── 加载首页 ─────────────────────────────────────────
async function loadDashboard() {
  try {
    const [ctx, reports] = await Promise.all([
      apiFetch('/api/index/context'),
      apiFetch('/api/index/reports'),
    ]);
    renderBanner(ctx);
    renderCards(reports);
    renderSignalList(reports);
  } catch (err) {
    console.error('Dashboard load failed:', err);
    document.getElementById('banner-direction').textContent =
      '数据加载失败: ' + err.message;
  }
}

window.loadDashboard = loadDashboard;
