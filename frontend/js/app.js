// LunarShroud 主应用入口
// 负责：路由切换、全局 Toast/Modal/Confirm、主题/语言切换、
//       实例状态中心（WS 推送 + 轮询兜底）、初始化所有页面模块

import Api from './api.js?v=1';
import { createSingleFlight } from './runtime.mjs';
import { initTooltips } from './tooltip.mjs';
import { getLocale, setLocale, t, LOCALE_NAMES } from './i18n.mjs';
import { open as openModelPicker } from './model_picker.mjs';
import { openCommandPalette } from './command_palette.mjs';
import { escapeHtml } from './html.mjs';
import { closeModal, showConfirm, showModal } from './modal.mjs';

export { escapeHtml };

// 导入所有页面模块
import * as dashboard from './dashboard.js?v=1';
import * as chat from './chat.js?v=1';
import * as workspace from './workspace.js?v=1';
import * as knowledge from './knowledge.js?v=1';
import * as observability from './observability.js?v=1';
import * as paramEditor from './param_editor.js?v=1';
import * as help from './help.js?v=1';
import * as settings from './settings.js?v=1';

// 页面注册表（顺序 = 初始化顺序；侧栏显示顺序由 NAV_GROUPS 决定）
const PAGES = [
  dashboard,
  chat,
  workspace,
  knowledge,
  observability,
  paramEditor,
  help,
  settings,
];

// 侧栏分组：目的地与动作分开；参数编辑器/设置是模态动作，不是路由页面。
const NAV_GROUPS = [
  { key: 'run', labelKey: 'nav.groupRun', pages: ['dashboard', 'chat'] },
  { key: 'resources', labelKey: 'nav.groupResources', pages: ['workspace', 'knowledge', 'observability'] },
  { key: 'tools', labelKey: 'nav.groupTools', pages: ['param_editor', 'help', 'settings'] },
];

// 当前激活页面
let activePage = null;
const pageInstances = new Map(); // id -> { module, root, activated }
let appCtx = null; // 见 buildAppContext()：页面 init() 与 renderPanel() 共用

// ========== 全局 Toast ==========

const toastContainer = () => document.getElementById('toast-container');

export function showToast(message, type = 'info', duration) {
  const container = toastContainer();
  if (!container) return;
  // 最多同时保留 4 条，避免批量错误把屏幕堆满。
  while (container.children.length >= 4) container.firstElementChild.remove();
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.setAttribute('role', type === 'error' ? 'alert' : 'status');
  const icons = {
    success: '✓',
    error: '✕',
    warning: '!',
    info: 'i',
  };
  el.innerHTML = `<span class="toast-icon" aria-hidden="true">${icons[type] || 'i'}</span><span class="toast-msg"></span>`;
  el.querySelector('.toast-msg').textContent = message;
  container.appendChild(el);
  const timeout = duration ?? (type === 'error' ? 7000 : type === 'warning' ? 5000 : 3000);
  let timer = setTimeout(remove, timeout);
  function remove() {
    clearTimeout(timer);
    el.classList.add('fade-out');
    setTimeout(() => el.remove(), 250);
  }
  // 悬停阅读时暂停倒计时，移开后留 1.5s 再消失。
  el.addEventListener('mouseenter', () => clearTimeout(timer));
  el.addEventListener('mouseleave', () => {
    clearTimeout(timer);
    timer = setTimeout(remove, 1500);
  });
  // 同时输出到控制台便于调试
  if (type === 'error') console.error('[Toast]', message);
  else console.log('[Toast]', message);
}

// ========== 工具函数 ==========

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

const THEME_STORAGE_KEY = 'lunarshroud_theme';
const THEME_QUERY = '(prefers-color-scheme: dark)';

