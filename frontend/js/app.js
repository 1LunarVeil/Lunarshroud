// LunarShroud 主应用入口
// 负责：路由切换、全局 Toast/Modal/Confirm、主题/语言切换、
//       实例状态中心（WS 推送 + 轮询兜底）、初始化所有页面模块

import Api from './api.js?v=1';
import { createSingleFlight } from './runtime.mjs';
import { getLocale, setLocale, t, LOCALE_NAMES } from './i18n.mjs';
import { open as openModelPicker } from './model_picker.mjs';

// 导入所有页面模块
import * as dashboard from './dashboard.js?v=1';
import * as models from './models.js?v=1';
import * as chat from './chat.js?v=1';
import * as paramEditor from './param_editor.js?v=1';
import * as presets from './presets.js?v=1';
import * as logs from './logs.js?v=1';
import * as monitor from './monitor.js?v=1';
import * as history from './history.js?v=1';
import * as help from './help.js?v=1';
import * as settings from './settings.js?v=1';

// 页面注册表（顺序 = 侧边栏顺序）
const PAGES = [
  dashboard,
  models,
  chat,
  paramEditor,
  presets,
  logs,
  monitor,
  history,
  help,
  settings,
];

// 当前激活页面
let activePage = null;
const pageInstances = new Map(); // id -> { module, root, activated }
let appCtx = null; // 见 buildAppContext()：页面 init() 与 renderPanel() 共用

// ========== 全局 Toast ==========

const toastContainer = () => document.getElementById('toast-container');

export function showToast(message, type = 'info', duration = 3000) {
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  const icons = {
    success: '✓',
    error: '✕',
    warning: '!',
    info: 'i',
  };
  el.innerHTML = `<span class="toast-icon">${icons[type] || 'i'}</span><span class="toast-msg"></span>`;
  el.querySelector('.toast-msg').textContent = message;
  toastContainer().appendChild(el);
  setTimeout(() => {
    el.classList.add('fade-out');
    setTimeout(() => el.remove(), 250);
  }, duration);
  // 同时输出到控制台便于调试
  if (type === 'error') console.error('[Toast]', message);
  else console.log('[Toast]', message);
}

// ========== 全局 Modal ==========

// 关闭回调：某些调用方（启动前检查）需要知道用户是"点了按钮"还是"按了 X /
// 点了遮罩"关闭的，否则 Promise 永远不 resolve。showModal 的第四个参数是可选的，
// 既有三参数调用完全不受影响。
let _modalCloseHook = null;

export function showModal(title, contentHtml, actions = [], options = {}) {
  closeModal();
  _modalCloseHook = typeof options.onClose === 'function' ? options.onClose : null;
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  overlay.id = 'app-modal-overlay';

  const actionsHtml = actions
    .map(
      (a, i) => `<button class="btn ${a.class || 'btn-secondary'}" data-idx="${i}">${a.label}</button>`
    )
    .join('');

  overlay.innerHTML = `
    <div class="modal ${modalSizeClass(actions)}">
      <div class="modal-header">
        <div class="modal-title"></div>
        <button class="modal-close" data-close>&times;</button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-footer">${actionsHtml}</div>
    </div>
  `;
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  if (typeof contentHtml === 'string') {
    body.innerHTML = contentHtml;
  } else if (contentHtml instanceof Node) {
    body.appendChild(contentHtml);
  }

  // 关闭事件
  overlay.querySelector('[data-close]').addEventListener('click', closeModal);
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeModal();
  });

  // 动作按钮
  overlay.querySelectorAll('[data-idx]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const idx = parseInt(btn.dataset.idx, 10);
      const action = actions[idx];
      if (!action) return;
      if (typeof action.onClick === 'function') {
        const ret = action.onClick(overlay, body);
        // 若 onClick 返回 false 则不关闭
        if (ret === false) return;
      }
      if (action.keepOpen) return;
      closeModal();
    });
  });

  document.body.appendChild(overlay);
  return overlay;
}

function modalSizeClass(actions) {
  if (actions.some((a) => a.size === 'lg')) return 'modal-lg';
  if (actions.some((a) => a.size === 'sm')) return 'modal-sm';
  return '';
}

