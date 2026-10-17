// 仪表盘页面
// 顶部"启动新实例"按钮 + 实例卡片网格
// 数据源：实例状态中心（WS 推送 + 轮询兜底），预设列表按需刷新

import { t, localizeBackendError } from './i18n.mjs';
import { emptyStateHtml } from './panel_views.mjs';
import { confirmLaunch, showDiagnosisModal } from './preflight.mjs';

export const id = 'dashboard';
export const title = '仪表盘';

let ctx = null;
let root = null;
let unsubscribe = null;
let presetCache = [];
let currentInstances = [];
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
        <div class="page-title">${ctx.escapeHtml(t('dash.title'))}</div>
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
  await Promise.all([refreshPresets(), refreshSetupState()]);
  unsubscribe = ctx.subscribeInstances(render);
}

export function onHide() {
  if (unsubscribe) unsubscribe();
  unsubscribe = null;
}

async function refreshSetupState() {
  try {
    const cfg = await ctx.Api.getSystemConfig();
    serverConfigured = Boolean(cfg && String(cfg.llama_server_path || '').trim());
    modelDirConfigured = Boolean(cfg && String(cfg.default_model_dir || '').trim());
  } catch (e) {
    // 读不到配置就不提示 —— 渲染层已有专门的诊断空态，别在这里叠一条假警告
    serverConfigured = true;
    modelDirConfigured = true;
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
      <div class="empty-state" style="grid-column: 1 / -1;">
        <div class="empty-icon">⚠️</div>
        <div>${ctx.escapeHtml(t('dash.debugTitle'))}</div>
        <div style="margin-top:8px;font-size:11px;color:var(--color-text-secondary);max-width:800px;word-break:break-all;text-align:left;">
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
    grid.innerHTML = `
      <div style="grid-column: 1 / -1;">
        ${emptyStateHtml(ctx, {
          icon: 'cpu',
          title: t('dash.empty'),
          sub: serverConfigured ? '' : t('dash.emptyNoServer'),
        })}
      </div>
    `;
    return;
  }
  grid.innerHTML = instances.map((i) => cardHtml(i)).join('');
  grid.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => handleAction(btn.dataset.action, btn.dataset.id));
  });
}

function presetLabel(presetName) {
  return presetName || t('dash.customPreset');
}

function cardHtml(i) {
  const actions = [];
  if (i.status === 'starting' || i.status === 'running' || i.status === 'stopping') {
    actions.push(`<button class="btn btn-sm btn-danger" data-action="stop" data-id="${i.id}">${ctx.escapeHtml(t('dash.stop'))}</button>`);
    actions.push(`<button class="btn btn-sm btn-secondary" data-action="restart" data-id="${i.id}">${ctx.escapeHtml(t('dash.restart'))}</button>`);
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
        <div class="instance-name">${ctx.escapeHtml(i.name || ctx.basename(i.model_path) || i.id)}</div>
        ${ctx.statusBadge(i.status)}
      </div>
      <div class="instance-meta">
        <div>${ctx.escapeHtml(t('dash.port'))}: <span class="text-mono">${i.port || '-'}</span> · PID: <span class="text-mono">${i.pid || '-'}</span></div>
        <div>${ctx.escapeHtml(t('dash.model'))}: ${ctx.escapeHtml(ctx.basename(i.model_path) || '-')}</div>
        <div>${ctx.escapeHtml(t('dash.preset'))}: ${ctx.escapeHtml(presetLabel(i.preset_name))}</div>
        ${i.ever_ready === false ? '' : `<div>${ctx.escapeHtml(t('dash.duration'))}: ${ctx.formatDuration(i.duration_seconds)}</div>`}
        ${i.error_message ? `<div style="color: var(--color-danger);">${ctx.escapeHtml(t('dash.error'))}: ${ctx.escapeHtml(localizeBackendError(i.error_message))}</div>` : ''}
      </div>
      <div class="instance-actions">${actions.join('')}</div>
    </div>
  `;
}

async function handleAction(action, id) {
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
  ctx.navigate('logs');
}

async function showCommandModal(id) {
  try {
    const cmd = await ctx.Api.getInstanceCommand(id);
    const body = `
      <pre class="text-mono" style="background: #0F172A; color: #E2E8F0; padding: 12px; border-radius: 8px; font-size: 12px; white-space: pre-wrap; word-break: break-all;">${ctx.escapeHtml(cmd.command)}</pre>
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

function openLaunchModal() {
  // 表单内容：可选预设下拉 + 端口覆盖（必填） + 实例名（可选）
  const presetOptions = presetCache
    .map((p) => `<option value="${ctx.escapeHtml(String(p.id))}">${ctx.escapeHtml(presetName(p))}</option>`)
    .join('');
  const body = `
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('dash.launchPreset'))}</label>
      <select class="select" id="launch-preset">
        <option value="">${ctx.escapeHtml(t('dash.noPreset'))}</option>
        ${presetOptions}
      </select>
      <div class="form-help">${ctx.escapeHtml(t('dash.launchPresetHelp'))}</div>
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('dash.launchPort'))} <span class="required">*</span></label>
      <input type="number" class="input" id="launch-port" placeholder="${ctx.escapeHtml(t('dash.launchPortPh'))}" />
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('dash.launchModel'))}</label>
      <input type="text" class="input" id="launch-model" placeholder="D:\\models\\xxx.gguf" />
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('dash.launchName'))}</label>
      <input type="text" class="input" id="launch-name" placeholder="${ctx.escapeHtml(t('dash.namePh'))}" />
    </div>
  `;
  ctx.showModal(t('dash.launchTitle'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('dash.start'),
      class: 'btn-primary',
      keepOpen: true,
      onClick: (overlay, bodyEl) => {
        doLaunch(bodyEl);
        return false; // 不自动关闭，由 doLaunch 控制关闭
      },
    },
  ]);
}

async function doLaunch(bodyEl) {
  const presetId = bodyEl.querySelector('#launch-preset').value;
  const port = bodyEl.querySelector('#launch-port').value.trim();
  const model = bodyEl.querySelector('#launch-model').value.trim();
  const name = bodyEl.querySelector('#launch-name').value.trim();

  const overrides = {};
  // 端口留空 → 0 = 自动分配可用端口；显式输入则用用户指定值
  if (port) overrides.port = parseInt(port, 10);
  else overrides.port = 0;
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

  if (!(await confirmLaunch(ctx, body))) return;

  try {
    const inst = await ctx.Api.createInstance(body);
    ctx.showToast(t('dash.launched', { name: inst.name, port: inst.port }), 'success');
    ctx.closeModal();
  } catch (e) {
    ctx.showToast(`${t('dash.launchFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}
