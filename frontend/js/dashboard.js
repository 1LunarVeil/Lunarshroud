// 仪表盘页面
// 顶部"启动新实例"按钮 + 实例卡片网格
// 数据源：实例状态中心（WS 推送 + 轮询兜底），预设列表按需刷新

import { t, localizeBackendError } from './i18n.mjs';
import { createRequestGate, setButtonBusy } from './runtime.mjs';
import { emptyStateHtml } from './panel_views.mjs';
import { hasUsableServer } from './configured_state.mjs';
import { confirmLaunch, showDiagnosisModal } from './preflight.mjs';

export const id = 'dashboard';
export const title = '仪表盘';

let ctx = null;
let root = null;
let unsubscribe = null;
const showGate = createRequestGate();
let presetCache = [];
let currentInstances = [];
let overviewBases = null;
// 全新安装时 llama_server_path 是空的（默认值刻意留空，见 backend/config.py），
// 此时"没有实例"并不是用户做错了什么 —— 空态必须说清楚下一步去哪。
let serverConfigured = true;
let modelDirConfigured = true;

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('dash.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('dash.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-primary" id="btn-launch">${ctx.escapeHtml(t('dash.launch'))}</button>
      </div>
    </div>
    <div id="setup-banner"></div>
    <div id="instance-grid" class="instance-grid"></div>
  `;
  root.querySelector('#btn-launch').addEventListener('click', openLaunchModal);
}

export async function onShow() {
  const request = showGate.begin();
  await Promise.all([refreshPresets(), refreshSetupState(), refreshOverview()]);
  // The user may have navigated away while config/presets were loading.
  // Subscribing then would keep a hidden page rendering and leak a listener.
  if (!showGate.isCurrent(request)) return;
  if (unsubscribe) unsubscribe();
  unsubscribe = ctx.subscribeInstances(render);
}

export function onHide() {
  showGate.cancel();
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
}

async function refreshSetupState() {
  try {
    const cfg = await ctx.Api.getSystemConfig();
    // 只看全局路径会把"只用运行环境"的正常用户判成没配置，横幅会一直挂着。
    serverConfigured = hasUsableServer({
      serverPath: cfg && cfg.llama_server_path,
      runtimes: await listRuntimesQuietly(),
    });
    modelDirConfigured = Boolean(cfg && String(cfg.default_model_dir || '').trim());
  } catch (e) {
    // 读不到配置就不提示 —— 渲染层已有专门的诊断空态，别在这里叠一条假警告
    serverConfigured = true;
    modelDirConfigured = true;
  }
}

/** 运行环境列表拿不到时返回空数组，让上层按"未配置"处理，而不是抛错打断渲染。 */
async function listRuntimesQuietly() {
  try {
    const data = await ctx.Api.listRuntimes();
    return (data && data.runtimes) || [];
  } catch {
    return [];
  }
}

function renderSetupBanner() {
  const slot = root.querySelector('#setup-banner');
  if (!slot) return;
  if (serverConfigured && modelDirConfigured) {
    slot.innerHTML = '';
    return;
  }
  const key = serverConfigured ? 'setup.modelsTitle' : 'setup.title';
  const bodyKey = serverConfigured ? 'setup.modelsBody' : 'setup.body';
  slot.innerHTML = `
    <div class="setup-banner">
      <div class="setup-banner-text">
        <strong>${ctx.escapeHtml(t(key))}</strong>
        <div>${ctx.escapeHtml(t(bodyKey))}</div>
      </div>
      <div class="setup-banner-actions">
        <button class="btn btn-secondary btn-sm" data-setup="guide">${ctx.escapeHtml(t('setup.openGuide'))}</button>
        <button class="btn btn-primary btn-sm" data-setup="settings">${ctx.escapeHtml(t('setup.openSettings'))}</button>
      </div>
    </div>`;
  slot.querySelector('[data-setup="guide"]').addEventListener('click', () => ctx.navigate('help'));
  slot.querySelector('[data-setup="settings"]').addEventListener('click', () => ctx.navigate('settings'));
}

async function refreshPresets() {
  try {
    presetCache = await ctx.Api.listPresets();
  } catch (e) {
    ctx.showToast(`${t('dash.presetLoadFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

async function refreshOverview() {
  try {
    const response = await ctx.Api.listKnowledge();
    overviewBases = Array.isArray(response?.bases) ? response.bases.length : 0;
  } catch {
    // 概览是装饰性信息：读不到就不显示那一行，而不是显示 0 骗人。
    overviewBases = null;
  }
}

// 空实例时的首屏。此前这里只有一个图标加一句"暂无实例"，用户面对整页空白，
// 而下一步该点什么完全要靠猜 —— 空态应该把动作直接摆出来。
function firstRunHtml() {
  const rows = [
    {
      key: t('dash.stateServer'),
      value: serverConfigured ? t('dash.stateReady') : t('dash.stateMissing'),
      tone: serverConfigured ? 'ok' : 'warn',
    },
    { key: t('dash.statePresets'), value: String((presetCache && presetCache.length) || 0) },
  ];
  if (overviewBases !== null) {
    rows.push({ key: t('dash.stateBases'), value: String(overviewBases) });
  }
  return `
    <div grid-full class="dash-start">
      <section class="card dash-start-actions">
        <div class="card-title">${ctx.escapeHtml(t('dash.startTitle'))}</div>
        <p class="card-subtitle">${ctx.escapeHtml(t('dash.startHelp'))}</p>
        <div class="dash-start-buttons">
          <button class="btn btn-primary" data-start="pick">${ctx.escapeHtml(t('dash.startPick'))}</button>
          <button class="btn btn-secondary" data-start="library">${ctx.escapeHtml(t('dash.startLibrary'))}</button>
          <button class="btn btn-secondary" data-start="knowledge">${ctx.escapeHtml(t('dash.startKnowledge'))}</button>
        </div>
      </section>
      <section class="card dash-start-state">
        <div class="card-title">${ctx.escapeHtml(t('dash.stateTitle'))}</div>
        <dl class="dash-state-list">
          ${rows.map((row) => `
            <div class="dash-state-row">
              <dt>${ctx.escapeHtml(row.key)}</dt>
              <dd class="${row.tone ? `is-${row.tone}` : ''}">${ctx.escapeHtml(row.value)}</dd>
            </div>`).join('')}
        </dl>
      </section>
    </div>`;
}

function wireFirstRun(scope) {
  const buttons = {
    pick: () => ctx.openModelPicker(),
    library: () => ctx.navigate('workspace'),
    knowledge: () => ctx.navigate('knowledge'),
  };
  scope.querySelectorAll('[data-start]').forEach((button) => {
    button.addEventListener('click', () => {
      const run = buttons[button.dataset.start];
      if (run) run();
    });
  });
}

function render(instances) {
  const grid = root.querySelector('#instance-grid');
  currentInstances = Array.isArray(instances) ? instances : [];
  renderSetupBanner();
  // 防御性检查：确保 instances 是数组
  if (!Array.isArray(instances)) {
    console.error('[dashboard] instances is not an array:', instances);
    const debugInfo = typeof instances === 'object' ? JSON.stringify(instances) : String(instances);
    const preview = debugInfo.length > 300 ? debugInfo.slice(0, 300) + '...' : debugInfo;
    const backendUrl = window.__LLAMA_BACKEND_URL__ || t('dash.debugUnset');
    const lastUrl = window.__LLAMA_LAST_REQUEST_URL__ || t('dash.debugNoReq');
    const tauriAvail = window.__TAURI__ ? 'yes' : 'no';
    const tauriError = window.__LLAMA_BACKEND_URL_ERROR__ || '';
    grid.innerHTML = `
      <div class="empty-state" grid-full>
        <div class="empty-icon">⚠️</div>
        <div>${ctx.escapeHtml(t('dash.debugTitle'))}</div>
        <div class="launch-hint">
          <div>${ctx.escapeHtml(t('dash.debugType'))}: ${typeof instances}</div>
          <div>TAURI: ${tauriAvail} ${tauriError ? '· ' + ctx.escapeHtml(t('dash.debugError')) + ': ' + ctx.escapeHtml(tauriError) : ''}</div>
          <div>${ctx.escapeHtml(t('dash.debugBackend'))}: ${ctx.escapeHtml(backendUrl)}</div>
          <div>${ctx.escapeHtml(t('dash.debugReq'))}: ${ctx.escapeHtml(lastUrl)}</div>
          <div>${ctx.escapeHtml(t('dash.debugResp'))}: ${ctx.escapeHtml(preview)}</div>
        </div>
      </div>
    `;
    return;
  }
  if (!instances || instances.length === 0) {
    grid.innerHTML = firstRunHtml();
    wireFirstRun(grid);
    return;
  }
  grid.innerHTML = instances.map((i) => cardHtml(i)).join('');
  grid.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => handleAction(btn.dataset.action, btn.dataset.id, btn));
  });
}

