// 运行环境页面：用户自选的 llama-server 构建（只读元数据 + 探测）。
// 这里不下载、不更新、不启动任何进程；注册与探测都交给后端注册表。

import { tauriInvoke } from './api.js?v=1';
import { t, localizeBackendError } from './i18n.mjs';
import { emptyStateHtml } from './panel_views.mjs';

export const id = 'runtimes';
export const title = '运行环境';

let ctx = null;
let root = null;
let profiles = [];
let preferences = {};
let selectedId = null;
let busy = false;
let loadError = '';

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('runtimes.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('runtimes.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-primary" id="btn-add-runtime">${ctx.escapeHtml(t('runtimes.add'))}</button>
      </div>
    </div>
    <div id="runtimes-body"><div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(t('runtimes.loading'))}</div></div>
  `;
  root.querySelector('#btn-add-runtime').addEventListener('click', () => addRuntime());
  root.querySelector('#runtimes-body').addEventListener('click', (event) => {
    const button = event.target.closest('[data-action]');
    if (!button) return;
    handleAction(button.dataset.action, button.dataset.id);
  });
}

export function onShow() {
  refresh();
}

export function onHide() {}

async function refresh() {
  loadError = '';
  try {
    const payload = await ctx.Api.listRuntimes();
    profiles = Array.isArray(payload.runtimes) ? payload.runtimes : [];
    preferences =
      payload.preferences && typeof payload.preferences === 'object' ? payload.preferences : {};
    if (!profiles.some((item) => item.id === selectedId)) {
      selectedId = profiles.length ? profiles[0].id : null;
    }
  } catch (error) {
    profiles = [];
    loadError = localizeBackendError(error.message || String(error));
  }
  render();
}

function render() {
  const body = root.querySelector('#runtimes-body');
  if (!body) return;
  if (loadError) {
    body.innerHTML = `
      <div class="card">
        <div class="diag-item diag-error">
          <div class="diag-head"><span class="diag-severity">${ctx.escapeHtml(t('diag.severity.error'))}</span>
            <span class="diag-title">${ctx.escapeHtml(t('runtimes.loadFailed'))}</span></div>
          <div class="diag-detail">${ctx.escapeHtml(loadError)}</div>
          <div class="diag-fix"><button class="btn btn-secondary btn-sm" data-action="retry">${ctx.escapeHtml(t('runtimes.retry'))}</button></div>
        </div>
      </div>`;
    return;
  }
  if (!profiles.length) {
    body.innerHTML = `<div class="card">${emptyStateHtml(ctx, {
      icon: 'cpu',
      title: t('runtimes.empty'),
      sub: t('runtimes.emptySub'),
    })}</div>`;
    return;
  }
  body.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(t('runtimes.registered'))} · ${profiles.length}</span>
        <div class="card-actions"><span class="text-sm text-muted">${ctx.escapeHtml(t('runtimes.defaultHint'))}</span></div>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr>
            <th>${ctx.escapeHtml(t('runtimes.name'))}</th>
            <th>${ctx.escapeHtml(t('runtimes.version'))}</th>
            <th>${ctx.escapeHtml(t('runtimes.backend'))}</th>
            <th>${ctx.escapeHtml(t('runtimes.status'))}</th>
            <th>${ctx.escapeHtml(t('runtimes.actions'))}</th>
          </tr></thead>
          <tbody>
            ${profiles.map((profile) => rowHtml(profile)).join('')}
          </tbody>
        </table>
      </div>
    </div>
    <div id="runtime-detail">${detailHtml()}</div>
  `;
}

function versionText(profile) {
  const version = profile.version || t('runtimes.unknown');
  const build = profile.build ? `b${profile.build}` : '';
  if (build && String(version).includes(build)) return version;
  return [version, build].filter(Boolean).join(' · ');
}

function statusHtml(profile) {
  if (profile.status === 'ok') {
    return `<span class="badge badge-running">${ctx.escapeHtml(t('runtimes.ok'))}</span>`;
  }
  return `<span class="badge badge-failed">${ctx.escapeHtml(t('runtimes.probeFailed'))}</span>`;
}

function rowHtml(profile) {
  const isDefault = preferences.gguf === profile.id;
  const selected = profile.id === selectedId;
  return `
    <tr${selected ? ' class="runtime-row-selected"' : ''}>
      <td>
        <div class="flex items-center gap-2">
          <button type="button" class="btn btn-sm btn-ghost" data-action="select" data-id="${ctx.escapeHtml(profile.id)}">${ctx.escapeHtml(profile.display_name || profile.id)}</button>
          ${isDefault ? `<span class="tag">${ctx.escapeHtml(t('runtimes.default'))}</span>` : ''}
        </div>
      </td>
      <td class="text-mono text-sm">${ctx.escapeHtml(versionText(profile))}</td>
      <td>${ctx.escapeHtml(backendLabel(profile.backend))}</td>
      <td>${statusHtml(profile)}</td>
      <td>
        <button class="btn btn-sm btn-secondary" data-action="probe" data-id="${ctx.escapeHtml(profile.id)}"${busy ? ' disabled' : ''}>${ctx.escapeHtml(t('runtimes.probe'))}</button>
        <button class="btn btn-sm btn-secondary" data-action="default" data-id="${ctx.escapeHtml(profile.id)}"${isDefault || profile.status !== 'ok' ? ' disabled' : ''} title="${ctx.escapeHtml(profile.status === 'ok' ? t('runtimes.setDefault') : t('runtimes.defaultUnavailable'))}">${ctx.escapeHtml(t('runtimes.setDefault'))}</button>
        <button class="btn btn-sm btn-ghost" data-action="remove" data-id="${ctx.escapeHtml(profile.id)}">${ctx.escapeHtml(t('runtimes.remove'))}</button>
      </td>
    </tr>`;
}

