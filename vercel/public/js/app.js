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
}

function navigateToChart(indexName, freq) {
  switchPage('chart');
  if (window.loadChart) {
    window.loadChart(indexName, freq || 'daily');
  }
}

// ── API 工具 ─────────────────────────────────────────
async function apiFetch(path) {
  const res = await fetch(API_BASE + path);
  if (!res.ok) {
    const text = await res.text();
    throw new Error(`API ${path}: ${res.status} ${text}`);
  }
  return res.json();
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

  // 刷新按钮
  document.getElementById('refresh-btn').addEventListener('click', () => {
    if (window.loadDashboard) window.loadDashboard();
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

  // 加载首页数据
  if (window.loadDashboard) window.loadDashboard();
});
