/* ═══════════════════════════════════════════════════
   🐲 隆小侠 — 墨龙设计系统 · 核心路由 + 主题 + 工具
   ═══════════════════════════════════════════════════ */

// ── 页面路由 ───────────────────────────────────────
const _pageCallbacks = {};

function switchPage(name) {
  // 切换页面
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.nav-item[data-page]').forEach(n => n.classList.remove('active'));

  const page = document.getElementById(`page-${name}`);
  if (page) {
    page.classList.add('active');
    // 重新触发 stagger 动画
    page.querySelectorAll('.stagger-in').forEach(container => {
      container.classList.remove('stagger-in');
      void container.offsetWidth; // force reflow
      container.classList.add('stagger-in');
    });
  }

  const nav = document.querySelector(`.nav-item[data-page="${name}"]`);
  if (nav) nav.classList.add('active');

  if (_pageCallbacks[name]) _pageCallbacks[name]();
}

function onPageLoad(name, fn) {
  _pageCallbacks[name] = fn;
}

// ── 主题 ───────────────────────────────────────────
const THEMES = ['bronze', 'cinnabar'];

function initTheme() {
  let saved = localStorage.getItem('web2-theme');
  // 迁移旧主题名
  if (saved === 'tradingview') saved = 'bronze';
  if (saved === 'anthropic') saved = 'cinnabar';
  if (!THEMES.includes(saved)) saved = 'bronze';
  document.documentElement.setAttribute('data-theme', saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'bronze' ? 'cinnabar' : 'bronze';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('web2-theme', next);
}

// ── API ────────────────────────────────────────────
async function apiFetch(path) {
  try {
    const resp = await fetch(path);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return await resp.json();
  } catch (e) {
    showToast(`请求失败: ${e.message}`);
    throw e;
  }
}

// ── CSS 变量 ────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── Toast（滑入动画）──────────────────────────────
let _toastTimer = null;
function showToast(msg, duration = 3000) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.remove('hide');
  el.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => {
    el.classList.remove('show');
    el.classList.add('hide');
    setTimeout(() => el.classList.remove('hide'), 300);
  }, duration);
}

// ── 动画工具 ──────────────────────────────────────
/**
 * KPI 数字从 0 滚动到目标值
 * @param {HTMLElement} el - 目标元素
 * @param {number} end - 目标值
 * @param {number} duration - 持续时间 ms
 * @param {string} suffix - 后缀 (如 '%')
 * @param {number} decimals - 小数位
 */
function animateValue(el, end, duration = 600, suffix = '', decimals = 0) {
  const start = 0;
  const startTime = performance.now();
  const range = end - start;

  function update(now) {
    const elapsed = now - startTime;
    const progress = Math.min(elapsed / duration, 1);
    // ease-out cubic
    const eased = 1 - Math.pow(1 - progress, 3);
    const current = start + range * eased;
    el.textContent = current.toFixed(decimals) + suffix;
    if (progress < 1) requestAnimationFrame(update);
  }
  requestAnimationFrame(update);
}

/**
 * 手动触发容器内子元素的 stagger 入场动画
 */
function triggerStagger(container) {
  if (!container) return;
  container.classList.remove('stagger-in');
  void container.offsetWidth;
  container.classList.add('stagger-in');
}

// ── 初始化 ─────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  // Nav-rail 导航
  document.querySelectorAll('.nav-item[data-page]').forEach(item => {
    item.addEventListener('click', () => switchPage(item.dataset.page));
  });

  // 主题切换
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);

  // 默认页面
  switchPage('cluster');
});