export function closeModal() {
  const hook = _modalCloseHook;
  _modalCloseHook = null;
  if (hook) {
    try {
      hook();
    } catch (e) {
      console.error('[modal onClose]', e);
    }
  }
  const existing = document.getElementById('app-modal-overlay');
  if (existing) existing.remove();
  // 浮层整体销毁后，模块作用域上的防抖计时器必须有机会自清 ——
  // 否则关窗后仍会触发一次请求（审计 R6 的 param_editor 条目）。
  document.dispatchEvent(new CustomEvent('modal-closed'));
}

// ========== Confirm（基于 Modal） ==========

export function showConfirm(message, title) {
  return new Promise((resolve) => {
    showModal(title || t('app.confirmTitle'), `<div style="font-size:14px;line-height:1.6;">${escapeHtml(message)}</div>`, [
      {
        label: t('app.cancel'),
        class: 'btn-secondary',
        onClick: () => resolve(false),
      },
      {
        label: t('app.ok'),
        class: 'btn-primary',
        onClick: () => resolve(true),
      },
    ]);
  });
}

// ========== 工具函数 ==========

export function escapeHtml(str) {
  if (str === null || str === undefined) return '';
  return String(str)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

export function formatDuration(seconds) {
  // 用 == null 而不是 !seconds：0 是合法的"刚启动"，不该显示成 '-'
  if (seconds == null || seconds === '' || Number.isNaN(Number(seconds)) || Number(seconds) < 0) return '-';
  const s = Math.floor(seconds);
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const sec = s % 60;
  if (h > 0) return `${h}h${m}m`;
  if (m > 0) return `${m}m${sec}s`;
  return `${sec}s`;
}

export function formatTime(iso) {
  if (!iso) return '-';
  try {
    const d = new Date(iso);
    return d.toLocaleString(getLocale() === 'zh-CN' ? 'zh-CN' : 'en-US', { hour12: false });
  } catch {
    return iso;
  }
}

export function formatBytes(n) {
  if (!n && n !== 0) return '-';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  let v = n;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i++;
  }
  return `${v.toFixed(i === 0 ? 0 : 2)} ${units[i]}`;
}

export function basename(path) {
  if (!path) return '';
  return path.replace(/\\/g, '/').split('/').pop();
}

const STATUS_KEYS = {
  running: 'badge.running',
  starting: 'badge.starting',
  stopped: 'badge.stopped',
  failed: 'badge.failed',
  stopping: 'badge.stopping',
  idle: 'badge.idle',
  success: 'badge.success',
  // 上一进程遗留的未终止记录（窗口关闭时后端被 Job Object 直接终止）：
  // 它既不是"运行中"也不是"失败"，必须有自己的状态，见 history_store 的启动收口。
  interrupted: 'badge.interrupted',
};

export function statusBadge(status) {
  const key = STATUS_KEYS[status];
  const label = key ? t(key) : (status || '-');
  // class 里的 status 也要过白名单：它同样进 innerHTML。当前 status 来自后端
  // InstanceStatus 枚举（6 个固定值），不可外部控制，但全仓库其余 HTML sink
  // 都转义，只有这里例外 —— 用白名单把例外消掉，将来状态来源变化也不会变成注入点。
  const known = key ? status : '';
  const cls = known || 'idle';
  return `<span class="badge badge-${cls}">${escapeHtml(label)}</span>`;
}

// 复制到剪贴板
export async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    showToast(t('app.copied'), 'success', 1500);
  } catch (e) {
    // 降级方案：使用临时 textarea
    const ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed';
    ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    try {
      document.execCommand('copy');
      showToast(t('app.copied'), 'success', 1500);
    } catch {
      showToast(t('app.copyFail'), 'error');
    }
    ta.remove();
  }
}

// ========== 主题切换 ==========

export function getTheme() {
  try {
    return localStorage.getItem('lunarshroud_theme') || 'dark';
  } catch {
    // 存储不可用（隐私模式/策略限制）时退回默认，不能因此白屏（审计 R6）
    return 'dark';
  }
}

