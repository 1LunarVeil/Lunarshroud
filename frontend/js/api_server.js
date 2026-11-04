// 本地 API 服务（OpenAI 兼容网关）。
//
// 这是 LM Studio 的 "Local Server" 页面在 LunarShroud 里的对应物，但边界更窄：
// 网关只转发到**已经在启动器里运行**的实例，不自己做模型加载策略（那由实例
// 生命周期负责）。所以这一页只回答四件事：
//
//   1. 服务开没开、开在哪个地址（一键开/关）
//   2. 端口、跨域来源、访问密钥（写进 config.json，重启也生效）
//   3. 谁在调它（请求日志，可清空、可导出）
//   4. 怎么接（curl / Python / PowerShell 片段，复制即用）
//
// 为什么状态要以后端为准而不是本地记一个布尔：网关可能因为端口被占而启动
// 失败。界面上"已开启"配一条连不上的服务，比直接显示错误更让人困惑。

import { t, localizeBackendError } from './i18n.mjs';
import { createRequestGate } from './runtime.mjs';

export const id = 'api_server';
export const title = '本地 API 服务';

let ctx = null;
let root = null;
let status = null;
let logs = [];
let recipes = null;
let pollTimer = null;
const loadGate = createRequestGate();

// 轮询间隔。请求日志是"看刚刚发生了什么"，2 秒足够；再快只会让后端为一份
// 200 条的 JSON 反复序列化。
const POLL_MS = 2000;

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('apiServer.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('apiServer.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary btn-sm" id="api-server-refresh">${ctx.escapeHtml(t('apiServer.refresh'))}</button>
      </div>
    </div>
    <div id="api-server-body"></div>
  `;
  root.querySelector('#api-server-refresh').addEventListener('click', () => refresh({ force: true }));
}

export function onShow() {
  refresh({ force: true });
  stopPolling();
  pollTimer = setInterval(() => refresh({ quiet: true }), POLL_MS);
}

export function onHide() {
  stopPolling();
  loadGate.cancel();
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function refresh({ quiet = false, force = false } = {}) {
  const request = loadGate.begin();
  try {
    const [nextStatus, nextLogs] = await Promise.all([
      ctx.Api.getApiServerStatus(),
      fetchLogs(),
    ]);
    if (!loadGate.isCurrent(request)) return;
    status = nextStatus;
    logs = nextLogs;
    if (force || !recipes) recipes = await ctx.Api.getApiServerRecipes();
    if (!loadGate.isCurrent(request)) return;
    render();
  } catch (error) {
    if (quiet) return;
    if (!loadGate.isCurrent(request)) return;
    root.querySelector('#api-server-body').innerHTML = `
      <div class="card">
        <div class="kb-error">${ctx.escapeHtml(t('apiServer.loadFailed'))}: ${ctx.escapeHtml(
          localizeBackendError(error.message)
        )}</div>
      </div>`;
  }
}

async function fetchLogs() {
  try {
    const data = await ctx.Api.getApiServerLogs(100);
    return Array.isArray(data && data.entries) ? data.entries : [];
  } catch {
    return [];
  }
}

/** 服务状态徽标：三态（运行中 / 已开启但起不来 / 已关闭）。 */
function statusBadge() {
  if (status.running) {
    return `<span class="badge badge-running">${ctx.escapeHtml(t('apiServer.stateRunning'))}</span>`;
  }
  if (status.enabled) {
    return `<span class="badge badge-failed">${ctx.escapeHtml(t('apiServer.stateFailed'))}</span>`;
  }
  return `<span class="badge badge-stopped">${ctx.escapeHtml(t('apiServer.stateStopped'))}</span>`;
}

function render() {
  const body = root.querySelector('#api-server-body');
  if (!body || !status) return;

  const models = Array.isArray(status.models) ? status.models : [];
  const keyHint = status.key_configured
    ? t('apiServer.keySet')
    : t('apiServer.keyUnset');

  body.innerHTML = `
    <section class="card api-server-hero">
      <div class="api-server-hero-main">
        <div class="api-server-hero-title">
          ${statusBadge()}
          <code class="api-server-url">${ctx.escapeHtml(status.base_url || '')}</code>
          <button class="btn btn-ghost btn-icon btn-sm" id="api-server-copy-url"
            title="${ctx.escapeHtml(t('apiServer.copyUrl'))}"
            aria-label="${ctx.escapeHtml(t('apiServer.copyUrl'))}">⧉</button>
        </div>
        <div class="api-server-hero-sub">
          ${ctx.escapeHtml(t('apiServer.modelsServed', { count: models.length }))}
          · ${ctx.escapeHtml(keyHint)}
        </div>
        ${
          status.error
            ? `<div class="api-server-error">${ctx.escapeHtml(status.error)}</div>`
            : ''
        }
      </div>
      <div class="api-server-hero-actions">
        <label class="api-server-switch">
          <span>${ctx.escapeHtml(t('apiServer.enable'))}</span>
          <span class="switch">
            <input type="checkbox" id="api-server-enabled" ${status.enabled ? 'checked' : ''} />
            <span class="switch-slider"></span>
          </span>
        </label>
      </div>
    </section>

    ${
      models.length
        ? `<section class="card">
            <div class="card-header"><div class="card-title">${ctx.escapeHtml(
              t('apiServer.loadedModels')
            )}</div></div>
            <div class="panel-list">
              ${models
                .map(
                  (model) => `
                <div class="panel-list-item api-server-model">
                  <span class="panel-list-label">${ctx.escapeHtml(model.name || model.id)}</span>
                  <span class="panel-list-meta">
                    <span class="panel-list-meta-text text-mono">${ctx.escapeHtml(
                      model.id || ''
                    )}</span>
                    <span class="badge badge-running">:${ctx.escapeHtml(
                      String(model.port ?? '-')
                    )}</span>
                  </span>
                </div>`
                )
                .join('')}
            </div>
          </section>`
        : `<section class="card">
            <div class="card-header"><div class="card-title">${ctx.escapeHtml(
              t('apiServer.loadedModels')
            )}</div></div>
            <div class="empty-state api-server-empty">
              <div class="empty-title">${ctx.escapeHtml(t('apiServer.noModels'))}</div>
              <div class="empty-sub">${ctx.escapeHtml(t('apiServer.noModelsHint'))}</div>
              <button class="btn btn-secondary btn-sm" id="api-server-goto-chat">${ctx.escapeHtml(
                t('apiServer.openChat')
              )}</button>
            </div>
          </section>`
    }

    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('apiServer.settings'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(t('apiServer.settingsHint'))}</div>
        </div>
        <div class="card-actions">
          <button class="btn btn-primary btn-sm" id="api-server-save">${ctx.escapeHtml(
            t('apiServer.save')
          )}</button>
        </div>
      </div>
      <div class="api-server-form">
        <div class="form-group">
          <label class="form-label" for="api-server-port">${ctx.escapeHtml(
            t('apiServer.port')
          )}</label>
          <input class="input" type="number" id="api-server-port" min="1" max="65535"
            value="${ctx.escapeHtml(String(status.port ?? 1234))}" />
          <div class="form-help">${ctx.escapeHtml(t('apiServer.portHelp'))}</div>
        </div>
        <div class="form-group">
          <label class="form-label" for="api-server-cors">${ctx.escapeHtml(
            t('apiServer.cors')
          )}</label>
          <input class="input" type="text" id="api-server-cors"
            placeholder="http://localhost:3000, http://127.0.0.1:5173"
            value="${ctx.escapeHtml((status.cors_origins || []).join(', '))}" />
          <div class="form-help">${ctx.escapeHtml(t('apiServer.corsHelp'))}</div>
        </div>
        <div class="form-group">
          <label class="form-label" for="api-server-key">${ctx.escapeHtml(
            t('apiServer.key')
          )}</label>
          <input class="input" type="password" id="api-server-key" autocomplete="off"
            placeholder="${ctx.escapeHtml(
              status.key_configured ? t('apiServer.keyKeepHint') : t('apiServer.keyPh')
            )}" />
          <div class="form-help">${ctx.escapeHtml(t('apiServer.keyHelp'))}</div>
        </div>
        <div class="form-group api-server-check">
          <label class="settings-radio">
            <input type="checkbox" id="api-server-log-requests" ${
              status.log_requests ? 'checked' : ''
            } />
            <span>${ctx.escapeHtml(t('apiServer.logRequests'))}</span>
          </label>
        </div>
      </div>
    </section>

    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('apiServer.recipes'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(t('apiServer.recipesHint'))}</div>
        </div>
      </div>
      <div class="api-server-recipes">
        ${recipeBlock('curl', recipes && recipes.curl)}
        ${recipeBlock('python', recipes && recipes.python)}
        ${recipeBlock('powershell', recipes && recipes.powershell)}
      </div>
      ${recipes && recipes.note ? `<div class="form-help">${ctx.escapeHtml(recipes.note)}</div>` : ''}
    </section>

    <section class="card">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('apiServer.requestLog'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(
            t('apiServer.requestLogHint', { count: logs.length })
          )}</div>
        </div>
        <div class="card-actions">
          <button class="btn btn-secondary btn-sm" id="api-server-export-logs">${ctx.escapeHtml(
            t('apiServer.exportLog')
          )}</button>
          <button class="btn btn-ghost btn-sm" id="api-server-clear-logs">${ctx.escapeHtml(
            t('apiServer.clearLog')
          )}</button>
        </div>
      </div>
      ${logsHtml()}
    </section>
  `;

  bind(body);
}

function recipeBlock(label, code) {
  if (!code) return '';
  // tabindex + role：这是可横向滚动的区域，键盘用户必须能聚焦后用方向键滚动，
  // 否则超长的那一行（curl 的 JSON 体）他们永远读不到右半部分。
  return `
    <div class="api-server-recipe">
      <div class="api-server-recipe-head">
        <span class="api-server-recipe-lang">${ctx.escapeHtml(label)}</span>
        <button class="btn btn-ghost btn-sm" data-copy-recipe="${ctx.escapeHtml(
          label
        )}">${ctx.escapeHtml(t('apiServer.copy'))}</button>
      </div>
      <pre class="api-server-code" tabindex="0" role="group"
        aria-label="${ctx.escapeHtml(label)}">${ctx.escapeHtml(code)}</pre>
    </div>`;
}

function logsHtml() {
  if (!logs.length) {
    return `<div class="empty-state">
      <div class="empty-title">${ctx.escapeHtml(t('apiServer.noLogs'))}</div>
      <div class="empty-sub">${ctx.escapeHtml(t('apiServer.noLogsHint'))}</div>
    </div>`;
  }
  return `
    <div class="table-wrap">
      <table class="table api-server-log-table">
        <thead>
          <tr>
            <th>${ctx.escapeHtml(t('apiServer.colTime'))}</th>
            <th>${ctx.escapeHtml(t('apiServer.colMethod'))}</th>
            <th>${ctx.escapeHtml(t('apiServer.colPath'))}</th>
            <th>${ctx.escapeHtml(t('apiServer.colStatus'))}</th>
            <th>${ctx.escapeHtml(t('apiServer.colModel'))}</th>
            <th>${ctx.escapeHtml(t('apiServer.colMs'))}</th>
          </tr>
        </thead>
        <tbody>
          ${logs
            .map((entry) => {
              // 状态码着色必须同时给出数字：颜色不能单独承载语义。
              const tone =
                entry.status >= 500 ? 'badge-failed' : entry.status >= 400 ? 'badge-stopping' : 'badge-running';
              return `<tr>
                <td class="text-mono">${ctx.escapeHtml(entry.at || '')}</td>
                <td class="text-mono">${ctx.escapeHtml(entry.method || '')}</td>
                <td class="text-mono break-all">${ctx.escapeHtml(entry.path || '')}${
                  entry.error
                    ? `<span class="api-server-log-error">${ctx.escapeHtml(entry.error)}</span>`
                    : ''
                }</td>
                <td><span class="badge ${tone}">${ctx.escapeHtml(String(entry.status ?? ''))}</span></td>
                <td class="text-mono break-all">${ctx.escapeHtml(
                  shortModel(entry.model)
                )}</td>
                <td class="text-mono">${ctx.escapeHtml(String(entry.ms ?? ''))}</td>
              </tr>`;
            })
            .join('')}
        </tbody>
      </table>
    </div>`;
}

/** 日志里的模型列只显示文件名：完整路径会把表格撑到横向滚动。 */
function shortModel(value) {
  const text = String(value || '');
  if (!text) return '—';
  const parts = text.split(/[\\/]/);
  return parts[parts.length - 1] || text;
}

function bind(body) {
  body.querySelector('#api-server-enabled')?.addEventListener('change', toggleEnabled);

  body.querySelector('#api-server-copy-url')?.addEventListener('click', () => {
    ctx.copyToClipboard(status.base_url || '');
    ctx.showToast(t('apiServer.urlCopied'), 'success');
  });

  body.querySelector('#api-server-goto-chat')?.addEventListener('click', () => {
    ctx.navigate('chat');
  });

  body.querySelector('#api-server-save')?.addEventListener('click', saveSettings);

  body.querySelector('#api-server-clear-logs')?.addEventListener('click', clearLogs);

  body.querySelector('#api-server-export-logs')?.addEventListener('click', exportLogs);

  body.querySelectorAll('[data-copy-recipe]').forEach((button) => {
    button.addEventListener('click', () => {
      const lang = button.dataset.copyRecipe;
      const code = recipes && recipes[lang === 'curl' ? 'curl' : lang === 'python' ? 'python' : 'powershell'];
      if (!code) return;
      ctx.copyToClipboard(code);
      ctx.showToast(t('apiServer.copied'), 'success');
    });
  });
}

async function toggleEnabled(event) {
  const enabled = Boolean(event.target.checked);
  event.target.disabled = true;
  try {
    // 开关直接写配置：这是"一键开启/关闭"的那个键，不该再要求按保存。
    await ctx.Api.updateSystemConfig({ api_server_enabled: enabled });
    // 后端要在保存后立即对齐监听（免重启启动器）。
    await ctx.Api.restartApiServer();
    await refresh({ force: true });
    ctx.showToast(
      enabled ? t('apiServer.enabledToast') : t('apiServer.disabledToast'),
      enabled ? 'success' : 'info'
    );
  } catch (error) {
    event.target.checked = !enabled;
    ctx.showToast(`${t('apiServer.saveFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    event.target.disabled = false;
  }
}

