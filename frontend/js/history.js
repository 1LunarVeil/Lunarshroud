// 启动历史页面
// 表格显示历史记录 + 详情 Modal + 重新启动 + 分页

import { t, localizeBackendError } from './i18n.mjs';
import { emptyStateHtml } from './panel_views.mjs';

export const id = 'history';
export const title = '启动历史';

let ctx = null;
let root = null;
let records = [];
let page = 1;
const PAGE_SIZE = 50;

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <div class="page-title">${ctx.escapeHtml(t('history.title'))}</div>
        <div class="page-subtitle">${ctx.escapeHtml(t('history.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="btn-refresh">${ctx.escapeHtml(t('history.refresh'))}</button>
        <button class="btn btn-danger" id="btn-clear">${ctx.escapeHtml(t('history.clearAll'))}</button>
      </div>
    </div>
    <div id="history-table" class="table-wrap"></div>
    <div class="pagination" id="history-pagination"></div>
  `;
  root.querySelector('#btn-refresh').addEventListener('click', () => { page = 1; refresh(); });
  root.querySelector('#btn-clear').addEventListener('click', clearAll);
}

export function onShow() { refresh(); }
export function onHide() {}

async function refresh() {
  const offset = (page - 1) * PAGE_SIZE;
  try {
    records = await ctx.Api.listHistory(PAGE_SIZE, offset);
    renderTable();
  } catch (e) {
    ctx.showToast(`${t('history.loadFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

function renderTable() {
  const wrap = root.querySelector('#history-table');
  if (!records.length) {
    wrap.innerHTML = emptyStateHtml(ctx, { icon: 'clock', title: t('history.empty'), sub: t('history.emptySub') });
    renderPagination(0);
    return;
  }
  wrap.innerHTML = `
    <table class="table">
      <thead>
        <tr>
          <th>${ctx.escapeHtml(t('history.time'))}</th>
          <th>${ctx.escapeHtml(t('dash.model'))}</th>
          <th>${ctx.escapeHtml(t('dash.port'))}</th>
          <th>${ctx.escapeHtml(t('history.status'))}</th>
          <th>${ctx.escapeHtml(t('history.duration'))}</th>
          <th>${ctx.escapeHtml(t('dash.preset'))}</th>
          <th>${ctx.escapeHtml(t('history.actions'))}</th>
        </tr>
      </thead>
      <tbody>
        ${records.map((r) => `
          <tr>
            <td>${ctx.formatTime(r.started_at)}</td>
            <td>${ctx.escapeHtml(ctx.basename(r.model_path) || r.model_path || '-')}</td>
            <td class="text-mono">${r.port || '-'}</td>
            <td>${ctx.statusBadge(r.status)}</td>
            <td class="text-mono">${ctx.formatDuration(r.duration_seconds)}</td>
            <td>${ctx.escapeHtml(r.preset_name || '-')}</td>
            <td>
              <button class="btn btn-sm btn-ghost" data-action="detail" data-id="${r.id}">${ctx.escapeHtml(t('history.detail'))}</button>
              <button class="btn btn-sm btn-ghost" data-action="relaunch" data-id="${r.id}">${ctx.escapeHtml(t('history.relaunch'))}</button>
              <button class="btn btn-sm btn-danger" data-action="delete" data-id="${r.id}">${ctx.escapeHtml(t('presets.delete'))}</button>
            </td>
          </tr>
        `).join('')}
      </tbody>
    </table>
  `;
  wrap.querySelectorAll('[data-action]').forEach((btn) => {
    btn.addEventListener('click', () => handleAction(btn.dataset.action, parseInt(btn.dataset.id, 10)));
  });
  // 假设满页则有下一页（粗略）
  renderPagination(records.length);
}

function renderPagination(currentCount) {
  const el = root.querySelector('#history-pagination');
  const hasPrev = page > 1;
  const hasNext = currentCount === PAGE_SIZE;
  el.innerHTML = `
    <button class="btn btn-sm btn-secondary" id="pg-prev" ${hasPrev ? '' : 'disabled'}>${ctx.escapeHtml(t('history.prevPage'))}</button>
    <span class="text-muted">${ctx.escapeHtml(t('history.pageOf', { page }))}</span>
    <button class="btn btn-sm btn-secondary" id="pg-next" ${hasNext ? '' : 'disabled'}>${ctx.escapeHtml(t('history.nextPage'))}</button>
  `;
  el.querySelector('#pg-prev').addEventListener('click', () => { if (hasPrev) { page--; refresh(); } });
  el.querySelector('#pg-next').addEventListener('click', () => { if (hasNext) { page++; refresh(); } });
}

async function handleAction(action, id) {
  switch (action) {
    case 'detail':
      showDetail(id);
      break;
    case 'relaunch':
      if (!(await ctx.showConfirm(t('history.confirmRelaunch')))) return;
      try {
        const inst = await ctx.Api.relaunchHistory(id);
        ctx.showToast(t('dash.launched', { name: inst.name, port: inst.port }), 'success');
      } catch (e) {
        ctx.showToast(`${t('history.relaunchFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    case 'delete':
      if (!(await ctx.showConfirm(t('history.confirmDelete')))) return;
      try {
        await ctx.Api.deleteHistory(id);
        ctx.showToast(t('history.deleted'), 'success');
        refresh();
      } catch (e) {
        ctx.showToast(`${t('history.deleteFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
  }
}

async function showDetail(id) {
  try {
    const h = await ctx.Api.getHistory(id);
    const params = h.params || {};
    const paramRows = Object.keys(params).map((k) => `
      <tr><td class="text-mono">${ctx.escapeHtml(k)}</td><td class="text-mono" style="word-break:break-all;">${ctx.escapeHtml(JSON.stringify(params[k]))}</td></tr>
    `).join('');
    const body = `
      <div class="mb-4 text-sm">
        <div><strong>${ctx.escapeHtml(t('history.time'))}:</strong> ${ctx.formatTime(h.started_at)}${h.stopped_at ? ` ~ ${ctx.formatTime(h.stopped_at)}` : ''}</div>
        <div><strong>${ctx.escapeHtml(t('dash.model'))}:</strong> ${ctx.escapeHtml(h.model_path || '-')}</div>
        <div><strong>${ctx.escapeHtml(t('dash.port'))}:</strong> ${h.port || '-'}</div>
        <div><strong>${ctx.escapeHtml(t('history.status'))}:</strong> ${ctx.statusBadge(h.status)}${h.exit_code !== null && h.exit_code !== undefined ? ` · exit ${h.exit_code}` : ''}</div>
        <div><strong>${ctx.escapeHtml(t('dash.preset'))}:</strong> ${ctx.escapeHtml(h.preset_name || '-')}</div>
        ${h.error_message ? `<div style="color:var(--color-danger);"><strong>${ctx.escapeHtml(t('dash.error'))}:</strong> ${ctx.escapeHtml(localizeBackendError(h.error_message))}</div>` : ''}
      </div>
      <div class="mb-2"><strong>${ctx.escapeHtml(t('history.launchCommand'))}:</strong></div>
      <pre class="text-mono" style="background:#0F172A; color:#E2E8F0; padding:10px; border-radius:8px; font-size:12px; white-space:pre-wrap; word-break:break-all; max-height:160px; overflow:auto;">${ctx.escapeHtml(h.command_text || '')}</pre>
      <div class="mt-4 mb-2"><strong>${ctx.escapeHtml(t('history.paramList', { count: Object.keys(params).length }))}:</strong></div>
      <div class="table-wrap" style="max-height:200px; overflow:auto;">
        <table class="table">
          <thead><tr><th>${ctx.escapeHtml(t('presets.param'))}</th><th>${ctx.escapeHtml(t('presets.value'))}</th></tr></thead>
          <tbody>${paramRows || `<tr><td colspan="2" class="empty-state">${ctx.escapeHtml(t('history.noParams'))}</td></tr>`}</tbody>
        </table>
      </div>
    `;
    ctx.showModal(t('history.detailTitle'), body, [
      { label: t('app.close'), class: 'btn-secondary', size: 'lg' },
      { label: t('history.copyCommand'), class: 'btn-primary', size: 'lg', onClick: () => ctx.copyToClipboard(h.command_text || '') },
    ]);
  } catch (e) {
    ctx.showToast(`${t('history.detailFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

async function clearAll() {
  if (!(await ctx.showConfirm(t('history.confirmClear')))) return;
  try {
    const r = await ctx.Api.clearHistory();
    ctx.showToast(t('history.cleared', { count: r.deleted_count || 0 }), 'success');
    page = 1;
    refresh();
  } catch (e) {
    ctx.showToast(`${t('history.clearFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}