export function setTheme(theme) {
  document.body.classList.remove('theme-dark', 'theme-light');
  document.body.classList.add(theme === 'dark' ? 'theme-dark' : 'theme-light');
  try {
    localStorage.setItem('lunarshroud_theme', theme);
  } catch {
    // 持久化失败只影响下次启动的初始主题，本次会话照常生效
  }
  // 通知 monitor 等需要重绘的页面
  document.dispatchEvent(new CustomEvent('theme-changed', { detail: { theme } }));
}

function toggleTheme() {
  const next = getTheme() === 'dark' ? 'light' : 'dark';
  setTheme(next);
  showToast(t(next === 'dark' ? 'app.themeDark' : 'app.themeLight'), 'info', 1500);
}

// ========== 语言切换 ==========

function cycleLocale() {
  const order = ['zh-CN', 'en-US'];
  const next = order[(order.indexOf(getLocale()) + 1) % order.length];
  if (setLocale(next)) {
    showToast(t('app.langSwitched'), 'info', 600);
    setTimeout(() => location.reload(), 500);
  }
}

// ========== 实例状态中心（WS 推送 + 轮询兜底） ==========
// 顶栏、仪表盘、Chat、日志页共用一份数据源：
// 正常时由 /ws/instances 在每次状态变化时推送；连接中断时退回 5 秒轮询。

const instanceListeners = new Set();
let instancesCache = [];

function emitInstances(list) {
  instancesCache = Array.isArray(list) ? list : [];
  renderHeaderStatus(instancesCache);
  for (const cb of [...instanceListeners]) {
    try { cb(instancesCache); } catch (e) { console.error('[instances] listener failed', e); }
  }
}

/**
 * 订阅实例列表更新；立即以当前缓存回调一次。
 * @returns {Function} 取消订阅函数
 */
export function subscribeInstances(cb) {
  instanceListeners.add(cb);
  cb(instancesCache);
  return () => instanceListeners.delete(cb);
}

async function pollInstances() {
  try {
    const list = await Api.listInstances();
    emitInstances(list);
  } catch (e) {
    // 后端失联：顶栏由 renderHeaderStatus 的 catch 分支呈现
    renderHeaderStatus(null, e);
  }
}

const pollInstancesOnce = createSingleFlight(pollInstances);

let instancesPollTimer = null;

function startInstancesPolling() {
  if (instancesPollTimer) return;
  pollInstancesOnce();
  instancesPollTimer = setInterval(pollInstancesOnce, 5000);
}

function stopInstancesPolling() {
  if (instancesPollTimer) {
    clearInterval(instancesPollTimer);
    instancesPollTimer = null;
  }
}

function startInstanceFeed() {
  Api.watchInstances({
    onMessage: (msg) => {
      if (msg && msg.type === 'instances') emitInstances(msg.instances);
    },
    onStatus: (state) => {
      if (state === 'connected') {
        stopInstancesPolling();
      } else if (state === 'reconnecting' || state === 'closed') {
        startInstancesPolling();
      }
    },
  });
  // WebSocket 建立连接前的首次数据
  pollInstancesOnce();
}

// ========== 顶栏状态指示器 ==========

function renderHeaderStatus(instances) {
  const dot = document.getElementById('header-status-dot');
  const text = document.getElementById('header-status-text');
  if (!dot || !text) return;
  if (!Array.isArray(instances)) {
    dot.className = 'status-dot offline';
    text.textContent = t('app.statusOffline');
    return;
  }
  const running = instances.filter((i) => i.status === 'running');
  const starting = instances.filter((i) => i.status === 'starting');
  const failed = instances.filter((i) => i.status === 'failed');

  if (running.length > 0) {
    dot.className = 'status-dot running';
    text.textContent = t('app.statusRunning', { count: running.length });
  } else if (starting.length > 0) {
    dot.className = 'status-dot starting';
    text.textContent = t('app.statusStarting', { count: starting.length });
  } else if (failed.length > 0) {
    dot.className = 'status-dot failed';
    text.textContent = t('app.statusFailed', { count: failed.length });
  } else {
    dot.className = 'status-dot';
    text.textContent = t('app.statusNone');
  }
}

