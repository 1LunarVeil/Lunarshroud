// LunarShroud 主应用入口
// 负责：路由切换、全局 Toast/Modal/Confirm、主题/语言切换、
//       实例状态中心（WS 推送 + 轮询兜底）、初始化所有页面模块

import Api from './api.js?v=1';
import { createSingleFlight } from './runtime.mjs';
import { initTooltips } from './tooltip.mjs';
import { getLocale, setLocale, t, localizeBackendError, LOCALE_NAMES } from './i18n.mjs';
import { open as openModelPicker } from './model_picker.mjs';
import { openCommandPalette } from './command_palette.mjs';
import { escapeHtml } from './html.mjs';
import { closeModal, showConfirm, showModal } from './modal.mjs';
import { attachResizer, readSidebarWidths, SIDEBAR_LIMITS } from './resizable.mjs';

export { escapeHtml };

// 导入所有页面模块
import * as dashboard from './dashboard.js?v=1';
import * as chat from './chat.js?v=1';
import * as apiServer from './api_server.js?v=1';
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
  apiServer,
  workspace,
  knowledge,
  observability,
  paramEditor,
  help,
  settings,
];

// 侧栏分组：目的地与动作分开；参数编辑器/设置是模态动作，不是路由页面。
// 分组顺序对齐 LM Studio 的信息架构：会话 → 服务 → 资源 → 工具。
// 「服务」单独成组（而不是并进「会话」）：本地 API 服务是"把模型开放出去"的
// 一件事，与"自己跟自己聊"是两种不同的使用意图；混在一组里，用户要在
// Chat 与 API 服务之间来回时得先读组名猜它归哪儿。
//
// `settings` 不在这里：它属于侧栏底部的固定动作（与"收起导航"同一区），
// 放进分组会让同一个入口在侧栏出现两次。
const NAV_GROUPS = [
  { key: 'run', labelKey: 'nav.groupRun', pages: ['dashboard', 'chat'] },
  { key: 'serving', labelKey: 'nav.groupServing', pages: ['api_server'] },
  { key: 'resources', labelKey: 'nav.groupResources', pages: ['workspace', 'knowledge', 'observability'] },
  { key: 'tools', labelKey: 'nav.groupTools', pages: ['param_editor', 'help'] },
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

/**
 * 顶栏中央的"当前模型"胶囊。
 *
 * 这是整窗唯一使用强调色实心填充的元素（见 docs/DESIGN_1.0.md 的 signature
 * 说明）：其余地方强调色只出现在焦点环、激活项左缘和链接上。
 *
 * 文案分两种语义：没有运行中实例时是**邀请动作**（"未加载模型"），有实例时
 * 是**陈述状态**（模型名）。以前两者都是 "选择要加载的模型"，于是"已经加载
 * 了 qwen3.5-4b"这个事实在顶栏上永远看不见 —— 用户只能去 Chat 页或仪表盘确认。
 *
 * 模型名取 `model_path` 的文件名（实例列表里没有更短的显示名），多个实例时
 * 用 "+N" 收口。全部按钮内容重建（innerHTML）后重新套上 kbd 提示，因为
 * `Ctrl+L` 是发现性信息，不该只在空态出现。
 */
/**
 * 顶栏模型胶囊：显示"当前在跟哪个模型说话"，并提供一键停止。
 *
 * 结构约束（改之前先读）：外壳是 `<div id="btn-model-picker" role="group">`
 * 而不是 `<button>` —— 胶囊里要放"停止"按钮，HTML 不允许 button 套 button；
 * 而且外层若是 button，点在停止上会冒泡成"打开模型选择器"。
 *
 * 另一个坑：这个函数在每次实例 WS 推送时都会重写 innerHTML，
 * 所以**事件必须委托在外层容器上绑一次**（见 initLayout），不能绑在内部节点。
 */
export function renderModelCapsule(instances) {
  const btn = document.getElementById('btn-model-picker');
  if (!btn) return;
  const list = Array.isArray(instances) ? instances : [];
  // stopping 也要显示：点完停止后实例先进 stopping，若这时胶囊立刻跳回
  // "选择要加载的模型"，用户会以为没生效。等它真的 stopped 再回空态。
  const active = list.filter(
    (item) => item && (item.status === 'running' || item.status === 'stopping')
  );
  const kbd = `<span class="kbd">Ctrl+L</span>`;
  if (!active.length) {
    btn.classList.remove('is-loaded');
    btn.innerHTML = `<button type="button" class="model-capsule-open">${escapeHtml(
      t('app.pickModel')
    )} ${kbd}</button>`;
    btn.title = t('app.pickModel');
    return;
  }
  const primary = active[0];
  const stopping = primary.status === 'stopping';
  const path = String(primary.model_path || primary.name || '');
  const base = path.split(/[\\/]/).pop() || primary.name || '';
  const label =
    active.length > 1
      ? t('app.modelCapsuleRunningMany', { name: base, count: active.length - 1 })
      : base;
  btn.classList.add('is-loaded');
  btn.classList.toggle('is-stopping', stopping);
  btn.innerHTML = `
    <button type="button" class="model-capsule-open">
      <span class="model-capsule-dot" aria-hidden="true"></span>
      <span class="model-capsule-name"></span>
      ${kbd}
    </button>
    <button type="button" class="model-capsule-stop" data-capsule-stop="${escapeHtml(
      String(primary.id || '')
    )}"${stopping ? ' disabled' : ''}
      title="${escapeHtml(t(stopping ? 'app.modelCapsuleStopping' : 'app.modelCapsuleStop'))}"
      aria-label="${escapeHtml(t(stopping ? 'app.modelCapsuleStopping' : 'app.modelCapsuleStop'))}">
      <svg viewBox="0 0 24 24" width="12" height="12" fill="currentColor" aria-hidden="true"><rect x="6" y="6" width="12" height="12" rx="2"/></svg>
    </button>`;
  const nameEl = btn.querySelector('.model-capsule-name');
  // 模型名来自文件系统，一律走 textContent —— 这里绝不拼 HTML。
  if (nameEl) nameEl.textContent = label;
  btn.title = stopping ? t('app.modelCapsuleStopping') : t('app.modelCapsuleHint');
}

/** 停止胶囊里那个实例。id 从 data 属性取（DOM 每次推送都会重建）。 */
async function stopCapsuleInstance(instanceId) {
  if (!instanceId) return;
  try {
    await Api.stopInstance(instanceId, false);
    showToast(t('dash.stopSent'), 'info');
  } catch (error) {
    showToast(`${t('dash.stopFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

function renderHeaderStatus(instances) {
  lastInstances = Array.isArray(instances) ? instances : [];
  renderModelCapsule(lastInstances);
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

// ---------- 侧栏底部硬件芯片（LM Studio 式常驻读数） ----------
//
// 为什么放在侧栏底部而不是仪表盘：这两个数字（内存 / 显存占用）在**整个会话
// 期间**都要能瞥见，而仪表盘是一个要主动跳过去的页面。LM Studio 把同样的
// 读数钉在左下角，用户随时知道"还够不够再开一个实例"。
//
// 数据源是 /api/system/overview（已有接口，monitor 页在用）。轮询 8s：
// 比 monitor 页的实时曲线慢，因为这里只需要"大致水位"，不需要波形。
const HARDWARE_POLL_MS = 8000;
let hardwareTimer = null;

function formatPercent(value) {
  const n = Number(value);
  if (!Number.isFinite(n)) return '—';
  return `${Math.round(n)}%`;
}

function formatGb(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '—';
  return n >= 10 ? String(Math.round(n)) : n.toFixed(1);
}

function renderHardwareChips(overview) {
  const el = document.getElementById('sidebar-hardware');
  if (!el) return;
  if (!overview || typeof overview !== 'object') {
    el.hidden = true;
    return;
  }
  el.hidden = false;
  const cpu = formatPercent(overview.cpu_percent);
  const memUsed = formatGb(overview.memory_used_gb);
  const memTotal = formatGb(overview.memory_total_gb);
  const gpus = Array.isArray(overview.gpus) ? overview.gpus : [];
  // 显存取**已用/总量**最大的那块卡（多卡时其余在仪表盘）。没有 GPU 数据
  // 时整块不显示 —— 编一个 0% 比留白更误导。
  //
  // 字段名实测是 `memory_used_mb` / `memory_total_mb`（见
  // `core/gpu_monitor.py` 的 snapshot）：早期代码按 `_gb` 读，于是显存芯片
  // 永远显示"无 GPU 数据"，而机器其实有卡 —— 这类"字段名猜错"的静默降级
  // 只有真跑一次 overview 才看得出来。
  let vram = null;
  for (const gpu of gpus) {
    const usedMb = Number(gpu.memory_used_mb);
    const totalMb = Number(gpu.memory_total_mb);
    if (Number.isFinite(usedMb) && Number.isFinite(totalMb) && totalMb > 0) {
      const ratio = usedMb / totalMb;
      if (!vram || ratio > vram.ratio) {
        vram = { used: usedMb / 1024, total: totalMb / 1024, ratio };
      }
    }
  }
  const vramText = vram
    ? `${formatGb(vram.used)}/${formatGb(vram.total)} GB`
    : t('sidebar.chipNoGpu');
  const memText = `${memUsed}/${memTotal} GB`;
  el.innerHTML = `
    <span class="hardware-chip" title="${escapeHtml(t('sidebar.chipCpu'))}">
      <span class="hardware-chip-key">${escapeHtml(t('sidebar.chipCpu'))}</span>
      <span class="hardware-chip-value">${escapeHtml(cpu)}</span>
    </span>
    <span class="hardware-chip" title="${escapeHtml(t('sidebar.chipRam'))}">
      <span class="hardware-chip-key">${escapeHtml(t('sidebar.chipRam'))}</span>
      <span class="hardware-chip-value">${escapeHtml(memText)}</span>
    </span>
    <span class="hardware-chip${vram ? '' : ' is-empty'}" title="${escapeHtml(t('sidebar.chipVram'))}">
      <span class="hardware-chip-key">${escapeHtml(t('sidebar.chipVram'))}</span>
      <span class="hardware-chip-value">${escapeHtml(vramText)}</span>
    </span>`;
}

async function pollHardware() {
  try {
    const overview = await Api.getSystemOverview();
    renderHardwareChips(overview);
  } catch {
    // 后端重启/离线：保留上一次读数而不是清空（清空会让芯片闪一下）。
    // 真正的"后端不可用"由顶栏状态点与横幅表达 —— 同一件事不报两遍。
  }
}

function startHardwareChips() {
  if (hardwareTimer) return;
  pollHardware();
  hardwareTimer = setInterval(pollHardware, HARDWARE_POLL_MS);
}

function stopHardwareChips() {
  if (hardwareTimer) {
    clearInterval(hardwareTimer);
    hardwareTimer = null;
  }
}

// ========== 侧栏列表（当前页面的列表直接长在导航栏里） ==========
//
// 1.0 第二轮把"二级列表列"整列删掉了。旧结构是：
//
//     [图标轨道] [列表列] [内容区]
//
// 三个分区各用一条边框分开，窗口被切成三条互不相干的带子；而"列表列"还要
// 一个顶栏开关去控制显隐 —— 那个开关藏在右上角第三个位置，关掉之后没人知道
// 怎么再打开。LM Studio 的做法是单列：导航项在上、当前页面的列表在下。
// 于是窗口只剩"导航栏 + 内容"两条带子，也不需要任何"显示/隐藏列表"的开关。
//
// 页面模块可选导出 renderSidebarList(el, ctx) 来提供列表区内容：
//   - 未导出 → 列表区为空并收起，只显示导航项
//   - 导出后由 navigateTo() 在每次切页时重新渲染
//
// 列表项**不能**复用 .sidebar-item：core-paths 的"9 个导航项"断言与
// renderSidebar() 的 navigateTo 绑定都按 .sidebar-item 选择，混用会让列表项
// 被当成页面入口。列表项用 .sidebar-list-item + [data-list-key]。

/** 列表区宿主的 id。app.js 在 renderSidebar 里创建，各页面往里面渲染。 */
const SIDEBAR_LIST_ID = 'sidebar-list-body';

/**
 * 当前页是否提供列表区内容。
 *
 * 与旧 pageHasPanel 的区别：现在没有"整列隐藏"这回事（列总是存在），
 * 这里只决定列表区**占不占高度**。
 */
function pageHasList(pageId) {
  const entry = pageId ? pageInstances.get(pageId) : null;
  if (!entry || typeof entry.module.renderSidebarList !== 'function') return false;
  return entry.module.hasSidebarList ? entry.module.hasSidebarList() : true;
}

/**
 * 重绘当前页面的列表区。
 *
 * 为什么每次都 `replaceChildren()` 而不是增量更新：列表内容由页面模块全权
 * 生成（自己带搜索框、过滤器、分组），app.js 不该知道它们的结构。代价是
 * 页面必须在数据没变时**不调用**它 —— 各页面已有这份判断（models 用
 * searchKeyword 局部重绘、logs 用 instanceSignature 去重、knowledge 用轮询
 * 节流）。
 */
function renderSidebarList(pageId) {
  const host = document.getElementById(SIDEBAR_LIST_ID);
  const wrap = document.getElementById('sidebar-list');
  if (!host || !wrap) return;

  const entry = pageId ? pageInstances.get(pageId) : null;
  const usable = pageHasList(pageId);

  // 列表区在无列表的页面整个收起（不占高度、不画分隔线）。
  wrap.hidden = !usable;
  if (!usable) {
    host.replaceChildren();
    return;
  }

  // 标题与主行动由**页面自己**声明，而不是在这里猜 `t('nav.' + pageId)`：
  // workspace / observability 是聚合页，标题必须跟着当前 tab 走
  // （"模型工作区"下切到运行环境时，标题该是"运行环境"）。
  const titleEl = document.getElementById('sidebar-list-title');
  const actionEl = document.getElementById('sidebar-list-action');
  const declaredTitle =
    (typeof entry.module.sidebarListTitle === 'function' && entry.module.sidebarListTitle()) ||
    (entry.module.sidebarListTitleKey ? t(entry.module.sidebarListTitleKey) : '') ||
    t('nav.' + pageId);
  if (titleEl) titleEl.textContent = declaredTitle;

  const action =
    typeof entry.module.sidebarListAction === 'function' ? entry.module.sidebarListAction() : null;
  if (actionEl) {
    if (action) {
      actionEl.hidden = false;
      actionEl.textContent = action.label || '+';
      actionEl.title = action.title || action.label || '';
      actionEl.setAttribute('aria-label', action.title || action.label || '');
      // onclick 直接覆盖：每次重绘都重新赋值，不会累积监听器。
      actionEl.onclick = () => action.onClick();
    } else {
      actionEl.hidden = true;
      actionEl.textContent = '';
      actionEl.onclick = null;
    }
  }

  host.replaceChildren();
  try {
    entry.module.renderSidebarList(host, appCtx);
  } catch (e) {
    console.error(`[renderSidebarList ${pageId} failed]`, e);
    host.replaceChildren();
  }
}

/**
 * 逐字符输入时重绘列表，但保留搜索框的焦点与光标。
 *
 * 列表整块重建会把 <input> 换掉，焦点随之丢失 —— 用户敲第二个字时键盘
 * 事件就落到 body 上了。models 与 chat 的搜索框都会踩这个坑，所以统一
 * 在这里做：记住 activeElement 的 id 与 selectionStart，重建后恢复。
 */
function refreshSidebarListPreservingFocus() {
  const active = document.activeElement;
  const focusId = active && active.id ? active.id : '';
  const caret =
    focusId && typeof active.selectionStart === 'number' ? active.selectionStart : null;
  renderSidebarList(activePage);
  if (!focusId) return;
  const restored = document.getElementById(focusId);
  if (!restored || typeof restored.focus !== 'function') return;
  restored.focus({ preventScroll: true });
  if (caret !== null && typeof restored.setSelectionRange === 'function') {
    try {
      restored.setSelectionRange(caret, caret);
    } catch {
      // 非文本输入（number 等）不支持 setSelectionRange，忽略即可
    }
  }
}

// ========== 路由 ==========

// ========== 路由 ==========

function renderSidebar() {
  const icons = {
    dashboard: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
    models: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/></svg>',
    chat: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>',
    api_server: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><rect x="2" y="3" width="20" height="7" rx="2"/><rect x="2" y="14" width="20" height="7" rx="2"/><line x1="6" y1="6.5" x2="6.01" y2="6.5"/><line x1="6" y1="17.5" x2="6.01" y2="17.5"/></svg>',
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

  // 三段结构：导航项（自然高度）/ 列表区（flex:1 可滚）/ 底部动作（固定）。
  // 导航项单独包一层 .sidebar-nav，这样列表区滚动时不会把导航项一起滚走。
  const navHtml = NAV_GROUPS.map((group) => {
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
    return `
      <div class="sidebar-group" data-group="${escapeHtml(group.key)}">
        <div class="sidebar-group-label">${escapeHtml(t(group.labelKey))}</div>
        ${items}
      </div>`;
  }).join('');

  sidebar.innerHTML = `
    <div class="sidebar-nav">${navHtml}</div>
    <!-- 当前页面的列表。没有列表的页面整块 hidden（样式与 DOM 都不占位）。 -->
    <section class="sidebar-list" id="sidebar-list" hidden>
      <div class="sidebar-list-head">
        <span class="sidebar-list-title" id="sidebar-list-title"></span>
        <button type="button" class="btn btn-ghost btn-icon btn-sm" id="sidebar-list-action"
          hidden aria-label="" title=""></button>
      </div>
      <div class="sidebar-list-body" id="${SIDEBAR_LIST_ID}"></div>
    </section>
    <div class="sidebar-footer">
      <!-- 设置是**动作**而不是导航目的地：它打开浮层、不改变路由。
           刻意用 .sidebar-action 而不是 .sidebar-item —— 后者是页面入口的
           选择器（navigateTo 绑定、导航计数、aria-current 都按它查询），
           混用会让"点设置"被当成一次路由跳转。 -->
      <button type="button" class="sidebar-action" id="btn-sidebar-settings"
        aria-haspopup="dialog"
        title="${escapeHtml(t('nav.settings'))}" aria-label="${escapeHtml(t('nav.settings'))}">
        <span class="icon" aria-hidden="true">${icons.settings}</span>
        <span class="label">${escapeHtml(t('nav.settings'))}</span>
      </button>
      <button type="button" class="sidebar-collapse" id="btn-sidebar-collapse"
        title="${escapeHtml(t('sidebar.collapse'))}" aria-label="${escapeHtml(t('sidebar.collapse'))}">
        <svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="11 17 6 12 11 7"/><polyline points="18 17 13 12 18 7"/></svg>
        <span class="label">${escapeHtml(t('sidebar.collapse'))}</span>
      </button>
    </div>`;
  sidebar.setAttribute('aria-label', t('nav.label'));
  // 只给真正的页面入口绑 navigateTo。列表项由页面模块自己绑事件。
  sidebar.querySelectorAll('.sidebar-item[data-page]').forEach((el) => {
    el.addEventListener('click', () => navigateTo(el.dataset.page));
  });
  const collapseBtn = sidebar.querySelector('#btn-sidebar-collapse');
  if (collapseBtn) {
    collapseBtn.addEventListener('click', () => setSidebarCollapsed(true));
  }
  // 设置是模态动作，但它在这套布局里属于"底部动作"，不是导航分组里的一项。
  const settingsBtn = sidebar.querySelector('#btn-sidebar-settings');
  if (settingsBtn) {
    settingsBtn.addEventListener('click', () => navigateTo('settings'));
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
  // 收起 = **整列隐藏**。刻意没有"图标轨道"中间态：68px 的窄条里图标没地方
  // 放文字，收起来必然难看（这是上一轮被指出的问题之一）。
  sidebar.classList.toggle('collapsed', collapsed);
  document.body.classList.toggle('sidebar-collapsed', collapsed);

  // 内联宽度必须先清掉再交给 CSS：拖拽手柄写的是 `style.width`，内联样式的
  // 优先级高于 `.sidebar.collapsed { width: 0 }`，不清的话收起后仍是原宽度。
  if (collapsed) {
    sidebar.style.removeProperty('width');
  }

  syncSidebarToggle();

  // 拖拽手柄在收起态没有意义（列是 0 宽），一并隐藏。
  const resizer = document.querySelector('.sidebar-resizer-wrap');
  if (resizer) resizer.hidden = collapsed;
}

/** 顶栏汉堡按钮的可见性：侧栏不可见时才出现（桌面收起 / 窄屏抽屉关闭）。 */
function syncSidebarToggle() {
  const toggle = document.getElementById('btn-sidebar-toggle');
  if (!toggle) return;
  const sidebar = document.getElementById('sidebar');
  const drawerOpen = Boolean(sidebar && sidebar.classList.contains('show'));
  toggle.hidden = !(getSidebarCollapsed() || (isMobileDrawerMode() && !drawerOpen));
}

/** 窄屏（≤900px）时侧栏是抽屉而不是常驻列。 */
function isMobileDrawerMode() {
  return window.matchMedia('(max-width: 900px)').matches;
}

function closeMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.remove('show');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.classList.remove('show');
  syncSidebarToggle();
}

function openMobileSidebar() {
  const sidebar = document.getElementById('sidebar');
  if (sidebar) sidebar.classList.add('show');
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.classList.add('show');
  syncSidebarToggle();
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
  // 侧栏列表区随页面切换（没有列表的页面整块收起）
  renderSidebarList(pageId);
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

  // 侧栏开关：桌面端"从收起恢复"与窄屏"打开抽屉"是同一件事，共用一个按钮。
  const sidebarToggle = document.getElementById('btn-sidebar-toggle');
  const sidebarBackdrop = document.getElementById('sidebar-backdrop');
  if (sidebarToggle) {
    const label = t('app.sidebarToggle');
    sidebarToggle.title = label;
    sidebarToggle.setAttribute('aria-label', label);
    sidebarToggle.addEventListener('click', () => {
      if (isMobileDrawerMode()) toggleMobileSidebar();
      else setSidebarCollapsed(false);
    });
  }
  if (sidebarBackdrop) {
    sidebarBackdrop.addEventListener('click', () => closeMobileSidebar());
  }
  // 视口跨过 900px 时要重算汉堡按钮的显隐：拖窄窗口后侧栏变抽屉、按钮必须
  // 出现，否则用户没有任何入口打开导航。用媒体查询监听而不是 resize 事件，
  // 因为后者每次拖动都触发、而这里只在跨断点时才需要做事。
  const drawerMedia = window.matchMedia('(max-width: 900px)');
  const onDrawerModeChange = () => {
    if (!drawerMedia.matches) {
      // 从窄屏拖回宽屏：抽屉状态必须清掉，否则侧栏会带着 .show 停在那里。
      closeMobileSidebar();
      applySidebarState();
    }
    syncSidebarToggle();
  };
  if (typeof drawerMedia.addEventListener === 'function') {
    drawerMedia.addEventListener('change', onDrawerModeChange);
  } else if (typeof drawerMedia.addListener === 'function') {
    drawerMedia.addListener(onDrawerModeChange);
  }

  // 居中模型选择器：按钮文案在这里注入（index.html 是静态的，写死中文会漏翻）
  const pickerBtn = document.getElementById('btn-model-picker');
  if (pickerBtn) {
    // 先用空态文案占位；实例列表到达后 renderModelCapsule() 会改成当前模型名。
    renderModelCapsule([]);
    // **委托一次**：renderModelCapsule 在每次实例推送时重写 innerHTML，
    // 绑在内部节点上的监听器下一次推送就没了。
    pickerBtn.addEventListener('click', (event) => {
      const stopBtn = event.target.closest('[data-capsule-stop]');
      if (stopBtn) {
        event.stopPropagation();
        void stopCapsuleInstance(stopBtn.dataset.capsuleStop);
        return;
      }
      openModelPicker(appCtx);
    });
  }

  // 全局 ESC 关闭 Modal 与窄屏侧边栏
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') {
      closeModal();
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
  // 侧栏底部常驻硬件读数（内存 / 显存 / CPU）
  startHardwareChips();
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
  // 页面在数据变化后主动重绘侧栏列表（例如参数 schema 异步加载完成）。
  // 保留 refreshPanel 作为别名：15 处既有调用点还没全部改名，
  // 而漏改一处就会在浏览器 console 留下 TypeError（测试会因此变红）。
  appCtx.refreshList = () => renderSidebarList(activePage);
  appCtx.refreshPanel = appCtx.refreshList;
  // 逐字符输入时重绘列表并保留搜索框焦点（models / chat 的搜索框用）。
  appCtx.refreshListKeepFocus = () => refreshSidebarListPreservingFocus();
  initLayout();
  initSidebarResizers();
  renderSidebar();
  initPages();
  navigateTo('dashboard');
  console.log('[LunarShroud] frontend ready');
}

/**
 * 接上侧栏的拖拽手柄，并把宽度存进配置。
 *
 * 存配置而不是 localStorage：换机器、清缓存、换 WebView 数据目录后布局都
 * 应该还在（打包态下 WebView 数据目录是可被清理的，见 AGENTS §2.18）。
 * 写回走防抖且只在松手时提交，避免拖动过程中每一帧都发一个 PUT。
 *
 * 1.0 第二轮只剩一个手柄：独立的列表列被删掉了，`chat_panel_width` 现在
 * 由 Chat 自己的右侧参数面板使用（见 chat.js 的 initInspectorResizer）。
 */
function initSidebarResizers() {
  const sidebar = document.getElementById('sidebar');
  const sidebarHandle = document.getElementById('sidebar-resizer');
  if (!sidebar || !sidebarHandle) return;

  let commitTimer = null;
  let pendingWidth = null;

  const commit = (width) => {
    pendingWidth = width;
    if (commitTimer) clearTimeout(commitTimer);
    commitTimer = setTimeout(async () => {
      const value = pendingWidth;
      pendingWidth = null;
      commitTimer = null;
      if (value === null) return;
      try {
        await Api.updateSystemConfig({ chat_sidebar_width: value });
      } catch (error) {
        // 保存失败不回滚视觉：宽度立即生效是用户的操作预期，
        // "重新打开回到旧值"才是没保存的真实表现。
        console.warn('[LunarShroud] 保存侧栏宽度失败:', error && error.message);
      }
    }, 500);
  };

  attachResizer({
    handle: sidebarHandle,
    target: sidebar,
    startWidth: SIDEBAR_LIMITS.default,
    limits: SIDEBAR_LIMITS,
    onCommit: commit,
  });

  // 配置到达后应用保存过的宽度。失败就用 CSS 默认值 —— 布局是纯装饰，
  // 不该因为一次配置请求失败而挡住界面。
  (async () => {
    try {
      const config = await Api.getSystemConfig();
      const widths = readSidebarWidths(config);
      sidebar.style.width = `${widths.sidebar}px`;
    } catch {
      /* 保持 CSS 默认值 */
    }
  })();
}

// DOM 就绪后启动
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', boot);
} else {
  boot();
}