const SUN_ICON =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><circle cx="12" cy="12" r="4"/><line x1="12" y1="2" x2="12" y2="4"/><line x1="12" y1="20" x2="12" y2="22"/><line x1="4.93" y1="4.93" x2="6.34" y2="6.34"/><line x1="17.66" y1="17.66" x2="19.07" y2="19.07"/><line x1="2" y1="12" x2="4" y2="12"/><line x1="20" y1="12" x2="22" y2="12"/><line x1="4.93" y1="19.07" x2="6.34" y2="17.66"/><line x1="17.66" y1="6.34" x2="19.07" y2="4.93"/></svg>';
const MOON_ICON =
  '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/></svg>';

export function getThemePreference() {
  try {
    const value = localStorage.getItem(THEME_STORAGE_KEY);
    return value === 'light' || value === 'dark' || value === 'system' ? value : 'system';
  } catch {
    // 存储不可用（隐私模式/策略限制）时退回系统偏好，不能因此白屏
    return 'system';
  }
}

export function resolveTheme(preference) {
  if (preference === 'light' || preference === 'dark') return preference;
  try {
    return window.matchMedia(THEME_QUERY).matches ? 'dark' : 'light';
  } catch {
    return 'dark';
  }
}

/** 解析后的实际主题（light/dark），供需要按主题重绘的页面使用。 */
export function getTheme() {
  return resolveTheme(getThemePreference());
}

function updateThemeButton(resolved) {
  const btn = document.getElementById('btn-theme');
  if (!btn) return;
  const current = t(resolved === 'dark' ? 'settings.themeDark' : 'settings.themeLight');
  const label = t('app.themeToggle', { current });
  btn.title = label;
  btn.setAttribute('aria-label', label);
  btn.innerHTML = `${resolved === 'dark' ? SUN_ICON : MOON_ICON}<span class="btn-label">${escapeHtml(current)}</span>`;
}

export function setTheme(theme) {
  const preference =
    theme === 'light' || theme === 'dark' || theme === 'system' ? theme : 'system';
  const resolved = resolveTheme(preference);
  document.body.classList.remove('theme-dark', 'theme-light');
  document.body.classList.add(resolved === 'dark' ? 'theme-dark' : 'theme-light');
  document.documentElement.style.colorScheme = resolved;
  try {
    localStorage.setItem(THEME_STORAGE_KEY, preference);
  } catch {
    // 持久化失败只影响下次启动的初始主题，本次会话照常生效
  }
  updateThemeButton(resolved);
  // 通知 monitor 等需要重绘的页面
  document.dispatchEvent(
    new CustomEvent('theme-changed', { detail: { theme: resolved, preference } })
  );
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
    setBackendBanner('connected');
  } catch (e) {
    // 后端失联：顶栏由 renderHeaderStatus 的 catch 分支呈现，
    // 但顶栏的小圆点不足以解释"为什么所有按钮都没反应"，所以补一条横幅。
    renderHeaderStatus(null, e);
    setBackendBanner('offline');
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
        setBackendBanner('connected');
      } else if (state === 'reconnecting' || state === 'closed') {
        startInstancesPolling();
        setBackendBanner('reconnecting');
      }
    },
  });
  // WebSocket 建立连接前的首次数据
  pollInstancesOnce();
}

let backendState = 'connected';

function setBackendBanner(state) {
  backendState = state;
  renderSidebarStatus(lastInstances);
  const banner = document.getElementById('backend-banner');
  if (!banner) return;
  if (state === 'connected') {
    banner.hidden = true;
    banner.textContent = '';
    return;
  }
  banner.hidden = false;
  banner.className = `backend-banner backend-banner-${state}`;
  banner.textContent = t(state === 'offline' ? 'app.backendOffline' : 'app.backendReconnecting');
}

// ========== 顶栏状态指示器 ==========

let lastInstances = [];

function renderHeaderStatus(instances) {
  lastInstances = Array.isArray(instances) ? instances : [];
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
  renderSidebarStatus(instances);
}