function presetLabel(presetName) {
  return presetName || t('dash.customPreset');
}

function cardHtml(i) {
  const actions = [];
  // 知识库的嵌入/重排/OCR 服务也是 llama-server 实例，但它们是系统按需拉起
  // 并会自动卸载的后台服务。不标出来的话，用户会以为是自己启动的模型，
  // 还会去点"停止"。
  const isKnowledgeService = String(i.name || '').startsWith('[KB] ');
  if (i.status === 'starting' || i.status === 'running' || i.status === 'stopping') {
    actions.push(`<button class="btn btn-sm btn-danger" data-action="stop" data-id="${i.id}">${ctx.escapeHtml(t('dash.stop'))}</button>`);
    actions.push(`<button class="btn btn-sm btn-secondary" data-action="restart" data-id="${i.id}">${ctx.escapeHtml(t('dash.restart'))}</button>`);
  }
  if (i.status === 'running') {
    actions.push(`<button class="btn btn-sm btn-primary" data-action="chat" data-id="${i.id}">${ctx.escapeHtml(t('dash.chat'))}</button>`);
  }
  actions.push(`<button class="btn btn-sm btn-secondary" data-action="logs" data-id="${i.id}">${ctx.escapeHtml(t('dash.logs'))}</button>`);
  actions.push(`<button class="btn btn-sm btn-secondary" data-action="command" data-id="${i.id}">${ctx.escapeHtml(t('dash.command'))}</button>`);
  if (i.status === 'failed') {
    actions.push(`<button class="btn btn-sm btn-warning" data-action="diagnose" data-id="${i.id}">${ctx.escapeHtml(t('diag.whyFailed'))}</button>`);
  }
  if (i.status === 'stopped' || i.status === 'failed') {
    actions.push(`<button class="btn btn-sm btn-ghost" data-action="remove" data-id="${i.id}">${ctx.escapeHtml(t('dash.remove'))}</button>`);
  }
  return `
    <div class="instance-card">
      <div class="flex items-center justify-between">
        <div class="instance-name">${ctx.escapeHtml(i.name || ctx.basename(i.model_path) || i.id)}${
          isKnowledgeService ? `<span class="instance-kind-badge">${ctx.escapeHtml(t('dash.knowledgeService'))}</span>` : ''
        }</div>
        ${ctx.statusBadge(i.status)}
      </div>
      <div class="instance-meta">
        <div>${ctx.escapeHtml(t('dash.port'))}: <span class="text-mono">${i.port || '-'}</span> · PID: <span class="text-mono">${i.pid || '-'}</span></div>
        <div>${ctx.escapeHtml(t('dash.model'))}: ${ctx.escapeHtml(ctx.basename(i.model_path) || '-')}</div>
        <div>${ctx.escapeHtml(t('dash.preset'))}: ${ctx.escapeHtml(presetLabel(i.preset_name))}</div>
        ${i.runtime_label || i.runtime_id ? `<div>${ctx.escapeHtml(t('dash.runtime'))}: ${ctx.escapeHtml(i.runtime_label || i.runtime_id)}</div>` : ''}
        ${i.ever_ready === false ? '' : `<div>${ctx.escapeHtml(t('dash.duration'))}: ${ctx.formatDuration(i.duration_seconds)}</div>`}
        ${i.error_message ? `<div class="text-danger">${ctx.escapeHtml(t('dash.error'))}: ${ctx.escapeHtml(localizeBackendError(i.error_message))}</div>` : ''}
        ${Array.isArray(i.warnings) && i.warnings.length
          ? i.warnings
              .map(
                (warning) =>
                  `<div class="text-warning">${ctx.escapeHtml(t('dash.warning'))}: ${ctx.escapeHtml(localizeBackendError(warning))}</div>`
              )
              .join('')
          : ''}
      </div>
      <div class="instance-actions">${actions.join('')}</div>
    </div>
  `;
}