// ========== 上下文面板（二级左栏） ==========
// 页面模块可选导出 renderPanel(el, ctx) 来提供面板内容：
//   - 未导出（或用户收起）→ 整列隐藏，主内容自动占满
//   - 导出后由 navigateTo() 在每次切页时重新渲染
// 面板与页面共用同一个 ctx 对象，其中 ctx.refreshPanel() 可让页面在数据变化后
// 主动重绘面板（例如参数 schema 异步加载完成后）。

const PANEL_STATE_KEY = 'lunarshroud_panel_collapsed';

function getPanelCollapsed() {
  try {
    return localStorage.getItem(PANEL_STATE_KEY) === '1';
  } catch {
    return false;
  }
}

function pageHasPanel(pageId) {
  const entry = pageId ? pageInstances.get(pageId) : null;
  return !!(entry && typeof entry.module.renderPanel === 'function');
}

function renderContextPanel(pageId) {
  const panel = document.getElementById('context-panel');
  const body = document.getElementById('context-panel-body');
  const title = document.getElementById('context-panel-title');
  if (!panel || !body) return;

  const entry = pageId ? pageInstances.get(pageId) : null;
  const usable = pageHasPanel(pageId) && !getPanelCollapsed();

  if (!usable) {
    panel.hidden = true;
    body.replaceChildren();
  } else {
    panel.hidden = false;
    if (title) title.textContent = t('nav.' + pageId);
    body.replaceChildren();
    try {
      entry.module.renderPanel(body, appCtx);
    } catch (e) {
      console.error(`[renderPanel ${pageId} failed]`, e);
      body.replaceChildren();
    }
  }
  syncPanelToggles();
}

function syncPanelToggles() {
  const panel = document.getElementById('context-panel');
  const toggle = document.getElementById('btn-panel-toggle');
  const collapse = document.getElementById('btn-panel-collapse');
  const visible = !!panel && !panel.hidden;
  if (toggle) {
    // 当前页没有面板时置灰而不是隐藏：隐藏会让顶栏按钮左右跳动
    const available = pageHasPanel(activePage);
    toggle.disabled = !available;
    toggle.setAttribute('aria-pressed', String(available && visible));
    toggle.title = available ? t('panel.toggle') : t('panel.unavailable');
  }
  if (collapse) {
    collapse.title = t('panel.collapse');
  }
}

function setPanelCollapsed(collapsed) {
  try {
    localStorage.setItem(PANEL_STATE_KEY, collapsed ? '1' : '0');
  } catch {
    // 忽略持久化失败（隐私模式等）
  }
  renderContextPanel(activePage);
}

function togglePanel() {
  setPanelCollapsed(!getPanelCollapsed());
}

// ========== 路由 ==========

function renderSidebar() {
  const icons = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    models: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    param_editor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1 0 2.83 2 2 0 0 1-2.83 0l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-2 2 2 2 0 0 1-2-2v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83 0 2 2 0 0 1 0-2.83l.06-.06a1.65 1.65 0 0 0 .33-1.82 1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1-2-2 2 2 0 0 1 2-2h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 0-2.83 2 2 0 0 1 2.83 0l.06.06a1.65 1.65 0 0 0 1.82.33H9a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 2-2 2 2 0 0 1 2 2v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 0 2 2 0 0 1 0 2.83l-.06.06a1.65 1.65 0 0 0-.33 1.82V9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 2 2 2 2 0 0 1-2 2h-.09a1.65 1.65 0 0 0-1.51 1z"/></svg>',
    presets: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
    logs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
    monitor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    history: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    help: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>',
  };
  const sidebar = document.getElementById('sidebar');
  sidebar.innerHTML = PAGES.map(
    (p) => `
      <div class="sidebar-item" data-page="${p.id}" title="${escapeHtml(t('nav.' + p.id))}">
        <span class="icon">${icons[p.id] || ''}</span>
        <span class="label">${escapeHtml(t('nav.' + p.id))}</span>
      </div>
    `
  ).join('');
  sidebar.querySelectorAll('.sidebar-item').forEach((el) => {
    el.addEventListener('click', () => {
      const id = el.dataset.page;
      navigateTo(id);
    });
  });
}