// 侧栏底部状态块：把"收起导航"下面那片纯留白换成真正有用的常驻信息。
// 只写界面已经知道的事实 —— 应用版本目前没有任何前端可读的来源，所以不显示。
function renderSidebarStatus(instances) {
  const el = document.getElementById('sidebar-status');
  if (!el) return;
  const list = Array.isArray(instances) ? instances : [];
  const running = list.filter((i) => i.status === 'running').length;
  const stateKey = backendState === 'offline'
    ? 'app.backendOffline'
    : backendState === 'reconnecting' ? 'app.backendReconnecting' : 'app.statusConnected';
  el.innerHTML = `
    <div class="sidebar-status-row">
      <span class="sidebar-status-key">${escapeHtml(t('app.statusBackend'))}</span>
      <span class="sidebar-status-value is-${escapeHtml(backendState)}">${escapeHtml(t(stateKey))}</span>
    </div>
    <div class="sidebar-status-row">
      <span class="sidebar-status-key">${escapeHtml(t('app.statusInstances'))}</span>
      <span class="sidebar-status-value">${running} / ${list.length}</span>
    </div>`;
}

// ========== 上下文面板（二级左栏） ==========
// 页面模块可选导出 renderPanel(el, ctx) 来提供面板内容：
//   - 未导出（或用户收起）→ 整列隐藏，主内容自动占满
//   - 导出后由 navigateTo() 在每次切页时重新渲染
// 面板与页面共用同一个 ctx 对象，其中 ctx.refreshPanel() 可让页面在数据变化后
// 主动重绘面板（例如参数 schema 异步加载完成后）。

const PANEL_STATE_KEY = 'lunarshroud_panel_collapsed';
const PANEL_DRAWER_QUERY = '(max-width: 900px)';

function isPanelDrawerMode() {
  return window.matchMedia(PANEL_DRAWER_QUERY).matches;
}

function getPanelCollapsed() {
  try {
    return localStorage.getItem(PANEL_STATE_KEY) === '1';
  } catch {
    return false;
  }
}

function pageHasPanel(pageId) {
  const entry = pageId ? pageInstances.get(pageId) : null;
  if (!entry || typeof entry.module.renderPanel !== 'function') return false;
  return entry.module.hasPanel ? entry.module.hasPanel() : true;
}

function renderContextPanel(pageId) {
  const panel = document.getElementById('context-panel');
  const body = document.getElementById('context-panel-body');
  const title = document.getElementById('context-panel-title');
  if (!panel || !body) return;

  const entry = pageId ? pageInstances.get(pageId) : null;
  // 窄屏下面板是抽屉：桌面端的"收起"偏好不应让页面列表彻底消失，
  // 否则模型库/预设/日志在窄窗口里没有切换入口。
  const usable = pageHasPanel(pageId) && (isPanelDrawerMode() || !getPanelCollapsed());

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
  const drawerMode = isPanelDrawerMode();
  const drawerOpen = document.body.classList.contains('panel-drawer-open');
  if (panel) {
    // 抽屉关闭时移出可访问树与 Tab 顺序；宽屏下面板始终可访问。
    panel.inert = drawerMode && !drawerOpen;
  }
  if (toggle) {
    // 当前页没有面板时置灰而不是隐藏：隐藏会让顶栏按钮左右跳动
    const available = pageHasPanel(activePage);
    toggle.disabled = !available;
    const open = drawerMode ? drawerOpen : visible;
    toggle.setAttribute('aria-pressed', String(available && open));
    toggle.title = available ? t('panel.toggle') : t('panel.unavailable');
  }
  if (collapse) {
    collapse.title = t('panel.collapse');
  }
}

function closePanelDrawer({ restoreFocus = false } = {}) {
  if (!document.body.classList.contains('panel-drawer-open')) return;
  document.body.classList.remove('panel-drawer-open');
  const backdrop = document.getElementById('sidebar-backdrop');
  const sidebar = document.getElementById('sidebar');
  if (backdrop && !(sidebar && sidebar.classList.contains('show'))) {
    backdrop.classList.remove('show');
  }
  syncPanelToggles();
  if (restoreFocus) {
    const toggle = document.getElementById('btn-panel-toggle');
    if (toggle && toggle.isConnected) toggle.focus({ preventScroll: true });
  }
}

