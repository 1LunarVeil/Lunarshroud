// 实时日志页面
// 实例列表在二级左栏（上下文面板），正文只有日志流（WebSocket）
// 着色：stdout 白、stderr/system 蓝、错误关键字红
// 实例列表来自实例状态中心（WS 推送 + 轮询兜底）

import { boundedAppend } from './runtime.mjs';
import { t, getLocale } from './i18n.mjs';
import { renderInstanceList, instanceSignature, emptyStateHtml } from './panel_views.mjs';

export const id = 'logs';
export const title = '实时日志';

let ctx = null;
let root = null;
let watcher = null;
let currentInstanceId = null;
let instanceList = [];    // 实例状态中心的最新快照（面板据此渲染）
let lastPanelSignature = '';
let logLines = [];        // 当前实例的所有日志缓存
let logKeys = new Set();  // 已见日志的去重键（重连后后端会重发历史日志）
let autoScroll = true;
let searchKeyword = '';
let levelFilter = 'all';  // all / stdout / stderr / system
let connToastShown = false; // 每次断线只提示一次
let unsubscribe = null;
const MAX_LOG_BUFFER = 5000;
const MAX_RENDERED_LOGS = 1500;

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <div class="page-title">${ctx.escapeHtml(t('logs.title'))}</div>
        <div class="page-subtitle">${ctx.escapeHtml(t('logs.subtitle'))}</div>
      </div>
    </div>
    <div class="logs-main">
      <div class="logs-toolbar">
        <input type="text" id="log-search" placeholder="${ctx.escapeHtml(t('logs.searchPh'))}" style="min-width:160px;"/>
        <select id="log-level" class="select" style="background:#0F172A;color:#E2E8F0;border:1px solid #334155;">
          <option value="all">${ctx.escapeHtml(t('logs.all'))}</option>
          <option value="stdout">stdout</option>
          <option value="stderr">stderr</option>
          <option value="system">system</option>
        </select>
        <button class="btn btn-sm" id="log-pause" data-paused="false">⏸ ${ctx.escapeHtml(t('logs.pause'))}</button>
        <button class="btn btn-sm" id="log-clear">${ctx.escapeHtml(t('logs.clear'))}</button>
        <button class="btn btn-sm" id="log-download">${ctx.escapeHtml(t('logs.download'))}</button>
        <span id="log-conn-state" class="log-conn-warn" style="display:none;"></span>
      </div>
      <div class="logs-view" id="logs-view"></div>
    </div>
  `;
  bindEvents();
}

function bindEvents() {
  root.querySelector('#log-search').addEventListener('input', (e) => {
    searchKeyword = e.target.value.toLowerCase();
    renderLogs();
  });
  root.querySelector('#log-level').addEventListener('change', (e) => {
    levelFilter = e.target.value;
    renderLogs();
  });
  root.querySelector('#log-pause').addEventListener('click', (e) => {
    autoScroll = !autoScroll;
    e.target.dataset.paused = (!autoScroll).toString();
    e.target.textContent = autoScroll ? `⏸ ${t('logs.pause')}` : `▶ ${t('logs.resume')}`;
  });
  root.querySelector('#log-clear').addEventListener('click', () => {
    logLines = [];
    logKeys.clear();
    renderLogs();
  });
  root.querySelector('#log-download').addEventListener('click', downloadLogs);
}

export function onShow() {
  unsubscribe = ctx.subscribeInstances(updateInstances);
  // 若有从仪表盘跳转带来的目标实例
  if (window.__pendingLogsInstanceId) {
    selectInstance(window.__pendingLogsInstanceId);
    window.__pendingLogsInstanceId = null;
  } else if (currentInstanceId) {
    // 切回页面时重新打开 WebSocket（onHide 时已关闭）
    openWs();
  } else {
    // 无实例：渲染统一空态引导（此前是一块暗色空白）
    renderLogs();
  }
}

export function onHide() {
  closeWs();
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
}

function updateInstances(list) {
  instanceList = Array.isArray(list) ? list : [];
  // 若当前选中实例不在列表里，清空选择
  if (currentInstanceId && !instanceList.some((i) => i.id === currentInstanceId)) {
    // 实例被移除，关闭 WS
    closeWs();
    currentInstanceId = null;
  }
  // WS 会持续推送实例状态，签名没变就不重绘 ——
  // 否则每次推送都会重建面板 DOM，把用户滚到一半的面板位置顶回顶部
  const sig = instanceSignature(instanceList, currentInstanceId);
  if (sig === lastPanelSignature) return;
  lastPanelSignature = sig;
  ctx.refreshPanel();
}

// 二级面板：实例列表（本模块不额外持有列表状态，直接读 instanceList）
export function renderPanel(el) {
  renderInstanceList(el, ctx, {
    instances: instanceList,
    activeId: currentInstanceId,
    onSelect: (id) => selectInstance(id),
    emptyText: t('logs.noInstances'),
  });
}

function selectInstance(instanceId) {
  if (currentInstanceId === instanceId) return;
  closeWs();
  currentInstanceId = instanceId;
  logLines = [];
  logKeys = new Set();
  renderLogs();
  openWs();
  // 面板高亮跟着走
  lastPanelSignature = '';
  ctx.refreshPanel();
}

function closeWs() {
  if (watcher) {
    watcher.close();
    watcher = null;
  }
  renderConnState('closed');
}

function openWs() {
  if (!currentInstanceId) return;
  closeWs();
  connToastShown = false;
  watcher = ctx.Api.watchInstanceLogs(currentInstanceId, {
    onMessage: pushLog,
    onStatus: renderConnState,
  });
}

// 收到一条日志：跳过心跳，按 时间戳|来源|内容 去重
// （WebSocket 重连后后端会重发最近历史，避免重复行）
function pushLog(msg) {
  if (!msg || msg.type === 'ping') return;
  const key = `${msg.timestamp}|${msg.source}|${msg.line}`;
  if (logKeys.has(key)) return;
  logKeys.add(key);
  logLines.push(msg);
  if (logLines.length > MAX_LOG_BUFFER) {
    const dropped = logLines.slice(0, logLines.length - MAX_LOG_BUFFER);
    logLines = logLines.slice(-MAX_LOG_BUFFER);
    for (const entry of dropped) {
      logKeys.delete(`${entry.timestamp}|${entry.source}|${entry.line}`);
    }
  }
  appendLogLine(msg);
}

function renderConnState(state) {
  const el = root.querySelector('#log-conn-state');
  if (!el) return;
  if (state === 'reconnecting') {
    el.textContent = `⚠ ${t('logs.reconnecting')}`;
    el.style.display = '';
    if (!connToastShown) {
      connToastShown = true;
      ctx.showToast(t('logs.reconnectingToast'), 'warning');
    }
  } else if (state === 'connected') {
    el.style.display = 'none';
    connToastShown = false;
  } else if (state === 'closed') {
    el.style.display = 'none';
  }
}

function appendLogLine(entry) {
  if (!shouldShow(entry)) return;
  const view = root.querySelector('#logs-view');
  const div = document.createElement('div');
  div.className = `log-line ${classifyLine(entry)}`;
  const ts = entry.timestamp ? formatTs(entry.timestamp) : '';
  div.innerHTML = `<span class="ts">${ctx.escapeHtml(ts)}</span>` + highlight(entry.line || '');
  boundedAppend(view, div, MAX_RENDERED_LOGS);
  if (autoScroll) view.scrollTop = view.scrollHeight;
}

function shouldShow(entry) {
  if (levelFilter !== 'all' && entry.source !== levelFilter) return false;
  if (searchKeyword && !String(entry.line || '').toLowerCase().includes(searchKeyword)) return false;
  return true;
}

function classifyLine(entry) {
  const source = entry.source || 'stdout';
  const line = String(entry.line || '').toLowerCase();
  if (/(error|fail|exception|traceback|fatal)/.test(line)) return source + ' error';
  return source;
}

function formatTs(ts) {
  // ts 可能是 unix 时间戳（秒）或 ISO 字符串
  let d;
  if (typeof ts === 'number') {
    d = new Date(ts * 1000);
  } else {
    d = new Date(ts);
  }
  if (isNaN(d.getTime())) return '';
  // 跟随当前 locale；原先写死 'zh-CN'，英文界面下日志时间戳仍是中文格式，
  // 与 app.js 的 formatTime（跟随 locale）不一致。
  return d.toLocaleTimeString(getLocale(), { hour12: false });
}

function highlight(text) {
  if (!searchKeyword) return ctx.escapeHtml(text);
  const safe = ctx.escapeHtml(text);
  // 关键词同样做 HTML 转义后再匹配，否则含 & < > 等字符的
  // 关键词永远匹配不到已转义文本中的对应实体
  const needle = escapeRegExp(ctx.escapeHtml(searchKeyword));
  const re = new RegExp(`(${needle})`, 'gi');
  return safe.replace(re, '<mark>$1</mark>');
}

function escapeRegExp(s) {
  return s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

function renderLogs() {
  const view = root.querySelector('#logs-view');
  view.innerHTML = '';
  const frag = document.createDocumentFragment();
  const visible = logLines.filter(shouldShow).slice(-MAX_RENDERED_LOGS);
  for (const entry of visible) {
    const div = document.createElement('div');
    div.className = `log-line ${classifyLine(entry)}`;
    const ts = entry.timestamp ? formatTs(entry.timestamp) : '';
    div.innerHTML = `<span class="ts">${ctx.escapeHtml(ts)}</span>` + highlight(entry.line || '');
    frag.appendChild(div);
  }
  // 空态：无实例 / 无日志时给引导，而不是一块暗色空白
  if (!visible.length) {
    view.appendChild(
      document
        .createRange()
        .createContextualFragment(
          emptyStateHtml(ctx, { icon: 'file', title: t(currentInstanceId ? 'logs.empty' : 'logs.noInstances'), sub: t('logs.emptySub') })
        )
    );
    return;
  }
  view.appendChild(frag);
  if (autoScroll) view.scrollTop = view.scrollHeight;
}

function downloadLogs() {
  if (!logLines.length) {
    ctx.showToast(t('logs.nothingToDownload'), 'warning');
    return;
  }
  const text = logLines.map((e) => `[${formatTs(e.timestamp)}] [${e.source}] ${e.line || ''}`).join('\n');
  const blob = new Blob([text], { type: 'text/plain;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = `logs-${currentInstanceId || 'unknown'}-${Date.now()}.log`;
  a.click();
  URL.revokeObjectURL(url);
  ctx.showToast(t('logs.downloaded'), 'success', 1500);
}