function closeMobileSidebar() {
  document.getElementById('sidebar').classList.remove('show');
  document.getElementById('sidebar-backdrop').classList.remove('show');
}

function openMobileSidebar() {
  document.getElementById('sidebar').classList.add('show');
  document.getElementById('sidebar-backdrop').classList.add('show');
}

function toggleMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (sidebar.classList.contains('show')) {
    closeMobileSidebar();
  } else {
    openMobileSidebar();
  }
}

// 「模态型」模块：导出 kind='modal' 的模块不是路由目的地，而是浮层。
// 侧栏点击它时打开浮层，当前路由不变（所以它永远不会被标记为 active）。
function isModalPage(mod) {
  return !!mod && mod.kind === 'modal';
}

function openModalPage(mod) {
  try {
    const ret = mod.open(appCtx);
    if (ret && typeof ret.catch === 'function') {
      ret.catch((e) => console.error(`[open ${mod.id} failed]`, e));
    }
  } catch (e) {
    console.error(`[open ${mod.id} failed]`, e);
  }
}

function navigateTo(pageId) {
  const target = PAGES.find((p) => p.id === pageId);
  if (isModalPage(target)) {
    closeMobileSidebar();
    openModalPage(target);
    return;
  }
  // 隐藏当前页
  if (activePage) {
    const prev = pageInstances.get(activePage);
    if (prev && typeof prev.module.onHide === 'function') {
      try { prev.module.onHide(); } catch (e) { console.error(e); }
    }
    prev.root.classList.remove('active');
  }
  // 切换侧边栏激活
  document.querySelectorAll('.sidebar-item').forEach((el) => {
    el.classList.toggle('active', el.dataset.page === pageId);
  });

  const entry = pageInstances.get(pageId);
  if (!entry) return;
  entry.root.classList.add('active');
  activePage = pageId;
  // 二级左栏内容随页面切换
  renderContextPanel(pageId);
  // 窄屏下点击导航后自动收起侧边栏
  closeMobileSidebar();
  if (typeof entry.module.onShow === 'function') {
    try {
      Promise.resolve(entry.module.onShow()).catch((e) => console.error(e));
    } catch (e) {
      console.error(e);
    }
  }
}

// ========== 初始化 ==========

function initLayout() {
  // 应用顶层结构已在 index.html 中静态定义
  // 这里仅注入主题与事件
  setTheme(getTheme());
  document.documentElement.lang = getLocale();
  // 首屏就给出正确的状态文案，而不是空着等第一次推送（index.html 静态留空）
  const statusText = document.getElementById('header-status-text');
  if (statusText) statusText.textContent = t('app.statusNone');

  // 顶栏按钮
  document.getElementById('btn-theme').addEventListener('click', toggleTheme);
  document.getElementById('btn-about').addEventListener('click', showAbout);
  const langBtn = document.getElementById('btn-lang');
  if (langBtn) {
    langBtn.title = `${LOCALE_NAMES[getLocale()]} ⇄ ${LOCALE_NAMES[getLocale() === 'zh-CN' ? 'en-US' : 'zh-CN']}`;
    langBtn.textContent = getLocale() === 'zh-CN' ? 'EN' : '中';
    langBtn.addEventListener('click', cycleLocale);
  }

  // 窄屏侧边栏切换
  const sidebarToggle = document.getElementById('btn-sidebar-toggle');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  if (sidebarToggle) {
    // index.html 静态留空（写死中文会漏翻），文案在这里注入
    const label = t('app.sidebarToggle');
    sidebarToggle.title = label;
    sidebarToggle.setAttribute('aria-label', label);
    sidebarToggle.addEventListener('click', toggleMobileSidebar);
  }
  if (sidebarBackdrop) sidebarBackdrop.addEventListener('click', closeMobileSidebar);

  // 二级左栏：顶栏按钮负责"收起/展开"，面板头部按钮负责"收起"
  const panelToggle = document.getElementById('btn-panel-toggle');
  const panelCollapse = document.getElementById('btn-panel-collapse');
  if (panelToggle) panelToggle.addEventListener('click', togglePanel);
  if (panelCollapse) panelCollapse.addEventListener('click', () => setPanelCollapsed(true));

  // 居中模型选择器：按钮文案在这里注入（index.html 是静态的，写死中文会漏翻）
  const pickerBtn = document.getElementById('btn-model-picker');
  if (pickerBtn) {
    pickerBtn.innerHTML = `${escapeHtml(t('app.pickModel'))} <span class="kbd">Ctrl+L</span>`;
    pickerBtn.title = t('app.pickModel');
    pickerBtn.addEventListener('click', () => openModelPicker(appCtx));
  }

  // 全局 ESC 关闭 Modal 与窄屏侧边栏
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeModal();
      closeMobileSidebar();
      return;
    }
    // Ctrl+L = 打开模型选择器。与 ESC 放在同一处，键盘处理只有一份。
    if ((e.ctrlKey || e.metaKey) && (e.key === 'l' || e.key === 'L')) {
      e.preventDefault(); // 否则会选中浏览器地址栏
      openModelPicker(appCtx);
    }
  });

  // 实例状态中心（WS 推送 + 轮询兜底），顶栏状态指示由它驱动
  startInstanceFeed();
}

