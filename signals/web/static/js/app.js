/**
 * 隆小侠 LONG CLAW — 应用核心
 * 路由 / 主题切换 / 全局工具
 */

// ── 配置（C→A 兼容：Electron 可通过 preload 注入覆盖）──
const API_BASE = window.API_BASE || '';
const WS_URL = window.WS_URL || 'ws://' + location.host + '/ws';

// ── 主题切换 ──────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('lc-theme') || 'tradingview';
  document.documentElement.dataset.theme = saved;
}

function toggleTheme() {
  const current = document.documentElement.dataset.theme;
  const next = current === 'anthropic' ? 'tradingview' : 'anthropic';
  document.documentElement.dataset.theme = next;
  localStorage.setItem('lc-theme', next);
  // 通知图表重新应用颜色
  if (window.chartInstance && window.chartInstance.applyTheme) {
    window.chartInstance.applyTheme();
  }
}

// ── 页面路由 ──────────────────────────────────────────
function switchPage(pageName) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-tab').forEach(t => t.classList.remove('active'));
  const page = document.getElementById('page-' + pageName);
  if (page) page.classList.add('active');
  const tab = document.querySelector(`.nav-tab[data-page="${pageName}"]`);
  if (tab) tab.classList.add('active');

  // 历史对照页：首次切入时加载缓存
  if (pageName === 'analog' && window.loadAnalogPage) {
    window.loadAnalogPage();
  }
  // 盘后复盘页
  if (pageName === 'review' && window.loadReviewPage) {
    window.loadReviewPage();
  }
  // 回测页
  if (pageName === 'backtest' && window.loadBacktestPage) {
    window.loadBacktestPage();
  }
}

function navigateToChart(indexName, freq) {
  switchPage('chart');
  if (window.loadChart) {
    window.loadChart(indexName, freq || 'daily');
  }
}

function navigateToIndustryChart(industryName) {
  switchPage('chart');
  if (window.loadIndustryChart) {
    window.loadIndustryChart(industryName);
  }
}

// ── Toast 通知 ──────────────────────────────────────
function showToast(msg, duration) {
  let toast = document.getElementById('toast');
  if (!toast) {
    toast = document.createElement('div');
    toast.id = 'toast';
    toast.className = 'toast';
    document.body.appendChild(toast);
  }
  toast.textContent = msg;
  toast.classList.add('show');
  setTimeout(() => toast.classList.remove('show'), duration || 2000);
}

// ── API 工具 ─────────────────────────────────────────
async function apiFetch(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) {
    const text = await res.text();
    const msg = `API ${path}: ${res.status}`;
    showToast(msg, 3000);
    throw new Error(`${msg} ${text}`);
  }
  return res.json();
}

// ── 导航工具 ─────────────────────────────────────────
function navigateToStock(symbol) {
  switchPage('stock');
  const input = document.getElementById('stock-input');
  if (input) {
    input.value = symbol;
    if (window.analyzeStock) window.analyzeStock();
  }
}

function navigateToTheme(theme) {
  switchPage('stock');
  const input = document.getElementById('stock-input');
  if (input) {
    input.value = theme;
    if (window.analyzeStock) window.analyzeStock();
  }
}

// ── CSS 变量读取 ─────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── 初始化 ───────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  // 主题切换
  document.getElementById('theme-toggle').addEventListener('click', toggleTheme);

  // Tab 导航
  document.querySelectorAll('.nav-tab').forEach(tab => {
    tab.addEventListener('click', () => switchPage(tab.dataset.page));
  });

  // 刷新按钮 — 触发后端重新分析，然后轮询加载
  document.getElementById('refresh-btn').addEventListener('click', async () => {
    const btn = document.getElementById('refresh-btn');
    btn.disabled = true;
    btn.textContent = '刷新中...';
    try {
      await fetch(API_BASE + '/api/index/refresh', { method: 'POST' });
      // 等待引擎就绪后重新加载
      const poll = setInterval(async () => {
        try {
          const res = await fetch(API_BASE + '/api/index/status');
          const st = await res.json();
          if (st.ready && !st.running) {
            clearInterval(poll);
            btn.disabled = false;
            btn.textContent = '刷新';
            if (window.loadDashboard) window.loadDashboard();
          }
        } catch (_) {}
      }, 2000);
      // 安全超时 3 分钟
      setTimeout(() => {
        clearInterval(poll);
        btn.disabled = false;
        btn.textContent = '刷新';
      }, 180000);
    } catch (e) {
      btn.disabled = false;
      btn.textContent = '刷新';
    }
  });

  // 图表返回按钮
  document.getElementById('chart-back').addEventListener('click', () => {
    switchPage('dashboard');
  });

  // 信号详情折叠
  document.getElementById('signal-details-toggle').addEventListener('click', () => {
    const body = document.getElementById('signal-details-body');
    const arrow = document.getElementById('signal-details-arrow');
    body.classList.toggle('open');
    arrow.innerHTML = body.classList.contains('open') ? '&#9650;' : '&#9660;';
  });

  // 初始化个股分析页
  if (window.initStockPage) window.initStockPage();

  // 加载首页数据
  if (window.loadDashboard) window.loadDashboard();
});
