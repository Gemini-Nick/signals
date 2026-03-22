/* ═══════════════════════════════════════════════════
   🐲 隆小侠 Web2 — 路由 + 主题 + 工具函数
   ═══════════════════════════════════════════════════ */

// ── 页面路由 ───────────────────────────────────────
const _pageCallbacks = {};

function switchPage(name) {
  document.querySelectorAll('.page').forEach(p => p.classList.remove('active'));
  document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
  const page = document.getElementById(`page-${name}`);
  if (page) page.classList.add('active');
  const tab = document.querySelector(`.tab[data-page="${name}"]`);
  if (tab) tab.classList.add('active');
  if (_pageCallbacks[name]) _pageCallbacks[name]();
}

function onPageLoad(name, fn) {
  _pageCallbacks[name] = fn;
}

// ── 主题 ───────────────────────────────────────────
function initTheme() {
  const saved = localStorage.getItem('web2-theme') || 'tradingview';
  document.documentElement.setAttribute('data-theme', saved);
}

function toggleTheme() {
  const current = document.documentElement.getAttribute('data-theme');
  const next = current === 'tradingview' ? 'anthropic' : 'tradingview';
  document.documentElement.setAttribute('data-theme', next);
  localStorage.setItem('web2-theme', next);
}

// ── API ────────────────────────────────────────────
async function apiFetch(path, timeoutMs = 30000) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const resp = await fetch(path, { signal: controller.signal });
    clearTimeout(timer);
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}));
      throw new Error(err.error || `HTTP ${resp.status}`);
    }
    return await resp.json();
  } catch (e) {
    clearTimeout(timer);
    if (e.name === 'AbortError') {
      showToast('请求超时，请重试');
      throw new Error('请求超时');
    }
    showToast(`请求失败: ${e.message}`);
    throw e;
  }
}

// ── CSS 变量 ────────────────────────────────────────
function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

// ── Toast ──────────────────────────────────────────
let _toastTimer = null;
function showToast(msg, duration = 3000) {
  const el = document.getElementById('toast');
  el.textContent = msg;
  el.classList.add('show');
  if (_toastTimer) clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => el.classList.remove('show'), duration);
}

// ── 初始化 ─────────────────────────────────────────
document.addEventListener('DOMContentLoaded', () => {
  initTheme();

  // Tab 点击
  document.querySelectorAll('.tab').forEach(tab => {
    tab.addEventListener('click', () => switchPage(tab.dataset.page));
  });

  // 主题切换
  document.getElementById('theme-toggle')?.addEventListener('click', toggleTheme);

  // 默认页面
  switchPage('cluster');
});