async function handleAction(action, id, button) {
  const mutating = action === 'stop' || action === 'restart' || action === 'remove';
  if (mutating) setButtonBusy(button, true);
  try {
    await handleActionInner(action, id);
  } finally {
    if (mutating) setButtonBusy(button, false);
  }
}

async function handleActionInner(action, id) {
  switch (action) {
    case 'stop':
      if (!(await ctx.showConfirm(t('dash.confirmStop')))) return;
      try {
        await ctx.Api.stopInstance(id, false);
        ctx.showToast(t('dash.stopSent'), 'info');
      } catch (e) {
        ctx.showToast(`${t('dash.stopFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    case 'restart':
      if (!(await ctx.showConfirm(t('dash.confirmRestart')))) return;
      try {
        await ctx.Api.restartInstance(id);
        ctx.showToast(t('dash.restartSent'), 'info');
      } catch (e) {
        ctx.showToast(`${t('dash.restartFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    case 'chat':
      window.__pendingChatInstanceId = id;
      ctx.navigate('chat');
      break;
    case 'logs':
      navigateToLogs(id);
      break;
    case 'command':
      showCommandModal(id);
      break;
    case 'diagnose': {
      const instance = currentInstances.find((item) => item.id === id);
      if (instance) await showDiagnosisModal(ctx, instance, instance.diagnosis);
      break;
    }
    case 'remove':
      if (!(await ctx.showConfirm(t('dash.confirmRemove')))) return;
      try {
        await ctx.Api.removeInstance(id);
        ctx.showToast(t('dash.removed'), 'success');
      } catch (e) {
        ctx.showToast(`${t('dash.removeFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
  }
}

function navigateToLogs(instanceId) {
  // 先把目标实例暂存到全局（logs 的 onShow 会读取此值）
  window.__pendingLogsInstanceId = instanceId;
  window.__pendingObservabilityTab = 'logs';
  ctx.navigate('observability');
}

async function showCommandModal(id) {
  try {
    const cmd = await ctx.Api.getInstanceCommand(id);
    const body = `
      <pre class="text-mono code-preview">${ctx.escapeHtml(cmd.command)}</pre>
    `;
    ctx.showModal(t('dash.cmdTitle'), body, [
      { label: t('app.close'), class: 'btn-secondary' },
      { label: t('dash.copy'), class: 'btn-primary', onClick: () => ctx.copyToClipboard(cmd.command) },
    ]);
  } catch (e) {
    ctx.showToast(`${t('dash.getCmdFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

function presetName(p) {
  const locale = document.documentElement.lang || 'zh-CN';
  return (locale === 'en-US' && p.name_en) ? p.name_en : p.name;
}

async function openLaunchModal() {
  // 表单内容：可选预设下拉 + 运行环境 + 端口覆盖（必填） + 实例名（可选）
  // 预设可能在参数编辑器里刚保存/删除；每次打开都重读，避免下拉停留在旧快照。
  await refreshPresets();
  const presetOptions = presetCache
    .map((p) => `<option value="${ctx.escapeHtml(String(p.id))}">${ctx.escapeHtml(presetName(p))}</option>`)
    .join('');
  let runtimeOptions = `<option value="">${ctx.escapeHtml(t('params.runtimeDefault'))}</option>`;
  try {
    const payload = await ctx.Api.listRuntimes();
    const profiles = Array.isArray(payload && payload.runtimes) ? payload.runtimes : [];
    runtimeOptions += profiles
      .filter((profile) => profile.status === 'ok')
      .map(
        (profile) =>
          `<option value="${ctx.escapeHtml(String(profile.id))}">${ctx.escapeHtml(
            profile.display_name || profile.id
          )}</option>`
      )
      .join('');
  } catch {
    // 运行环境列表拉不到就保持"默认"——后端仍会使用设置里的 llama-server 路径
  }
  const body = `
    <div class="form-group">
      <label class="form-label" for="launch-preset">${ctx.escapeHtml(t('dash.launchPreset'))}</label>
      <select class="select" id="launch-preset" aria-describedby="launch-preset-help">
        <option value="">${ctx.escapeHtml(t('dash.noPreset'))}</option>
        ${presetOptions}
      </select>
      <div class="form-help" id="launch-preset-help">${ctx.escapeHtml(t('dash.launchPresetHelp'))}</div>
    </div>
    <div class="form-group">
      <label class="form-label" for="launch-runtime">${ctx.escapeHtml(t('dash.launchRuntime'))}</label>
      <select class="select" id="launch-runtime" aria-describedby="launch-runtime-help">
        ${runtimeOptions}
      </select>
      <div class="form-help" id="launch-runtime-help">${ctx.escapeHtml(t('dash.launchRuntimeHelp'))}</div>
    </div>
    <div class="form-group">
      <label class="form-label" for="launch-port">${ctx.escapeHtml(t('dash.launchPort'))}</label>
      <input type="number" class="input" id="launch-port" placeholder="${ctx.escapeHtml(t('dash.launchPortPh'))}" aria-describedby="launch-port-help" min="1" max="65535" />
      <div class="form-help" id="launch-port-help">${ctx.escapeHtml(t('dash.launchPortHelp'))}</div>
    </div>
    <div class="form-group">
      <label class="form-label" for="launch-model">${ctx.escapeHtml(t('dash.launchModel'))}</label>
      <input type="text" class="input" id="launch-model" placeholder="D:\\models\\xxx.gguf" />
    </div>
    <div class="form-group">
      <label class="form-label" for="launch-name">${ctx.escapeHtml(t('dash.launchName'))}</label>
      <input type="text" class="input" id="launch-name" placeholder="${ctx.escapeHtml(t('dash.namePh'))}" />
    </div>
  `;
  ctx.showModal(t('dash.launchTitle'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('dash.start'),
      class: 'btn-primary',
      keepOpen: true,
      onClick: (overlay, bodyEl, button) => {
        doLaunch(bodyEl, button);
        return false; // 不自动关闭，由 doLaunch 控制关闭
      },
    },
  ]);
}

async function doLaunch(bodyEl, button) {
  if (button && button.disabled) return;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  }
  try {
    const presetId = bodyEl.querySelector('#launch-preset').value;
    const runtimeId = bodyEl.querySelector('#launch-runtime')?.value || '';
    const port = bodyEl.querySelector('#launch-port').value.trim();
    const model = bodyEl.querySelector('#launch-model').value.trim();
    const name = bodyEl.querySelector('#launch-name').value.trim();

    const overrides = {};
    // 端口留空 → 0 = 自动分配可用端口；显式输入则用用户指定值
    if (port) {
      const parsedPort = Number(port);
      if (!Number.isInteger(parsedPort) || parsedPort < 1 || parsedPort > 65535) {
        ctx.showToast(t('settings.portRangeWarn'), 'warning');
        return;
      }
      overrides.port = parsedPort;
    } else {
      overrides.port = 0;
    }
    if (model) overrides.model = model;

    const body = { param_overrides: overrides };
    if (presetId) {
      body.preset_id = presetId;
    } else {
      // 不选预设时加载默认参数（后端拒绝空 params）
      try {
        const defaults = await ctx.Api.getParamDefaults();
        body.params = defaults || {};
      } catch (e) {
        ctx.showToast(`${t('dash.defaultsFailed')}: ${localizeBackendError(e.message)}`, 'error');
        return;
      }
    }
    if (name) body.name = name;
    // 留空 = 交给后端决定（预设固定的 runtime > 默认偏好 > 设置里的路径）
    if (runtimeId) body.runtime_id = runtimeId;

    if (!(await confirmLaunch(ctx, body))) return;

    try {
      const inst = await ctx.Api.createInstance(body);
      ctx.showToast(t('dash.launched', { name: inst.name, port: inst.port }), 'success');
      ctx.closeModal();
    } catch (e) {
      ctx.showToast(`${t('dash.launchFailed')}: ${localizeBackendError(e.message)}`, 'error');
    }
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }
}
