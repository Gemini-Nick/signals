#!/usr/bin/env node
/**
 * 本地开发服务器 — mock API + 静态文件
 * 用于在无 Upstash Redis 的环境中验证前端。
 *
 * 用法：node vercel/dev-server.js [port]
 */
const http = require('http');
const fs = require('fs');
const path = require('path');

const PORT = parseInt(process.argv[2] || '3000', 10);
const PUBLIC = path.join(__dirname, 'public');

// ── Mock 数据 ────────────────────────────────────────

const now = Math.floor(Date.now() / 1000);

// 生成 mock K线数据（60 根）
function mockOHLCV(basePrice, days) {
  const bars = [];
  let price = basePrice;
  for (let i = 0; i < days; i++) {
    const time = now - (days - i) * 86400;
    const change = (Math.random() - 0.48) * basePrice * 0.02;
    const open = price;
    const close = price + change;
    const high = Math.max(open, close) + Math.random() * basePrice * 0.005;
    const low = Math.min(open, close) - Math.random() * basePrice * 0.005;
    const volume = Math.floor(1e8 + Math.random() * 5e8);
    bars.push({ time, open: +open.toFixed(2), high: +high.toFixed(2), low: +low.toFixed(2), close: +close.toFixed(2), volume });
    price = close;
  }
  return bars;
}

// 生成 mock 笔数据
function mockBiList(ohlcv) {
  const biList = [];
  for (let i = 0; i < ohlcv.length - 10; i += 5) {
    const slice = ohlcv.slice(i, i + 5);
    const highs = slice.map(b => b.high);
    const lows = slice.map(b => b.low);
    biList.push({
      direction: biList.length % 2 === 0 ? 'up' : 'down',
      sdt: slice[0].time,
      edt: slice[slice.length - 1].time,
      high: Math.max(...highs),
      low: Math.min(...lows),
    });
  }
  return biList;
}

const INDICES = {
  '上证50':   { symbol: '000016.SH', base: 2600 },
  '沪深300':  { symbol: '000300.SH', base: 3800 },
  '创业板指': { symbol: '399006.SZ', base: 2100 },
  '科创50':   { symbol: '000688.SH', base: 980 },
  '超大盘':   { symbol: '000043.SH', base: 6500 },
  '中证500':  { symbol: '000905.SH', base: 5200 },
  '中证1000': { symbol: '000852.SH', base: 6800 },
  '恒生科技': { symbol: 'HSTECH.HK', base: 4200 },
  '标普500':  { symbol: 'SPY',       base: 540 },
  '纳斯达克': { symbol: 'QQQ',       base: 480 },
  '道琼斯':   { symbol: 'DIA',       base: 420 },
};

const TRENDS = ['上涨趋势', '中枢震荡', '下跌趋势'];
const SIGNALS_POOL = ['一买', '二买', '三买', '背驰买', '无'];

function pickRandom(arr) { return arr[Math.floor(Math.random() * arr.length)]; }

// 生成 reports（每次请求一致，用固定 seed）
const mockReports = Object.entries(INDICES).map(([name, info]) => {
  const trend = pickRandom(TRENDS);
  const dailySig = pickRandom(SIGNALS_POOL);
  const f30Sig = pickRandom(SIGNALS_POOL);
  const f15Sig = pickRandom(SIGNALS_POOL);
  const hasBuy = [dailySig, f30Sig, f15Sig].some(s => s.includes('买'));
  return {
    name,
    symbol: info.symbol,
    data_available: true,
    daily_trend: trend,
    f30_latest_signal: f30Sig,
    f15_latest_signal: f15Sig,
    daily_latest_signal: dailySig,
    latest_price: info.base + (Math.random() - 0.5) * info.base * 0.02,
    has_buy_signal: hasBuy,
    has_sell_signal: false,
    three_level_aligned: hasBuy && Math.random() > 0.7,
    is_bullish: trend === '上涨趋势',
  };
});

const mockContext = {
  overall_direction: '偏多',
  sentiment_phase: '震荡上行',
  position_suggestion: '[mock] 当前处于模拟环境，数据仅供验证前端渲染',
  summary: '11只指数整体偏多，建议逢低布局',
};

