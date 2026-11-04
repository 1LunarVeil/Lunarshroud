// 预设管理页面
// 预设列表在二级左栏（上下文面板），正文是选中预设的详情与操作
// 新建/编辑/克隆/删除/导入/导出/Diff

import { t, localizeBackendError } from './i18n.mjs';
import { createRequestGate, scheduleObjectUrlRevoke, setButtonBusy } from './runtime.mjs';
import { renderList, emptyStateHtml } from './panel_views.mjs';

export const id = 'presets';
export const title = '预设管理';

let ctx = null;
let root = null;
let presets = [];
let selectedId = null;
const detailGate = createRequestGate();
let locale = 'zh-CN';

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  locale = document.documentElement.lang === 'en-US' ? 'en-US' : 'zh-CN';
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('presets.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('presets.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="btn-import">${ctx.escapeHtml(t('presets.import'))}</button>
        <button class="btn btn-secondary" id="btn-diff">${ctx.escapeHtml(t('presets.diff'))}</button>
        <button class="btn btn-primary" id="btn-new">${ctx.escapeHtml(t('presets.new'))}</button>
      </div>
    </div>
    <div id="preset-detail"></div>
  `;
  root.querySelector('#btn-new').addEventListener('click', () => openEditModal(null));
  root.querySelector('#btn-import').addEventListener('click', openImportModal);
  root.querySelector('#btn-diff').addEventListener('click', openDiffModal);
  // 操作按钮随详情内容一起重绘，所以用事件委托绑在容器上，
  // 避免每次 renderDetail() 都重新绑定一遍
  root.querySelector('#preset-detail').addEventListener('click', (e) => {
    const btn = e.target.closest('[data-action]');
    if (btn) handleAction(btn.dataset.action, btn.dataset.id, btn);
  });
}

export function onShow() { refresh(); }
export function onHide() {
  detailGate.cancel();
}

function displayName(p) {
  return (locale === 'en-US' && p.name_en) ? p.name_en : p.name;
}

function displayDescription(p) {
  return (locale === 'en-US' && p.description_en) ? p.description_en : (p.description || '');
}

function displayTags(p) {
  if (locale === 'en-US' && Array.isArray(p.tags_en) && p.tags_en.length) return p.tags_en;
  return p.tags || [];
}

async function refresh() {
  try {
    presets = await ctx.Api.listPresets();
    if (selectedId) {
      // 仍选中之前选中的预设
      const stillExists = presets.some((p) => p.id === selectedId);
      if (!stillExists) selectedId = presets.length ? presets[0].id : null;
    } else if (presets.length) {
      selectedId = presets[0].id;
    }
    // 先落定 selectedId 再画面板，否则首次进入时高亮会闪一下
    ctx.refreshList?.();
    renderDetail();
  } catch (e) {
    ctx.showToast(`${t('presets.loadFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

// 二级面板：预设列表。
// 列表项不放操作按钮 —— 侧栏虽比旧面板宽，但一行塞四个按钮仍会读成表单；
// 操作统一收到正文详情页（只对"当前选中的那一个"生效，语义也更清楚）。
/** 侧栏列表的标题与主行动。 */
export function sidebarListTitle() {
  return t('nav.presets');
}

export function sidebarListAction() {
  // 新建预设走的是与页头 #btn-new 同一个入口，避免"新建"有两套代码。
  return { label: '+', title: t('presets.new'), onClick: () => openEditModal(null) };
}

export function renderSidebarList(el) {
  renderList(el, ctx, {
    items: presets.map((p) => ({
      key: String(p.id),
      label: displayName(p),
      meta: displayDescription(p) || undefined,
    })),
    activeKey: selectedId,
    onSelect: (key) => {
      selectedId = key;
      ctx.refreshList?.();
      renderDetail();
    },
    emptyText: t('presets.empty'),
  });
}

async function renderDetail() {
  const el = root.querySelector('#preset-detail');
  if (!selectedId) {
    detailGate.cancel();
    el.innerHTML = `<div class="card">${emptyStateHtml(ctx, { icon: 'bookmark', title: t('presets.selectHint'), sub: t('presets.selectHintSub') })}</div>`;
    return;
  }
  const p = presets.find((x) => x.id === selectedId);
  if (!p) {
    detailGate.cancel();
    el.innerHTML = '';
    return;
  }
  // 获取完整预设（含 params）。请求代次防止快速点击 A→B 时 A 的慢响应
  // 覆盖 B 的正文与操作按钮。
  const requestedId = selectedId;
  const request = detailGate.begin();
  let full;
  try {
    full = await ctx.Api.getPreset(requestedId);
  } catch (e) {
    if (!detailGate.isCurrent(request) || selectedId !== requestedId) return;
    el.innerHTML = `<div class="card">${ctx.escapeHtml(t('presets.loadFailed'))}: ${ctx.escapeHtml(localizeBackendError(e.message))}</div>`;
    return;
  }
  if (!detailGate.isCurrent(request) || selectedId !== requestedId) return;
  const params = full.params || {};
  const keys = Object.keys(params);
  const tags = displayTags(full);
  el.innerHTML = `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(displayName(full))}</span>
        <div class="card-actions">
          <button class="btn btn-sm btn-secondary" data-action="edit" data-id="${ctx.escapeHtml(full.id)}">${ctx.escapeHtml(t('presets.edit'))}</button>
          <button class="btn btn-sm btn-secondary" data-action="clone" data-id="${ctx.escapeHtml(full.id)}">${ctx.escapeHtml(t('presets.clone'))}</button>
          <button class="btn btn-sm btn-secondary" data-action="export" data-id="${ctx.escapeHtml(full.id)}">${ctx.escapeHtml(t('presets.export'))}</button>
          <button class="btn btn-sm btn-danger" data-action="delete" data-id="${ctx.escapeHtml(full.id)}">${ctx.escapeHtml(t('presets.delete'))}</button>
        </div>
      </div>
      ${displayDescription(full) ? `<div class="text-muted mb-2">${ctx.escapeHtml(displayDescription(full))}</div>` : ''}
      ${tags.length ? `<div class="mb-2">${tags.map((tag) => `<span class="tag">${ctx.escapeHtml(tag)}</span>`).join('')}</div>` : ''}
      <div class="text-sm text-muted">ID: <span class="text-mono">${ctx.escapeHtml(full.id)}</span> · ${ctx.escapeHtml(t('presets.createdAt'))} ${ctx.formatTime(full.created_at)}${full.runtime_id ? ` · ${ctx.escapeHtml(t('dash.runtime'))}: <span class="text-mono">${ctx.escapeHtml(full.runtime_id)}</span>` : ''}</div>
    </div>
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(t('presets.paramDetail', { count: keys.length }))}</span>
        <button class="btn btn-sm btn-ghost" id="btn-copy-params" ml-auto>${ctx.escapeHtml(t('presets.copyJson'))}</button>
      </div>
      <div class="table-wrap">
        <table class="table">
          <thead><tr><th>${ctx.escapeHtml(t('presets.param'))}</th><th>${ctx.escapeHtml(t('presets.value'))}</th></tr></thead>
          <tbody>
            ${keys.map((k) => `
              <tr><td class="text-mono">${ctx.escapeHtml(k)}</td><td class="text-mono" break-all>${ctx.escapeHtml(JSON.stringify(params[k]))}</td></tr>
            `).join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
  el.querySelector('#btn-copy-params').addEventListener('click', () => {
    ctx.copyToClipboard(JSON.stringify(full, null, 2));
  });
}

async function handleAction(action, id, button) {
  const mutating = action === 'clone' || action === 'delete';
  if (mutating) setButtonBusy(button, true);
  try {
    await handleActionInner(action, id);
  } finally {
    if (mutating) setButtonBusy(button, false);
  }
}

async function handleActionInner(action, id) {
  const p = presets.find((x) => x.id === id);
  if (!p) {
    // 列表刚刷新过，按钮携带的旧 id 已失效；刷新列表让 UI 与数据对齐
    ctx.showToast(t('presets.staleGone'), 'warning');
    refresh();
    return;
  }
  switch (action) {
    case 'edit': {
      try {
        const full = await ctx.Api.getPreset(id);
        openEditModal(full);
      } catch (e) {
        ctx.showToast(`${t('presets.loadFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    }
    case 'clone': {
      const fallback = `${displayName(p)} ${t('presets.copySuffix')}`;
      const name = await promptCloneName(fallback);
      if (name === null) return;
      try {
        await ctx.Api.clonePreset(id, name || fallback);
        ctx.showToast(t('presets.cloned'), 'success');
        refresh();
      } catch (e) {
        ctx.showToast(`${t('presets.cloneFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    }
    case 'export': {
      try {
        const full = await ctx.Api.exportPreset(id);
        const blob = new Blob([JSON.stringify(full, null, 2)], { type: 'application/json' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `${id}.json`;
        a.click();
        scheduleObjectUrlRevoke(url, (value) => URL.revokeObjectURL(value), 1000);
        ctx.showToast(t('presets.exported'), 'success', 1500);
      } catch (e) {
        ctx.showToast(`${t('presets.exportFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    }
    case 'delete': {
      if (!(await ctx.showConfirm(t('presets.confirmDelete', { name: displayName(p) })))) return;
      try {
        await ctx.Api.deletePreset(id);
        ctx.showToast(t('presets.deleted'), 'success');
        if (selectedId === id) selectedId = null;
        refresh();
      } catch (e) {
        ctx.showToast(`${t('presets.deleteFailed')}: ${localizeBackendError(e.message)}`, 'error');
      }
      break;
    }
  }
}

/** 克隆预设名输入：用应用内浮层，避免原生 prompt 与主题/焦点体系脱节。 */
function promptCloneName(defaultName) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    ctx.showModal(
      t('presets.clone'),
      `<div class="form-group">
        <label class="form-label" for="clone-name">${ctx.escapeHtml(t('presets.clonePrompt'))}</label>
        <input type="text" class="input" id="clone-name" value="${ctx.escapeHtml(defaultName)}"/>
      </div>`,
      [
        { label: t('app.cancel'), class: 'btn-secondary', onClick: () => done(null) },
        {
          label: t('presets.clone'),
          class: 'btn-primary',
          onClick: () => {
            const value = document.getElementById('clone-name')?.value.trim() || defaultName;
            done(value);
          },
        },
      ],
      { onClose: () => done(null) }
    );
  });
}

// ========== 新建/编辑 Modal ==========

async function openEditModal(existing) {
  // 加载默认参数作为新预设的初始 params
  let defaults = {};
  try { defaults = await ctx.Api.getParamDefaults(); } catch {}
  const isEdit = !!existing;
  const init = existing ? JSON.stringify(existing.params, null, 2) : JSON.stringify(defaults, null, 2);
  const body = `
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('params.presetName'))} <span class="required">*</span></label>
      <input type="text" class="input" id="e-name" value="${existing ? ctx.escapeHtml(displayName(existing)) : ''}" placeholder="${ctx.escapeHtml(t('params.presetNamePh'))}"/>
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('params.description'))}</label>
      <textarea class="textarea" id="e-desc" placeholder="${ctx.escapeHtml(t('params.descPh'))}">${existing ? ctx.escapeHtml(displayDescription(existing)) : ''}</textarea>
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('params.tags'))}</label>
      <input type="text" class="input" id="e-tags" value="${existing && existing.tags ? ctx.escapeHtml((locale === 'en-US' && existing.tags_en ? existing.tags_en : existing.tags).join(', ')) : ''}" placeholder="${ctx.escapeHtml(t('params.tagsPh'))}"/>
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('presets.paramsJson'))}</label>
      <textarea class="textarea" id="e-params" mono-editor>${ctx.escapeHtml(init)}</textarea>
      <div class="form-help">${ctx.escapeHtml(t('presets.jsonHint'))}</div>
    </div>
  `;
  ctx.showModal(isEdit ? t('presets.editTitle') : t('presets.newTitle'), body, [
    { label: t('app.cancel'), class: 'btn-secondary', size: 'lg' },
    {
      label: t('params.save'),
      class: 'btn-primary',
      keepOpen: true,
      size: 'lg',
      onClick: (overlay, bodyEl) => {
        doSave(bodyEl, existing);
        return false;
      },
    },
  ]);
}

async function doSave(bodyEl, existing) {
  const name = bodyEl.querySelector('#e-name').value.trim();
  const desc = bodyEl.querySelector('#e-desc').value.trim();
  const tagsStr = bodyEl.querySelector('#e-tags').value.trim();
  const paramsStr = bodyEl.querySelector('#e-params').value;
  if (!name) { ctx.showToast(t('params.nameRequired'), 'warning'); return; }
  let params;
  try {
    params = JSON.parse(paramsStr);
  } catch (e) {
    ctx.showToast(`${t('presets.jsonInvalid')}: ${e.message}`, 'error');
    return;
  }
  const tags = tagsStr ? tagsStr.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [];
  try {
    if (existing) {
      await ctx.Api.updatePreset(existing.id, { name, description: desc, tags, params });
      ctx.showToast(t('presets.updated'), 'success');
    } else {
      await ctx.Api.createPreset({ name, description: desc, tags, params });
      ctx.showToast(t('presets.created'), 'success');
    }
    ctx.closeModal();
    refresh();
  } catch (e) {
    ctx.showToast(`${t('params.saveFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

// ========== 导入 Modal ==========

function openImportModal() {
  const body = `
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('presets.pasteJson'))}</label>
      <textarea class="textarea" id="i-json" mono-editor placeholder='{"name":"...","params":{...}}'></textarea>
    </div>
    <div class="form-group">
      <label class="form-label">${ctx.escapeHtml(t('presets.orUpload'))}</label>
      <input type="file" class="input" id="i-file" accept=".json,application/json"/>
    </div>
  `;
  ctx.showModal(t('presets.import'), body, [
    { label: t('app.cancel'), class: 'btn-secondary', size: 'lg' },
    {
      label: t('presets.import'),
      class: 'btn-primary',
      keepOpen: true,
      size: 'lg',
      onClick: (overlay, bodyEl) => {
        doImport(bodyEl);
        return false;
      },
    },
  ]);
}

async function doImport(bodyEl) {
  const fileInput = bodyEl.querySelector('#i-file');
  const maxBytes = 1024 * 1024;
  let jsonStr;
  if (fileInput.files && fileInput.files[0]) {
    if (fileInput.files[0].size > maxBytes) {
      ctx.showToast(t('presets.fileTooLarge'), 'warning');
      return;
    }
    jsonStr = await fileInput.files[0].text();
  } else {
    jsonStr = bodyEl.querySelector('#i-json').value.trim();
  }
  if (!jsonStr) { ctx.showToast(t('presets.noJson'), 'warning'); return; }
  // 从记事本/PowerShell 复制粘贴进来的 JSON 常带前导 BOM：File.text() 会按
  // 规范剥掉它，但 textarea 这条路不会，而 JSON.parse("\uFEFF{}") 是直接抛的。
  // 剥一次两种来源都安全。
  jsonStr = jsonStr.replace(/^\uFEFF/, '');
  if (new Blob([jsonStr]).size > maxBytes) {
    ctx.showToast(t('presets.fileTooLarge'), 'warning');
    return;
  }
  let data;
  try {
    data = JSON.parse(jsonStr);
  } catch (e) {
    ctx.showToast(`${t('presets.jsonInvalid')}: ${e.message}`, 'error');
    return;
  }
  try {
    await ctx.Api.importPreset(data);
    ctx.showToast(t('presets.imported'), 'success');
    ctx.closeModal();
    refresh();
  } catch (e) {
    ctx.showToast(`${t('presets.importFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

// ========== Diff Modal ==========

function openDiffModal() {
  if (presets.length < 2) {
    ctx.showToast(t('presets.needTwo'), 'warning');
    return;
  }
  const opts = presets.map((p) => `<option value="${ctx.escapeHtml(String(p.id))}">${ctx.escapeHtml(displayName(p))}</option>`).join('');
  const body = `
    <div class="flex gap-2 mb-4">
      <div class="flex-1">
        <label class="form-label">${ctx.escapeHtml(t('presets.presetA'))}</label>
        <select class="select" id="d-a">${opts}</select>
      </div>
      <div class="flex-1">
        <label class="form-label">${ctx.escapeHtml(t('presets.presetB'))}</label>
        <select class="select" id="d-b">${opts}</select>
      </div>
    </div>
    <div id="d-result"></div>
  `;
  ctx.showModal(t('presets.diffTitle'), body, [
    { label: t('app.close'), class: 'btn-secondary', size: 'lg' },
    {
      label: t('presets.compare'),
      class: 'btn-primary',
      keepOpen: true,
      size: 'lg',
      onClick: (overlay, bodyEl) => {
        runDiff(bodyEl);
        return false;
      },
    },
  ]);
}

async function runDiff(bodyEl) {
  const id1 = bodyEl.querySelector('#d-a').value;
  const id2 = bodyEl.querySelector('#d-b').value;
  if (id1 === id2) {
    ctx.showToast(t('presets.pickDifferent'), 'warning');
    return;
  }
  const result = bodyEl.querySelector('#d-result');
  result.innerHTML = `<div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(t('presets.comparing'))}</div>`;
  try {
    const diff = await ctx.Api.diffPresets(id1, id2);
    renderDiff(result, diff);
  } catch (e) {
    result.innerHTML = `<div class="empty-state">${ctx.escapeHtml(t('presets.compareFailed'))}: ${ctx.escapeHtml(localizeBackendError(e.message))}</div>`;
  }
}

function renderDiff(container, diff) {
  // 后端返回：{preset1, preset2, diff:[{param,label,value1,value2,changed}], summary}
  if (!diff || !diff.diff) {
    container.innerHTML = `<div class="empty-state">${ctx.escapeHtml(t('presets.noResult'))}</div>`;
    return;
  }
  const rows = diff.diff;
  const changedOnly = rows.filter((r) => r.changed);
  const summary = diff.summary || {};
  const name1 = (locale === 'en-US' && diff.preset1.name_en) ? diff.preset1.name_en : diff.preset1.name;
  const name2 = (locale === 'en-US' && diff.preset2.name_en) ? diff.preset2.name_en : diff.preset2.name;
  container.innerHTML = `
    <div class="text-sm text-muted mb-2">
      ${ctx.escapeHtml(t('presets.diffOf', { a: name1, b: name2 }))} · ${ctx.escapeHtml(t('presets.diffSummary', { total: summary.total_params ?? rows.length, changed: changedOnly.length }))}
    </div>
    <div class="table-wrap" scroll-panel-380>
      <table class="table">
        <thead><tr><th>${ctx.escapeHtml(t('presets.param'))}</th><th>${ctx.escapeHtml(t('presets.presetA'))}</th><th>${ctx.escapeHtml(t('presets.presetB'))}</th></tr></thead>
        <tbody>
          ${rows.map((r) => {
            const cls = r.changed ? 'changed' : '';
            return `<tr class="${cls}">
              <td class="text-mono">${ctx.escapeHtml(r.param)}<br/><span class="text-muted text-sm">${ctx.escapeHtml(r.label || '')}</span></td>
              <td class="text-mono" break-all>${ctx.escapeHtml(JSON.stringify(r.value1 ?? '—'))}</td>
              <td class="text-mono" break-all>${ctx.escapeHtml(JSON.stringify(r.value2 ?? '—'))}</td>
            </tr>`;
          }).join('')}
        </tbody>
      </table>
    </div>
  `;
}