function backendLabel(backend) {
  const family = backend && backend.family ? String(backend.family) : 'unknown';
  if (family === 'unknown') return t('runtimes.unknown');
  return family.toUpperCase();
}

/** 后端设备列表（--list-devices 的真实结果）。
 *
 * 四个官方 Windows 包（CPU/Vulkan/OpenVINO/ROCm）版本串与 --help 完全相同，
 * 只有这里能看出这份构建在本机到底能用什么加速器。空列表是**正常**结果：
 * CPU 包本来没有设备，ROCm 包在非 AMD 机器上也如实为空。
 */
function devicesHtml(profile) {
  const devices = Array.isArray(profile.devices) ? profile.devices : [];
  const family = profile.backend && profile.backend.family ? String(profile.backend.family) : '';
  if (!devices.length) {
    const hint =
      family === 'rocm'
        ? t('runtimes.noDevicesRocm')
        : family === 'openvino'
          ? t('runtimes.noDevicesOpenvino')
          : t('runtimes.noDevices');
    return `<div class="text-sm text-muted">${ctx.escapeHtml(hint)}</div>`;
  }
  return `
    <div class="table-wrap mt-2">
      <table class="table">
        <thead><tr>
          <th>${ctx.escapeHtml(t('runtimes.deviceName'))}</th>
          <th>${ctx.escapeHtml(t('runtimes.deviceDescription'))}</th>
          <th>${ctx.escapeHtml(t('runtimes.deviceMemory'))}</th>
        </tr></thead>
        <tbody>
          ${devices
            .map((device) => {
              const memory =
                device.total_mib != null
                  ? `${Math.round(device.total_mib / 1024 * 10) / 10} GiB` +
                    (device.free_mib != null
                      ? ` · ${ctx.escapeHtml(t('runtimes.deviceFree'))} ${Math.round(device.free_mib / 1024 * 10) / 10} GiB`
                      : '')
                  : '-';
              return `<tr>
                <td class="text-mono text-sm">${ctx.escapeHtml(device.name || '')}</td>
                <td class="text-sm">${ctx.escapeHtml(device.description || '')}</td>
                <td class="text-sm text-muted">${memory}</td>
              </tr>`;
            })
            .join('')}
        </tbody>
      </table>
    </div>`;
}