function openPanelDrawer() {
  if (!isPanelDrawerMode()) return;
  closeMobileSidebar();
  document.body.classList.add('panel-drawer-open');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.classList.add('show');
  syncPanelToggles();
  const collapse = document.getElementById('btn-panel-collapse');
  if (collapse && collapse.isConnected) collapse.focus({ preventScroll: true });
}

function setPanelCollapsed(collapsed) {
  if (isPanelDrawerMode()) {
    if (collapsed) closePanelDrawer({ restoreFocus: true });
    return;
  }
  try {
    localStorage.setItem(PANEL_STATE_KEY, collapsed ? '1' : '0');
  } catch {
    // 忽略持久化失败（隐私模式等）
  }
  renderContextPanel(activePage);
}

function togglePanel() {
  if (isPanelDrawerMode()) {
    if (document.body.classList.contains('panel-drawer-open')) {
      closePanelDrawer({ restoreFocus: true });
    } else {
      openPanelDrawer();
    }
    return;
  }
  setPanelCollapsed(!getPanelCollapsed());
}

// ========== 路由 ==========

function renderSidebar() {
  const icons = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    models: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    param_editor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><line x1="4" y1="21" x2="4" y2="14"/><line x1="4" y1="10" x2="4" y2="3"/><line x1="12" y1="21" x2="12" y2="12"/><line x1="12" y1="8" x2="12" y2="3"/><line x1="20" y1="21" x2="20" y2="16"/><line x1="20" y1="12" x2="20" y2="3"/><line x1="1" y1="14" x2="7" y2="14"/><line x1="9" y1="8" x2="15" y2="8"/><line x1="17" y1="16" x2="23" y2="16"/></svg>',
    presets: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
    knowledge: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20"/><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z"/><line x1="9" y1="7" x2="15" y2="7"/><line x1="9" y1="11" x2="15" y2="11"/></svg>',
    workspace: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z"/><polyline points="3.27 6.96 12 12.01 20.73 6.96"/><line x1="12" y1="22.08" x2="12" y2="12"/></svg>',
    observability: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    logs: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="8" y1="13" x2="16" y2="13"/><line x1="8" y1="17" x2="13" y2="17"/></svg>',
    monitor: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="22 12 18 12 15 21 9 3 6 12 2 12"/></svg>',
    history: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>',
    help: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="10"/><path d="M9.09 9a3 3 0 0 1 5.83 1c0 2-3 3-3 3"/><line x1="12" y1="17" x2="12.01" y2="17"/></svg>',
    settings: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M12.22 2h-.44a2 2 0 0 0-2 2v.18a2 2 0 0 1-1 1.73l-.43.25a2 2 0 0 1-2 0l-.15-.08a2 2 0 0 0-2.73.73l-.22.38a2 2 0 0 0 .73 2.73l.15.1a2 2 0 0 1 1 1.72v.51a2 2 0 0 1-1 1.74l-.15.09a2 2 0 0 0-.73 2.73l.22.38a2 2 0 0 0 2.73.73l.15-.08a2 2 0 0 1 2 0l.43.25a2 2 0 0 1 1 1.73V20a2 2 0 0 0 2 2h.44a2 2 0 0 0 2-2v-.18a2 2 0 0 1 1-1.73l.43-.25a2 2 0 0 1 2 0l.15.08a2 2 0 0 0 2.73-.73l.22-.39a2 2 0 0 0-.73-2.73l-.15-.08a2 2 0 0 1-1-1.74v-.5a2 2 0 0 1 1-1.74l.15-.09a2 2 0 0 0 .73-2.73l-.22-.38a2 2 0 0 0-2.73-.73l-.15.08a2 2 0 0 1-2 0l-.43-.25a2 2 0 0 1-1-1.73V4a2 2 0 0 0-2-2z"/><circle cx="12" cy="12" r="3"/></svg>',
  };
  const sidebar = document.getElementById('sidebar');
  const pageById = new Map(PAGES.map((p) => [p.id, p]));
  sidebar.innerHTML =
    NAV_GROUPS.map((group, index) => {
      const items = group.pages
        .map((id) => {
          const p = pageById.get(id);
          if (!p) return '';
          const label = t('nav.' + id);
          const modal = isModalPage(p);
          return `
        <button type="button" class="sidebar-item" data-page="${escapeHtml(id)}"
          title="${escapeHtml(label)}" aria-label="${escapeHtml(label)}"
          ${modal ? 'aria-haspopup="dialog"' : ''}>
          <span class="icon" aria-hidden="true">${icons[id] || ''}</span>
          <span class="label">${escapeHtml(label)}</span>
        </button>`;
        })
        .join('');
      return `${index === NAV_GROUPS.length - 1 ? '<div class="sidebar-spacer"></div>' : ''}
      <div class="sidebar-group" data-group="${escapeHtml(group.key)}">
        <div class="sidebar-group-label">${escapeHtml(t(group.labelKey))}</div>
        ${items}
      </div>`;
    })
    .join('') +
    `
    <div class="sidebar-footer">
      <div class="sidebar-status" id="sidebar-status" role="status"></div>
      <button type="button" class="sidebar-collapse" id="btn-sidebar-collapse">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="11 17 6 12 11 7"/><polyline points="18 17 13 12 18 7"/></svg>
        <span class="label">${escapeHtml(t('sidebar.collapse'))}</span>
      </button>
    </div>`;
  sidebar.setAttribute('aria-label', t('nav.label'));
  sidebar.querySelectorAll('.sidebar-item').forEach((el) => {
    el.addEventListener('click', () => navigateTo(el.dataset.page));
  });
  const collapseBtn = sidebar.querySelector('#btn-sidebar-collapse');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', () => setSidebarCollapsed(!getSidebarCollapsed()));
  }
  applySidebarState();
}