const mockStatus = {
  ready: true,
  running: false,
  last_update: now,
  error: '',
  index_count: 11,
  signal_count: mockReports.filter(r => r.has_buy_signal).length,
};

// 预生成 chart 数据（每个指数 × 3 频率）
const chartCache = {};
for (const [name, info] of Object.entries(INDICES)) {
  for (const freq of ['daily', '30min', '15min']) {
    const days = freq === 'daily' ? 120 : 60;
    const ohlcv = mockOHLCV(info.base, days);
    const biList = mockBiList(ohlcv);
    const signals = biList
      .filter((_, i) => i % 3 === 0)
      .map(bi => ({
        dt: bi.edt,
        type: pickRandom(['一买', '二买', '三买']),
        confidence: 0.6 + Math.random() * 0.3,
        price: bi.direction === 'up' ? bi.high : bi.low,
        details: '[mock] 模拟信号',
        freq: freq === 'daily' ? '日线' : freq === '30min' ? '30分钟' : '15分钟',
      }));

    chartCache[`${name}:${freq}`] = {
      ohlcv,
      bi_list: biList,
      fx_list: [],
      zhongshu: [],
      signals,
      meta: { name, symbol: info.symbol, freq },
    };
  }
}

// ── 路由 ──────────────────────────────────────────────

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.png': 'image/png',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
};

function jsonResponse(res, data, status = 200) {
  res.writeHead(status, {
    'Content-Type': 'application/json; charset=utf-8',
    'Access-Control-Allow-Origin': '*',
  });
  res.end(JSON.stringify(data));
}

function handleAPI(req, res) {
  const url = new URL(req.url, `http://localhost:${PORT}`);
  const p = url.pathname;

  if (p === '/api/index/context') {
    return jsonResponse(res, mockContext);
  }
  if (p === '/api/index/reports') {
    return jsonResponse(res, mockReports);
  }
  if (p === '/api/index/status') {
    return jsonResponse(res, mockStatus);
  }
  if (p === '/api/screener/results') {
    return jsonResponse(res, []);
  }

  // /api/chart/{symbol}?freq=daily
  const chartMatch = p.match(/^\/api\/chart\/(.+)$/);
  if (chartMatch) {
    const symbol = decodeURIComponent(chartMatch[1]);
    const freq = url.searchParams.get('freq') || 'daily';
    if (!['daily', '30min', '15min'].includes(freq)) {
      return jsonResponse(res, { detail: 'freq 必须是 daily/30min/15min' }, 400);
    }
    const key = `${symbol}:${freq}`;
    const data = chartCache[key];
    if (!data) {
      return jsonResponse(res, { detail: `未找到: ${symbol} ${freq}` }, 404);
    }
    return jsonResponse(res, data);
  }

  return jsonResponse(res, { detail: 'Not found' }, 404);
}

function handleStatic(req, res) {
  let filePath = path.join(PUBLIC, req.url === '/' ? 'index.html' : req.url);

  // SPA fallback
  if (!fs.existsSync(filePath) && !req.url.startsWith('/api/')) {
    filePath = path.join(PUBLIC, 'index.html');
  }

  if (!fs.existsSync(filePath)) {
    res.writeHead(404);
    res.end('Not found');
    return;
  }

  const ext = path.extname(filePath);
  const mime = MIME[ext] || 'application/octet-stream';
  const content = fs.readFileSync(filePath);
  res.writeHead(200, { 'Content-Type': mime });
  res.end(content);
}

// ── 服务器 ────────────────────────────────────────────

const server = http.createServer((req, res) => {
  if (req.url.startsWith('/api/')) {
    handleAPI(req, res);
  } else {
    handleStatic(req, res);
  }
});

server.listen(PORT, '0.0.0.0', () => {
  console.log(`\n  🐲 隆小侠 Dev Server (mock data)`);
  console.log(`  http://localhost:${PORT}\n`);
  console.log(`  API endpoints:`);
  console.log(`    GET /api/index/context`);
  console.log(`    GET /api/index/reports`);
  console.log(`    GET /api/index/status`);
  console.log(`    GET /api/chart/{symbol}?freq=daily|30min|15min`);
  console.log(`    GET /api/screener/results\n`);
  console.log(`  Mock data: ${Object.keys(INDICES).length} indices × 3 freqs = ${Object.keys(chartCache).length} charts\n`);
});