function detailHtml() {
  const profile = profiles.find((item) => item.id === selectedId);
  if (!profile) return '';
  const family = profile.backend && profile.backend.family ? String(profile.backend.family) : '';
  const manifest = profile.manifest || {};
  const display = manifest.display || {};
  const localized =
    (display['zh-CN'] && display['zh-CN'].description) ||
    (display.en && display.en.description) ||
    '';
  const warnings = Array.isArray(profile.warnings) ? profile.warnings : [];
  const probe = profile.probe || {};
  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(profile.display_name || profile.id)}</span>
        <div class="card-actions">${statusHtml(profile)}</div>
      </div>
      ${localized ? `<div class="text-sm text-muted mb-2">${ctx.escapeHtml(localized)}</div>` : ''}
      <div class="text-mono text-sm mb-2 break-all text-tertiary">${ctx.escapeHtml(profile.executable_path || '')}</div>
      <div class="flex gap-3 mt-2" flex-wrap>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.version'))}:</span> <span class="text-mono">${ctx.escapeHtml(versionText(profile))}</span></span>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.backend'))}:</span> ${ctx.escapeHtml(backendLabel(profile.backend))}</span>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.domains'))}:</span> ${ctx.escapeHtml((profile.domains || []).join(', ') || '-')}</span>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.formats'))}:</span> ${ctx.escapeHtml((profile.supported_model_formats || []).join(', ') || '-')}</span>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.options'))}:</span> ${ctx.escapeHtml(String(probe.option_count ?? '-'))}</span>
        <span><span class="text-muted">${ctx.escapeHtml(t('runtimes.probedAt'))}:</span> ${ctx.escapeHtml(probe.probed_at ? ctx.formatTime(new Date(probe.probed_at * 1000).toISOString()) : '-')}</span>
      </div>
      <div class="mt-3">
        <div class="card-title">${ctx.escapeHtml(t('runtimes.devices'))}</div>
        ${devicesHtml(profile)}
        ${
          Array.isArray(profile.devices) && profile.devices.length
            ? `<div class="form-help mt-2">${ctx.escapeHtml(t('runtimes.devicesHint'))}</div>`
            : ''
        }
        ${
          family === 'openvino'
            ? `<div class="form-help mt-1">${ctx.escapeHtml(t('runtimes.openvinoNote'))}</div>`
            : ''
        }
      </div>
      ${
        probe.error
          ? `<div class="diag-item diag-error mt-2"><div class="diag-detail">${ctx.escapeHtml(probe.error)}</div></div>`
          : ''
      }
      ${
        warnings.length
          ? `<div class="param-field-warn mt-2">${warnings
              .map((warning) => ctx.escapeHtml(warning))
              .join('<br/>')}</div>`
          : ''
      }
      ${
        manifest.name || manifest.version
          ? `<div class="mt-2 text-sm text-muted">${ctx.escapeHtml(t('runtimes.manifest'))}: ${ctx.escapeHtml(
              manifest.name || ''
            )} ${ctx.escapeHtml(manifest.version || '')}</div>`
          : `<div class="mt-2 text-sm text-muted">${ctx.escapeHtml(t('runtimes.noManifest'))}</div>`
      }
    </div>`;
}

async function handleAction(action, runtimeId) {
  if (!runtimeId) return;
  if (action === 'retry') {
    refresh();
    return;
  }
  if (action === 'select') {
    selectedId = runtimeId;
    render();
    return;
  }
  if (busy) return;
  const profile = profiles.find((item) => item.id === runtimeId);
  if (!profile) return;
  if (action === 'probe') {
    busy = true;
    render();
    try {
      await ctx.Api.probeRuntime(runtimeId);
      ctx.showToast(t('runtimes.probeSuccess'), 'success');
      await refresh();
    } catch (error) {
      ctx.showToast(
        `${t('runtimes.addFailed')}: ${localizeBackendError(error.message)}`,
        'error'
      );
    } finally {
      busy = false;
      render();
    }
    return;
  }
  if (action === 'default') {
    try {
      await ctx.Api.setRuntimePreferences({
        defaults: { ...preferences, gguf: runtimeId },
      });
      ctx.showToast(t('runtimes.defaultSet'), 'success');
      await refresh();
    } catch (error) {
      ctx.showToast(
        `${t('runtimes.defaultFailed')}: ${localizeBackendError(error.message)}`,
        'error'
      );
    }
    return;
  }
  if (action === 'remove') {
    if (
      !(await ctx.showConfirm(
        t('runtimes.confirmRemove', { name: profile.display_name || profile.id })
      ))
    )
      return;
    try {
      await ctx.Api.deleteRuntime(runtimeId);
      if (selectedId === runtimeId) selectedId = null;
      ctx.showToast(t('runtimes.removed'), 'success');
      await refresh();
    } catch (error) {
      ctx.showToast(
        `${t('runtimes.removeFailed')}: ${localizeBackendError(error.message)}`,
        'error'
      );
    }
  }
}

async function addRuntime() {
  if (busy) return;
  let path = null;
  try {
    if (window.__TAURI__) {
      path = await tauriInvoke('open_file_dialog');
    } else {
      path = await promptRuntimePath();
    }
  } catch (error) {
    ctx.showToast(`${t('runtimes.addFailed')}: ${String(error.message || error)}`, 'error');
    return;
  }
  if (!path) return;
  busy = true;
  const addButton = root.querySelector('#btn-add-runtime');
  if (addButton) {
    addButton.disabled = true;
    addButton.setAttribute('aria-busy', 'true');
  }
  try {
    const profile = await ctx.Api.registerRuntime({ path });
    selectedId = profile.id;
    ctx.showToast(t('runtimes.added', { name: profile.display_name || profile.id }), 'success');
    await refresh();
  } catch (error) {
    ctx.showToast(`${t('runtimes.addFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    busy = false;
    if (addButton && addButton.isConnected) {
      addButton.disabled = false;
      addButton.removeAttribute('aria-busy');
    }
    render();
  }
}

function promptRuntimePath() {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    ctx.showModal(
      t('runtimes.add'),
      `<div class="form-group">
        <label class="form-label" for="runtime-path">${ctx.escapeHtml(t('runtimes.pathPrompt'))}</label>
        <input type="text" class="input" id="runtime-path" placeholder="D:\\llama\\llama-server.exe"/>
        <div class="form-help">${ctx.escapeHtml(t('runtimes.pathPh'))}</div>
      </div>`,
      [
        { label: t('app.cancel'), class: 'btn-secondary', onClick: () => done(null) },
        {
          label: t('runtimes.add'),
          class: 'btn-primary',
          onClick: () => {
            const value = document.getElementById('runtime-path')?.value.trim();
            done(value || null);
          },
        },
      ],
      { onClose: () => done(null) }
    );
  });
}