// 侧栏展开/收起是用户选择，跨会话保持；默认展开（图标 + 文字）。
const SIDEBAR_STATE_KEY = 'lunarshroud_sidebar_collapsed';

function getSidebarCollapsed() {
  try {
    return localStorage.getItem(SIDEBAR_STATE_KEY) === '1';
  } catch {
    return false;
  }
}

function setSidebarCollapsed(collapsed) {
  try {
    localStorage.setItem(SIDEBAR_STATE_KEY, collapsed ? '1' : '0');
  } catch {
    // 持久化失败只影响下次启动的初始状态
  }
  applySidebarState();
}

function applySidebarState() {
  const sidebar = document.getElementById('sidebar');
  if (!sidebar) return;
  const collapsed = getSidebarCollapsed();
  sidebar.classList.toggle('collapsed', collapsed);
  const btn = document.getElementById('btn-sidebar-collapse');
  if (btn) {
    const label = t(collapsed ? 'sidebar.expand' : 'sidebar.collapse');
    btn.title = label;
    btn.setAttribute('aria-label', label);
    btn.setAttribute('aria-pressed', String(collapsed));
    const svg = btn.querySelector('svg');
    if (svg) svg.classList.toggle('flipped', collapsed);
  }
}

function closeMobileSidebar() {
  document.getElementById('sidebar').classList.remove('show');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop && !document.body.classList.contains('panel-drawer-open')) {
    backdrop.classList.remove('show');
  }
}

