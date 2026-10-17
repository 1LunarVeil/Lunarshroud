// Chat 页面
// 直接嵌入 llama-server 内置 Web UI（继承 search/history/MCP/settings/文件上传等全部功能）
// 通过 iframe 加载运行中实例的 HTTP 服务；数据源为实例状态中心（WS 推送）

import { isLoopbackHost, normalizeHttpUrl } from './runtime.mjs';
import { t } from './i18n.mjs';

export const id = 'chat';
export const title = 'Chat';

let ctx = null;
let root = null;

let instances = [];
let selectedInstanceId = '';
let unsubscribe = null;

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  root.innerHTML = `
    <div class="chat-page-v2">
      <div class="chat-toolbar">
        <div class="chat-toolbar-left">
          <div class="chat-instance-selector">
            <span class="chat-instance-label">${ctx.escapeHtml(t('chat.instance'))}</span>
            <select id="chat-instance-select" class="form-control form-control-sm">
              <option value="">${ctx.escapeHtml(t('chat.loading'))}</option>
            </select>
          </div>
          <div class="chat-instance-info" id="chat-instance-info"></div>
        </div>
        <div class="chat-toolbar-right">
          <button class="btn btn-ghost btn-sm" id="btn-chat-refresh" title="${ctx.escapeHtml(t('chat.refresh'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="23 4 23 10 17 10"/><polyline points="1 20 1 14 7 14"/><path d="M3.51 9a9 9 0 0 1 14.85-3.36L23 10M1 14l4.64 4.36A9 9 0 0 0 20.49 15"/></svg>
          </button>
          <button class="btn btn-ghost btn-sm" id="btn-chat-external" title="${ctx.escapeHtml(t('chat.openExternal'))}">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
          </button>
        </div>
      </div>
      <div class="chat-frame-container" id="chat-frame-container">${emptyHtml()}</div>
    </div>
  `;
  bindEvents();
}

function emptyHtml() {
  return `
    <div class="chat-empty" id="chat-empty">
      <svg width="64" height="64" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="chat-empty-icon">
        <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
      </svg>
      <div class="chat-empty-title">${ctx.escapeHtml(t('chat.emptyTitle'))}</div>
      <div class="chat-empty-hint">${ctx.escapeHtml(t('chat.emptyHint'))}</div>
      <div class="chat-empty-actions">
        <button class="btn btn-primary btn-sm" id="chat-goto-dashboard">${ctx.escapeHtml(t('chat.gotoDashboard'))}</button>
        <button class="btn btn-secondary btn-sm" id="chat-reload-last">${ctx.escapeHtml(t('chat.reloadLast'))} <span class="kbd">Ctrl+R</span></button>
      </div>
    </div>
  `;
}

/** 非回环实例：iframe 必然空白，直接说明原因并给外部打开 */
function externalOnlyHtml(inst) {
  return `
    <div class="chat-empty" id="chat-external-only">
      <svg width="56" height="56" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" class="chat-empty-icon">
        <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
        <polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/>
      </svg>
      <div class="chat-empty-title">${ctx.escapeHtml(t('chat.externalOnlyTitle'))}</div>
      <div class="chat-empty-hint">${ctx.escapeHtml(t('chat.externalOnlyReason', { host: inst.host }))}</div>
      <button class="btn btn-primary btn-sm" id="chat-open-external">${ctx.escapeHtml(t('chat.openInBrowser'))}</button>
    </div>
  `;
}

export function onShow() {
  unsubscribe = ctx.subscribeInstances(updateInstances);
  document.addEventListener('keydown', onKeydown);
}

export function onHide() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
  document.removeEventListener('keydown', onKeydown);
}

/**
 * `Ctrl+R` = 重载上次使用的模型，**只在空态生效**。
 *
 * 不做成全局快捷键是刻意的：Ctrl+R 是浏览器/WebView 的刷新键，在有实例可用时
 * 抢它会让"想刷新页面"变成"又拉起一个模型"。
 */
function onKeydown(event) {
  if (!(event.ctrlKey || event.metaKey) || event.key !== 'r') return;
  // 契约 §4.2：空态判定用 #chat-empty 是否在 DOM 里，而不是数 instances ——
  // instances 来自 WS 推送缓存，刚停止实例时会短暂滞后；且空态卡片上
  // 明摆着 "Ctrl+R" 按钮，键盘判定必须与可见的空态 UI 一致。
  if (!root.querySelector('#chat-empty')) return;
  if (document.querySelector('#app-modal-overlay')) return;
  event.preventDefault();
  relaunchLastModel();
}

/** 用最近一条启动历史重新拉起（后端已有 relaunch 接口，不重复实现启动逻辑） */
async function relaunchLastModel() {
  let records = [];
  try {
    records = await ctx.Api.listHistory(1, 0);
  } catch (e) {
    ctx.showToast(`${t('chat.reloadFailed')}: ${String(e && e.message ? e.message : e)}`, 'error');
    return;
  }
  const last = Array.isArray(records) ? records[0] : null;
  if (!last) {
    ctx.showToast(t('chat.reloadNone'), 'info');
    return;
  }
  // 用文件名而不是整条路径做提示：路径会把 toast 撑成一整行
  const label = String(last.model_path || '').split(/[\\/]/).pop() || last.preset_name || '';
  try {
    await ctx.Api.relaunchHistory(last.id);
    ctx.showToast(t('chat.reloading', { name: label }), 'success');
  } catch (e) {
    ctx.showToast(`${t('chat.reloadFailed')}: ${String(e && e.message ? e.message : e)}`, 'error');
  }
}