function showAbout() {
  showModal(
    t('app.aboutTitle'),
    `
    <div style="font-size:14px;line-height:1.8;">
      <p><strong>LunarShroud</strong> ${escapeHtml(t('app.aboutDesc1'))}</p>
      <p>${escapeHtml(t('app.aboutDesc2'))}</p>
      <p style="color: var(--color-text-secondary); margin-top: 10px; font-size: 12px;">${escapeHtml(t('app.aboutStack'))}</p>
    </div>
    `,
    [{ label: t('app.close'), class: 'btn-primary' }]
  );
}

function initPages() {
  const main = document.getElementById('main-content');
  const ctx = appCtx;
  for (const mod of PAGES) {
    // 模态型模块没有页面容器，由 open() 自建浮层
    if (isModalPage(mod)) continue;
    const root = document.createElement('div');
    root.className = 'page';
    root.id = `page-${mod.id}`;
    main.appendChild(root);
    pageInstances.set(mod.id, { module: mod, root, activated: false });
    try {
      mod.init(root, ctx);
    } catch (e) {
      console.error(`[init page ${mod.id} failed]`, e);
      root.innerHTML = `<div class="empty-state"><div class="empty-icon">⚠️</div>${escapeHtml(t('app.pageInitFail'))}: ${escapeHtml(
        e.message
      )}</div>`;
    }
  }
}

function updateDebugBanner() {
  const banner = document.getElementById('debug-banner');
  if (!banner) return;
  const script = document.querySelector('script[src*="/js/app.js"]');
  let v = '';
  if (script) {
    try {
      const url = new URL(script.src, location.href);
      v = url.searchParams.get('v') || '';
    } catch {
      // 忽略解析失败
    }
  }
  banner.textContent = v ? `DEBUG v=${v} loaded` : 'DEBUG loaded';
}

// 页面模块上下文：页面 init() 与 renderPanel() 共用同一个对象
function buildAppContext() {
  return {
    Api,
    showToast,
    showModal,
    closeModal,
    showConfirm,
    escapeHtml,
    formatDuration,
    formatTime,
    formatBytes,
    basename,
    statusBadge,
    copyToClipboard,
    setTheme,
    getTheme,
    subscribeInstances,
    t,
    // 页面间跳转。以前页面模块靠 document.querySelector('.sidebar-item[data-page=…]').click()
    // 模拟点击，既依赖 DOM 结构又会被复制到多处；统一走这里。
    navigate: navigateTo,
  };
}

// 启动
function boot() {
  updateDebugBanner();
  appCtx = buildAppContext();
  // 让页面能在自己的数据变化后重绘二级面板
  appCtx.refreshPanel = () => renderContextPanel(activePage);
  initLayout();
  renderSidebar();
  initPages();
  navigateTo('dashboard');
  console.log('[LunarShroud] frontend ready');
}

// DOM 就绪后启动
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