function openMobileSidebar() {
  closePanelDrawer();
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
  closePanelDrawer();
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
  // 切换侧边栏激活（class 负责视觉，aria-current 负责读屏）
  document.querySelectorAll('.sidebar-item').forEach((el) => {
    const isActive = el.dataset.page === pageId;
    el.classList.toggle('active', isActive);
    if (isActive) el.setAttribute('aria-current', 'page');
    else el.removeAttribute('aria-current');
  });

  const entry = pageInstances.get(pageId);
  if (!entry) return;
  entry.root.classList.add('active');
  activePage = pageId;
  document.title = `${t('nav.' + pageId)} · LunarShroud`;
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
  setTheme(getThemePreference());
  // 系统主题变化时，只有“跟随系统”需要实时重绘。
  try {
    const media = window.matchMedia(THEME_QUERY);
    media.addEventListener('change', () => {
      if (getThemePreference() === 'system') setTheme('system');
    });
  } catch {
    // 旧 WebView 不支持 addEventListener 时忽略；用户仍可手动切换
  }
  document.documentElement.lang = getLocale();
  // 首屏就给出正确的状态文案，而不是空着等第一次推送（index.html 静态留空）
  const statusText = document.getElementById('header-status-text');
  if (statusText) statusText.textContent = t('app.statusNone');

  // 顶栏按钮
  document.getElementById('btn-theme').addEventListener('click', toggleTheme);
  const aboutBtn = document.getElementById('btn-about');
  if (aboutBtn) {
    const aboutLabel = t('app.about');
    aboutBtn.title = aboutLabel;
    aboutBtn.setAttribute('aria-label', aboutLabel);
    aboutBtn.innerHTML = `${aboutBtn.innerHTML}<span class="btn-label">${escapeHtml(aboutLabel)}</span>`;
  }
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
  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener('click', () => {
      closeMobileSidebar();
      closePanelDrawer({ restoreFocus: true });
    });
  }

  // 二级左栏：顶栏按钮负责"收起/展开"，面板头部按钮负责"收起"
  const panelToggle = document.getElementById('btn-panel-toggle');
  const panelCollapse = document.getElementById('btn-panel-collapse');
  if (panelToggle) panelToggle.addEventListener('click', togglePanel);
  if (panelCollapse) {
    panelCollapse.addEventListener('click', () => {
      if (isPanelDrawerMode()) closePanelDrawer({ restoreFocus: true });
      else setPanelCollapsed(true);
    });
  }
  // 抽屉里选中模型/预设/实例后自动收起，避免遮住刚打开的内容。
  const panelBody = document.getElementById('context-panel-body');
  if (panelBody) {
    panelBody.addEventListener('click', (event) => {
      if (!isPanelDrawerMode()) return;
      if (event.target.closest('button, a')) closePanelDrawer();
    });
  }
  // 从窄屏拖宽后，抽屉状态与 inert 必须归零，否则面板会被永久隔离。
  const panelModeMedia = window.matchMedia(PANEL_DRAWER_QUERY);
  const onPanelModeChange = () => {
    if (!panelModeMedia.matches) closePanelDrawer();
    syncPanelToggles();
  };
  if (typeof panelModeMedia.addEventListener === 'function') {
    panelModeMedia.addEventListener('change', onPanelModeChange);
  } else if (typeof panelModeMedia.addListener === 'function') {
    panelModeMedia.addListener(onPanelModeChange);
  }

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
      closePanelDrawer({ restoreFocus: true });
      closeMobileSidebar();
      return;
    }
    // Ctrl+K = 命令面板；Ctrl+L = 模型选择器。与 ESC 放在同一处。
    if ((e.ctrlKey || e.metaKey) && (e.key === 'k' || e.key === 'K')) {
      e.preventDefault();
      // A modal page (parameter editor / settings) may hold unsaved state.
      // Do not replace it from the shell; close it first.
      if (document.querySelector('#app-modal-overlay')) return;
      openCommandPalette(appCtx);
      return;
    }
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
    <div class="about-body">
      <p><strong>LunarShroud</strong> ${escapeHtml(t('app.aboutDesc1'))}</p>
      <p>${escapeHtml(t('app.aboutDesc2'))}</p>
      <p class="about-stack">${escapeHtml(t('app.aboutStack'))}</p>
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
    getThemePreference,
    subscribeInstances,
    t,
    // 页面间跳转。以前页面模块靠 document.querySelector('.sidebar-item[data-page=…]').click()
    // 模拟点击，既依赖 DOM 结构又会被复制到多处；统一走这里。
    navigate: navigateTo,
    openModelPicker: () => openModelPicker(appCtx),
  };
}

// 启动
function boot() {
  updateDebugBanner();
  initTooltips(document);
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