function bindEvents() {
  root.querySelector('#chat-instance-select').addEventListener('change', (e) => {
    selectedInstanceId = e.target.value;
    loadInstanceUI();
  });

  root.querySelector('#btn-chat-refresh').addEventListener('click', () => {
    const iframe = root.querySelector('#chat-iframe');
    if (iframe) {
      iframe.src = iframe.src;
      ctx.showToast(t('chat.refreshed'), 'info', 1500);
    }
  });

  root.querySelector('#btn-chat-external').addEventListener('click', () => {
    const inst = instances.find((i) => i.id === selectedInstanceId);
    if (inst) {
      const safeUrl = normalizeHttpUrl(inst.url);
      if (safeUrl) window.open(safeUrl, '_blank', 'noopener,noreferrer');
    }
  });
}

// 后端返回 host 可能是通配地址，浏览器访问需转回环
function browserHost(inst) {
  return inst.host === '0.0.0.0' ? '127.0.0.1' : inst.host;
}

function updateInstances(list) {
  instances = (Array.isArray(list) ? list : [])
    .filter((i) => i.status === 'running')
    .map((i) => {
      const host = browserHost(i);
      return {
        ...i,
        url: `http://${host}:${i.port}`,
        // 回环才嵌得进 iframe（CSP 只允许 127.0.0.1，且非回环要 API Key）
        loopback: isLoopbackHost(host),
      };
    });

  const select = root.querySelector('#chat-instance-select');
  const prev = selectedInstanceId;

  if (instances.length === 0) {
    select.innerHTML = `<option value="">${ctx.escapeHtml(t('chat.noRunning'))}</option>`;
    showEmpty();
    selectedInstanceId = '';
    return;
  }

  select.replaceChildren();
  for (const instance of instances) {
    const option = document.createElement('option');
    option.value = String(instance.id || '');
    option.textContent = `${instance.name || ''} · ${instance.host}:${instance.port}`;
    select.appendChild(option);
  }

  if (prev && instances.find((i) => i.id === prev)) {
    select.value = prev;
  } else {
    selectedInstanceId = instances[0].id;
    select.value = selectedInstanceId;
    loadInstanceUI();
  }

  updateInstanceInfo();
}

function updateInstanceInfo() {
  const inst = instances.find((i) => i.id === selectedInstanceId);
  const infoEl = root.querySelector('#chat-instance-info');
  if (inst) {
    const modelName = String(inst.model_path || '').split(/[\\\/]/).pop();
    infoEl.textContent = modelName;
  }
}

function loadInstanceUI() {
  const inst = instances.find((i) => i.id === selectedInstanceId);
  if (!inst) {
    showEmpty();
    return;
  }
  if (!inst.loopback) {
    // 嵌进去只会得到一片空白，而且用户无从知道为什么 —— 直接说明并给出口
    showExternalOnly(inst);
    return;
  }

  const container = root.querySelector('#chat-frame-container');
  const safeUrl = normalizeHttpUrl(inst.url);
  if (!safeUrl) {
    showEmpty();
    ctx.showToast(t('chat.invalidUrl'), 'error');
    return;
  }
  const iframe = document.createElement('iframe');
  iframe.id = 'chat-iframe';
  iframe.src = safeUrl;
  iframe.className = 'chat-iframe';
  iframe.setAttribute('frameborder', '0');
  // 沙箱化跨源 WebUI（审计 F11）：只放行脚本、表单与"自身源"（localStorage /
  // 同源 fetch 依赖它），顶层导航、弹窗、下载等一律关闭。
  // clipboard-read 一并移除：复制按钮只需要 write，跨源帧"读剪贴板"是超范围权限。
  iframe.setAttribute('sandbox', 'allow-scripts allow-forms allow-same-origin');
  iframe.setAttribute('allow', 'clipboard-write');
  iframe.referrerPolicy = 'no-referrer';
  container.replaceChildren(iframe);

  updateInstanceInfo();
}

function showEmpty() {
  const container = root.querySelector('#chat-frame-container');
  container.innerHTML = emptyHtml();
  container.querySelector('#chat-goto-dashboard').addEventListener('click', () => {
    ctx.navigate('dashboard');
  });
  container.querySelector('#chat-reload-last').addEventListener('click', relaunchLastModel);
}

function showExternalOnly(inst) {
  const container = root.querySelector('#chat-frame-container');
  container.innerHTML = externalOnlyHtml(inst);
  const btn = container.querySelector('#chat-open-external');
  if (btn) {
    btn.addEventListener('click', () => {
      const safeUrl = normalizeHttpUrl(inst.url);
      if (safeUrl) window.open(safeUrl, '_blank', 'noopener,noreferrer');
      else ctx.showToast(t('chat.invalidUrl'), 'error');
    });
  }
}