async function saveSettings() {
  const body = root.querySelector('#api-server-body');
  const portInput = body.querySelector('#api-server-port');
  const corsInput = body.querySelector('#api-server-cors');
  const keyInput = body.querySelector('#api-server-key');
  const logToggle = body.querySelector('#api-server-log-requests');

  const port = Number(portInput.value);
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    ctx.showToast(t('apiServer.portInvalid'), 'warning');
    portInput.focus();
    return;
  }

  const payload = {
    api_server_port: port,
    api_server_cors_origins: corsInput.value.trim(),
    api_server_log_requests: Boolean(logToggle.checked),
  };
  // 密钥留空 = 保持现状，而不是清空。用户打开这一页时输入框总是空的
  // （后端不回传密钥），保存其它字段不该顺手把密钥抹掉。
  const nextKey = keyInput.value.trim();
  if (nextKey) payload.api_server_key = nextKey;

  const button = body.querySelector('#api-server-save');
  button.disabled = true;
  try {
    await ctx.Api.updateSystemConfig(payload);
    await ctx.Api.restartApiServer();
    await refresh({ force: true });
    ctx.showToast(t('apiServer.saved'), 'success');
  } catch (error) {
    ctx.showToast(`${t('apiServer.saveFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    button.disabled = false;
  }
}

async function clearLogs() {
  try {
    await ctx.Api.clearApiServerLogs();
    await refresh({ quiet: true });
  } catch (error) {
    ctx.showToast(`${t('apiServer.clearFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

/** 导出为 CSV：日志要能被贴进 issue / 表格，JSON 在聊天窗口里不好读。 */
function exportLogs() {
  if (!logs.length) {
    ctx.showToast(t('apiServer.noLogs'), 'warning');
    return;
  }
  const header = ['at', 'method', 'path', 'status', 'model', 'stream', 'ms', 'error'];
  const escapeCsv = (value) => `"${String(value ?? '').replace(/"/g, '""')}"`;
  const rows = [header.join(',')];
  for (const entry of logs) {
    rows.push(header.map((key) => escapeCsv(entry[key])).join(','));
  }
  const blob = new Blob([rows.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const link = document.createElement('a');
  const stamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, 19);
  link.href = url;
  link.download = `lunarshroud-api-server-${stamp}.csv`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  // 立刻 revoke 会让部分浏览器在下完之前失效；给一帧的余量。
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}
