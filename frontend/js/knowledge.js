// 本地知识库页面：建库、导入资料、索引进度、检索测试与模型配置。
// 页面只做编排；提取/分块/嵌入/OCR 全部在后端完成，前端不接触模型进程。

import { localizeBackendError, t } from './i18n.mjs';
import { createRequestGate, setButtonBusy } from './runtime.mjs';
import { renderList, emptyStateHtml } from './panel_views.mjs';
import { tauriInvoke } from './api.js';

export const id = 'knowledge';
export const title = '知识库';

const POLL_MS = 2000;
let ctx = null;
let root = null;
let detailEl = null;
let bases = [];
let selectedId = null;
let detail = null;
let documents = [];
let settingsDirty = false;
let pollTimer = null;
let searchResults = null;
let searchLoading = false;
let searchError = '';
const detailGate = createRequestGate();

export function init(el, appCtx) {
  root = el;
  ctx = appCtx;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('knowledge.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('knowledge.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="kb-unload">${ctx.escapeHtml(t('knowledge.unload'))}</button>
        <button class="btn btn-primary" id="kb-new">${ctx.escapeHtml(t('knowledge.new'))}</button>
      </div>
    </div>
    <div id="kb-detail"></div>
  `;
  detailEl = root.querySelector('#kb-detail');
  root.querySelector('#kb-new').addEventListener('click', openCreateModal);
  root.querySelector('#kb-unload').addEventListener('click', unloadServices);
  detailEl.addEventListener('click', onClick);
  detailEl.addEventListener('input', onFieldInput);
  detailEl.addEventListener('change', onFieldInput);
}

export function onShow() {
  refresh();
}

export function onHide() {
  detailGate.cancel();
  clearPoll();
}

/** 侧栏列表的标题与主行动。 */
export function sidebarListTitle() {
  return t('nav.knowledge');
}

export function sidebarListAction() {
  return { label: '+', title: t('knowledge.new'), onClick: () => openCreateModal() };
}

export function renderSidebarList(el) {
  renderList(el, ctx, {
    items: bases.map((base) => ({
      key: base.id,
      label: base.name,
      meta: t('knowledge.panelMeta', {
        docs: base.document_count || 0,
        chunks: base.chunk_count || 0,
      }),
      badge: statusBadge(base.status),
    })),
    activeKey: selectedId,
    onSelect: (key) => {
      if (key === selectedId) return;
      selectedId = key;
      settingsDirty = false;
      searchResults = null;
      searchError = '';
      ctx.refreshList?.();
      loadDetail({ full: true });
    },
    emptyText: t('knowledge.panelEmpty'),
  });
}

function statusBadge(status) {
  const known = ['idle', 'indexing', 'error', 'pending', 'staged', 'ready', 'queued', 'skipped'];
  const key = known.includes(status) ? status : 'idle';
  const label = t(`knowledge.status.${key}`);
  const tone = key === 'error'
    ? 'failed'
    : key === 'indexing' || key === 'queued'
      ? 'starting'
      : key === 'ready'
        ? 'success'
        : 'stopped';
  return `<span class="badge badge-${tone}">${ctx.escapeHtml(label)}</span>`;
}

async function refresh({ quiet = false } = {}) {
  try {
    const response = await ctx.Api.listKnowledge();
    bases = Array.isArray(response?.bases) ? response.bases : [];
    if (selectedId && !bases.some((base) => base.id === selectedId)) selectedId = null;
    if (!selectedId && bases.length) selectedId = bases[0].id;
    ctx.refreshList?.();
    if (!selectedId) {
      detail = null;
      documents = [];
      renderDetail();
      return;
    }
    await loadDetail({ full: !quiet || !detail, quiet });
  } catch (error) {
    if (!quiet) {
      ctx.showToast(`${t('knowledge.loadFailed')}: ${localizeBackendError(error.message)}`, 'error');
    }
  }
}

async function loadDetail({ full = false, quiet = false } = {}) {
  if (!selectedId) return;
  const requestId = selectedId;
  const token = detailGate.begin();
  try {
    const response = await ctx.Api.getKnowledge(requestId);
    if (!detailGate.isCurrent(token) || requestId !== selectedId) return;
    detail = response?.base || null;
    documents = Array.isArray(response?.documents) ? response.documents : [];
    if (!detail) {
      selectedId = null;
      ctx.refreshList?.();
      renderDetail();
      return;
    }
    if (full || !detailEl.querySelector('#kb-summary')) {
      renderDetail();
    } else {
      renderSummary();
      renderDocuments();
    }
    schedulePoll();
  } catch (error) {
    if (!detailGate.isCurrent(token)) return;
    if (!quiet) {
      ctx.showToast(`${t('knowledge.loadFailed')}: ${localizeBackendError(error.message)}`, 'error');
    }
  }
}

function renderDetail() {
  if (!selectedId || !detail) {
    detailEl.innerHTML = `<div class="card">${emptyStateHtml(ctx, {
      icon: 'layers',
      title: t('knowledge.selectHint'),
      sub: t('knowledge.selectHintSub'),
    })}</div>`;
    return;
  }
  detailEl.innerHTML = `
    ${summaryHtml()}
    <div class="card kb-block">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('knowledge.documents'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(t('knowledge.documentsHelp'))}</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-secondary btn-sm" data-action="add-file">${ctx.escapeHtml(t('knowledge.addFile'))}</button>
          <button class="btn btn-secondary btn-sm" data-action="add-folder">${ctx.escapeHtml(t('knowledge.addFolder'))}</button>
          <button class="btn btn-secondary btn-sm" data-action="add-paths">${ctx.escapeHtml(t('knowledge.addPaths'))}</button>
          <button class="btn btn-secondary btn-sm" data-action="reindex">${ctx.escapeHtml(t('knowledge.reindex'))}</button>
          <button class="btn btn-secondary btn-sm" data-action="reindex-force">${ctx.escapeHtml(t('knowledge.reindexForce'))}</button>
          <button class="btn btn-danger btn-sm" data-action="delete-base">${ctx.escapeHtml(t('knowledge.deleteBase'))}</button>
        </div>
      </div>
      <div class="table-wrap">
        <table class="table kb-doc-table">
          <thead><tr>
            <th scope="col">${ctx.escapeHtml(t('knowledge.colFile'))}</th>
            <th scope="col">${ctx.escapeHtml(t('knowledge.colStatus'))}</th>
            <th scope="col">${ctx.escapeHtml(t('knowledge.colChunks'))}</th>
            <th scope="col">${ctx.escapeHtml(t('knowledge.colActions'))}</th>
          </tr></thead>
          <tbody id="kb-doc-body"></tbody>
        </table>
      </div>
    </div>
    <div class="card kb-block">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('knowledge.settings'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(t('knowledge.settingsHelp'))}</div>
        </div>
        <div class="page-actions">
          <button class="btn btn-primary btn-sm" data-action="save-settings">${ctx.escapeHtml(t('knowledge.save'))}</button>
        </div>
      </div>
      <div class="kb-settings-grid">
        ${settingsHtml()}
      </div>
    </div>
    <div class="card kb-block">
      <div class="card-header">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('knowledge.searchTitle'))}</div>
          <div class="card-subtitle">${ctx.escapeHtml(t('knowledge.searchHelp'))}</div>
        </div>
      </div>
      <form class="kb-search-form" id="kb-search-form">
        <input class="input" id="kb-search-input" type="search" maxlength="500"
          placeholder="${ctx.escapeHtml(t('knowledge.searchPlaceholder'))}"
          aria-label="${ctx.escapeHtml(t('knowledge.searchPlaceholder'))}" />
        <button class="btn btn-secondary" type="submit">${ctx.escapeHtml(t('knowledge.search'))}</button>
      </form>
      <div id="kb-search-results" class="kb-search-results"></div>
    </div>
  `;
  renderDocuments();
  renderSearchResults();
  const form = detailEl.querySelector('#kb-search-form');
  if (form) {
    form.addEventListener('submit', (event) => {
      event.preventDefault();
      runSearch();
    });
  }
}

function summaryHtml() {
  const base = detail || {};
  const progress = Math.max(0, Math.min(100, Number(base.progress) || 0));
  const indexing = base.status === 'indexing';
  const totalBytes = documents.reduce((sum, doc) => sum + (Number(doc.size_bytes) || 0), 0);
  return `
    <div class="card kb-summary" id="kb-summary">
      <div class="kb-summary-head">
        <div>
          <h2 class="kb-name">${ctx.escapeHtml(base.name || '')}</h2>
          <p class="kb-description">${ctx.escapeHtml(base.description || t('knowledge.noDescription'))}</p>
        </div>
        <div class="kb-summary-status">${statusBadge(base.status)}</div>
      </div>
      <div class="kb-stats">
        <div class="kb-stat"><span class="kb-stat-value">${documents.length}</span><span class="kb-stat-label">${ctx.escapeHtml(t('knowledge.documents'))}</span></div>
        <div class="kb-stat"><span class="kb-stat-value">${Number(base.chunk_count) || 0}</span><span class="kb-stat-label">${ctx.escapeHtml(t('knowledge.chunks'))}</span></div>
        <div class="kb-stat"><span class="kb-stat-value">${ctx.formatBytes(totalBytes)}</span><span class="kb-stat-label">${ctx.escapeHtml(t('knowledge.sourceSize'))}</span></div>
        <div class="kb-stat"><span class="kb-stat-value">${Number(base.embedding_dim) || '-'}</span><span class="kb-stat-label">${ctx.escapeHtml(t('knowledge.dimensions'))}</span></div>
      </div>
      ${
        indexing
          ? `<div class="kb-progress-wrap">
              <div class="kb-progress" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${progress}" aria-label="${ctx.escapeHtml(t('knowledge.progress'))}"><span style="width:${progress}%"></span></div>
              <div class="kb-progress-text">${ctx.escapeHtml(base.error || t('knowledge.progressMessage', { progress, message: base.error || '' }))}</div>
              <button class="btn btn-secondary btn-sm kb-cancel" data-action="cancel-index">${ctx.escapeHtml(t('knowledge.cancel'))}</button>
            </div>`
          : ''
      }
      ${
        base.status === 'error' && base.error
          ? `<div class="kb-error" role="alert">${ctx.escapeHtml(base.error)}</div>`
          : base.error
            ? `<div class="kb-last-message">${ctx.escapeHtml(base.error)}</div>`
            : ''
      }
    </div>`;
}

function settingsHtml() {
  const base = detail || {};
  const pathField = (key, label, help) => `
    <div class="param-field">
      <label class="param-field-label" for="kb-field-${key}">${ctx.escapeHtml(label)}</label>
      <div class="param-field-control">
        <input class="input" id="kb-field-${key}" data-field="${key}" value="${ctx.escapeHtml(base[key] || '')}" placeholder="D:\\models\\...">
        <button type="button" class="btn btn-secondary btn-sm" data-browse="${key}">${ctx.escapeHtml(t('knowledge.browse'))}</button>
      </div>
      <div class="param-field-help">${ctx.escapeHtml(help)}</div>
    </div>`;
  return `
    ${pathField('embedding_model', t('knowledge.embeddingModel'), t('knowledge.embeddingModelHelp'))}
    ${pathField('ocr_model', t('knowledge.ocrModel'), t('knowledge.ocrModelHelp'))}
    ${pathField('ocr_mmproj', t('knowledge.ocrMmproj'), t('knowledge.ocrMmprojHelp'))}
    ${pathField('rerank_model', t('knowledge.rerankModel'), t('knowledge.rerankModelHelp'))}
    <div class="param-field param-field-switch">
      <label class="param-field-label" for="kb-field-rerank_enabled">${ctx.escapeHtml(t('knowledge.rerankEnabled'))}</label>
      <div class="param-field-control">
        <label class="switch"><input type="checkbox" id="kb-field-rerank_enabled" data-field="rerank_enabled" ${base.rerank_enabled ? 'checked' : ''}/><span class="switch-slider"></span></label>
      </div>
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.rerankEnabledHelp'))}</div>
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-field-chunk_chars">${ctx.escapeHtml(t('knowledge.chunkChars'))}</label>
      <input class="input" id="kb-field-chunk_chars" data-field="chunk_chars" type="number" min="200" max="4000" step="50" value="${Number(base.chunk_chars) || 1200}">
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.chunkCharsHelp'))}</div>
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-field-chunk_overlap">${ctx.escapeHtml(t('knowledge.chunkOverlap'))}</label>
      <input class="input" id="kb-field-chunk_overlap" data-field="chunk_overlap" type="number" min="0" max="2000" step="50" value="${Number(base.chunk_overlap) || 0}">
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-field-top_k">${ctx.escapeHtml(t('knowledge.topK'))}</label>
      <input class="input" id="kb-field-top_k" data-field="top_k" type="number" min="1" max="20" step="1" value="${Number(base.top_k) || 6}">
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.topKHelp'))}</div>
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-field-min_score">${ctx.escapeHtml(t('knowledge.minScore'))}</label>
      <input class="input" id="kb-field-min_score" data-field="min_score" type="number" min="0" max="1" step="0.05" value="${Number(base.min_score) || 0}">
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.minScoreHelp'))}</div>
    </div>
    <div class="param-field param-field-wide">
      <label class="param-field-label" for="kb-field-ocr_prompt">${ctx.escapeHtml(t('knowledge.ocrPrompt'))}</label>
      <input class="input" id="kb-field-ocr_prompt" data-field="ocr_prompt" value="${ctx.escapeHtml(base.ocr_prompt || 'Text Recognition:')}">
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.ocrPromptHelp'))}</div>
    </div>
  `;
}

function renderSummary() {
  const holder = detailEl.querySelector('#kb-summary');
  if (holder && detail) holder.outerHTML = summaryHtml();
}

function renderDocuments() {
  const body = detailEl.querySelector('#kb-doc-body');
  if (!body) return;
  if (!documents.length) {
    body.innerHTML = `<tr><td colspan="4" class="kb-empty-cell">${ctx.escapeHtml(t('knowledge.noDocuments'))}</td></tr>`;
    return;
  }
  body.innerHTML = documents
    .map((doc) => {
      const status = doc.status || 'pending';
      const error = doc.error
        ? `<span class="kb-doc-error" title="${ctx.escapeHtml(doc.error)}">${ctx.escapeHtml(doc.error)}</span>`
        : '';
      return `<tr>
        <td>
          <div class="kb-doc-name" title="${ctx.escapeHtml(doc.path)}">${ctx.escapeHtml(doc.display_name || doc.path)}${
            doc.kind === 'pdf-ocr'
              ? `<span class="kb-doc-ocr" title="${ctx.escapeHtml(t('knowledge.ocrBadgeHelp'))}">${ctx.escapeHtml(t('knowledge.ocrBadge'))}</span>`
              : ''
          }</div>
          <div class="kb-doc-meta">${ctx.escapeHtml(ctx.formatBytes(doc.size_bytes || 0))}</div>
        </td>
        <td>${statusBadge(status)}${error}</td>
        <td>${Number(doc.chunk_count) || 0}</td>
        <td class="kb-doc-actions">
          <button class="btn btn-ghost btn-sm" data-action="preview-doc" data-id="${ctx.escapeHtml(doc.id)}" ${doc.status === 'ready' ? '' : 'disabled'}>${ctx.escapeHtml(t('knowledge.preview'))}</button>
          <button class="btn btn-ghost btn-sm kb-danger-text" data-action="delete-doc" data-id="${ctx.escapeHtml(doc.id)}">${ctx.escapeHtml(t('knowledge.deleteDoc'))}</button>
        </td>
      </tr>`;
    })
    .join('');
}

function onFieldInput(event) {
  if (event.target.closest('[data-field]')) settingsDirty = true;
}

function readSettings() {
  const value = (key) => detailEl.querySelector(`[data-field="${key}"]`);
  const text = (key) => String(value(key)?.value || '');
  return {
    embedding_model: text('embedding_model').trim(),
    ocr_model: text('ocr_model').trim(),
    ocr_mmproj: text('ocr_mmproj').trim(),
    rerank_model: text('rerank_model').trim(),
    ocr_prompt: text('ocr_prompt'),
    chunk_chars: Number(text('chunk_chars')) || 1200,
    chunk_overlap: Number(text('chunk_overlap')) || 0,
    top_k: Number(text('top_k')) || 6,
    min_score: Number(text('min_score')) || 0,
    rerank_enabled: Boolean(value('rerank_enabled')?.checked),
  };
}

async function saveSettings(button) {
  if (!detail) return;
  setButtonBusy(button, true);
  try {
    detail = await ctx.Api.updateKnowledge(detail.id, readSettings());
    settingsDirty = false;
    ctx.showToast(t('knowledge.saved'), 'success');
    renderSummary();
    await refresh({ quiet: true });
  } catch (error) {
    ctx.showToast(`${t('knowledge.saveFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    setButtonBusy(button, false);
  }
}

async function addPaths(paths) {
  if (!detail || !paths.length) return false;
  try {
    const result = await ctx.Api.addKnowledgeDocuments(detail.id, paths);
    ctx.showToast(
      t('knowledge.added', { added: result.added, updated: result.updated }),
      'success'
    );
    await loadDetail({ full: false });
    // The worker may not have flipped the base to "indexing" by the time this
    // response arrives; poll once soon instead of leaving a stale idle panel.
    setTimeout(() => loadDetail({ full: false, quiet: true }), 800);
    return true;
  } catch (error) {
    ctx.showToast(`${t('knowledge.addFailed')}: ${localizeBackendError(error.message)}`, 'error');
    return false;
  }
}

async function pickAndAdd(kind) {
  try {
    const command = kind === 'folder' ? 'open_directory_dialog' : 'open_file_dialog';
    const path = await tauriInvoke(command);
    if (path) await addPaths([path]);
  } catch (error) {
    ctx.showToast(`${t('params.dialogFailed')}: ${error.message || error}`, 'error');
  }
}

function openPathsModal() {
  const body = `<textarea class="textarea kb-paths-input" id="kb-paths-input" rows="6"
    placeholder="${ctx.escapeHtml(t('knowledge.addPathsPlaceholder'))}"
    aria-label="${ctx.escapeHtml(t('knowledge.addPathsPlaceholder'))}"></textarea>
    <div class="param-field-help">${ctx.escapeHtml(t('knowledge.addPathsHint'))}</div>`;
  ctx.showModal(t('knowledge.addPathsTitle'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('knowledge.add'),
      class: 'btn-primary',
      onClick: async () => {
        const text = document.getElementById('kb-paths-input')?.value || '';
        const paths = text
          .split(/\r?\n/)
          .map((line) => line.trim().replace(/^"(.*)"$/, '$1'))
          .filter(Boolean);
        if (!paths.length) {
          ctx.showToast(t('knowledge.addPathsEmpty'), 'warning');
          return false;
        }
        if (paths.length > 50) {
          ctx.showToast(t('knowledge.addPathsLimit'), 'warning');
          return false;
        }
        // 失败时返回 false：弹窗必须留在原样，否则用户输的路径和错误一起消失（§2.20）。
        return await addPaths(paths);
      },
    },
  ]);
}

function openCreateModal() {
  const body = `
    <div class="param-field">
      <label class="param-field-label" for="kb-new-name">${ctx.escapeHtml(t('knowledge.newName'))}</label>
      <input class="input" id="kb-new-name" maxlength="120" placeholder="${ctx.escapeHtml(t('knowledge.newNamePlaceholder'))}">
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-new-description">${ctx.escapeHtml(t('knowledge.newDescription'))}</label>
      <input class="input" id="kb-new-description" maxlength="2000">
    </div>
    <div class="param-field">
      <label class="param-field-label" for="kb-new-embedding">${ctx.escapeHtml(t('knowledge.embeddingModel'))}</label>
      <input class="input" id="kb-new-embedding" placeholder="D:\\models\\Qwen3-Embedding-0.6B-Q8_0.gguf">
      <div class="param-field-help">${ctx.escapeHtml(t('knowledge.embeddingModelHelp'))}</div>
    </div>`;
  ctx.showModal(t('knowledge.new'), body, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('knowledge.create'),
      class: 'btn-primary',
      onClick: async () => {
        const name = document.getElementById('kb-new-name')?.value.trim() || '';
        if (!name) {
          ctx.showToast(t('knowledge.nameRequired'), 'warning');
          return false;
        }
        try {
          const base = await ctx.Api.createKnowledge({
            name,
            description: document.getElementById('kb-new-description')?.value || '',
            embedding_model: (document.getElementById('kb-new-embedding')?.value || '').trim(),
          });
          selectedId = base.id;
          settingsDirty = false;
          ctx.showToast(t('knowledge.created'), 'success');
          await refresh();
          return true;
        } catch (error) {
          ctx.showToast(`${t('knowledge.createFailed')}: ${localizeBackendError(error.message)}`, 'error');
          return false;
        }
      },
    },
  ]);
}

async function deleteBase() {
  if (!detail) return;
  const confirmed = await ctx.showConfirm(
    t('knowledge.deleteBaseConfirm', { name: detail.name }),
    t('knowledge.deleteBase')
  );
  if (!confirmed) return;
  try {
    await ctx.Api.deleteKnowledge(detail.id);
    ctx.showToast(t('knowledge.deleted'), 'success');
    selectedId = null;
    settingsDirty = false;
    detail = null;
    documents = [];
    await refresh();
  } catch (error) {
    ctx.showToast(`${t('knowledge.deleteFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function deleteDocument(documentId) {
  if (!detail) return;
  const doc = documents.find((item) => item.id === documentId);
  const confirmed = await ctx.showConfirm(
    t('knowledge.deleteDocConfirm', { name: doc?.display_name || documentId }),
    t('knowledge.deleteDoc')
  );
  if (!confirmed) return;
  try {
    await ctx.Api.deleteKnowledgeDocument(detail.id, documentId);
    ctx.showToast(t('knowledge.docDeleted'), 'success');
    await loadDetail({ full: false });
  } catch (error) {
    ctx.showToast(`${t('knowledge.deleteFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function scheduleIndex(force) {
  if (!detail) return;
  if (force) {
    const confirmed = await ctx.showConfirm(t('knowledge.reindexConfirm'), t('knowledge.reindexForce'));
    if (!confirmed) return;
  }
  try {
    await ctx.Api.indexKnowledge(detail.id, force);
    ctx.showToast(t('knowledge.scheduled'), 'success');
    await loadDetail({ full: false });
    setTimeout(() => loadDetail({ full: false, quiet: true }), 800);
  } catch (error) {
    ctx.showToast(`${t('knowledge.indexFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function cancelIndex() {
  if (!detail) return;
  const button = detailEl.querySelector('[data-action="cancel-index"]');
  if (button) button.disabled = true;
  try {
    await ctx.Api.cancelKnowledge(detail.id);
    ctx.showToast(t('knowledge.cancelRequested'), 'info');
    await loadDetail({ full: true });
  } catch (error) {
    if (button) button.disabled = false;
    ctx.showToast(`${t('knowledge.cancelFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function previewDocument(documentId) {
  const doc = documents.find((item) => item.id === documentId);
  const title = doc?.display_name || t('knowledge.chunksTitle');
  const content = `<div id="kb-chunks-body" class="kb-chunks-body">${ctx.escapeHtml(t('knowledge.loading'))}</div>`;
  ctx.showModal(`${title} · ${t('knowledge.chunksTitle')}`, content, [
    { label: t('app.close'), class: 'btn-secondary' },
  ]);
  try {
    const response = await ctx.Api.listKnowledgeChunks(detail.id, documentId);
    const chunks = Array.isArray(response?.chunks) ? response.chunks : [];
    const holder = document.getElementById('kb-chunks-body');
    if (!holder) return;
    if (!chunks.length) {
      holder.textContent = t('knowledge.noChunks');
      return;
    }
    holder.innerHTML = chunks
      .map(
        (chunk) => `<article class="kb-chunk">
          <div class="kb-chunk-meta">#${Number(chunk.ordinal) + 1}${chunk.page ? ` · ${ctx.escapeHtml(t('chat.sourcePage', { page: chunk.page }))}` : ''} · ${Number(chunk.token_estimate) || 0} tokens</div>
          <div class="kb-chunk-text">${ctx.escapeHtml(chunk.text || '')}</div>
        </article>`
      )
      .join('');
  } catch (error) {
    const holder = document.getElementById('kb-chunks-body');
    if (holder) holder.textContent = `${t('knowledge.loadChunksFailed')}: ${localizeBackendError(error.message)}`;
  }
}

async function runSearch() {
  if (!detail) return;
  const input = detailEl.querySelector('#kb-search-input');
  const query = String(input?.value || '').trim();
  if (!query) return;
  searchLoading = true;
  searchError = '';
  searchResults = null;
  renderSearchResults();
  try {
    const response = await ctx.Api.searchKnowledge(detail.id, query);
    searchResults = Array.isArray(response?.results) ? response.results : [];
  } catch (error) {
    searchError = localizeBackendError(error.message);
  } finally {
    searchLoading = false;
    renderSearchResults();
  }
}

function renderSearchResults() {
  const holder = detailEl.querySelector('#kb-search-results');
  if (!holder) return;
  if (searchLoading) {
    holder.innerHTML = `<div class="kb-search-state" role="status">${ctx.escapeHtml(t('knowledge.searching'))}</div>`;
    return;
  }
  if (searchError) {
    holder.innerHTML = `<div class="kb-search-state kb-error" role="alert">${ctx.escapeHtml(searchError)}</div>`;
    return;
  }
  if (!searchResults) {
    holder.innerHTML = '';
    return;
  }
  if (!searchResults.length) {
    holder.innerHTML = `<div class="kb-search-state">${ctx.escapeHtml(t('knowledge.searchNoResults'))}</div>`;
    return;
  }
  holder.innerHTML = `<div class="kb-search-count">${ctx.escapeHtml(t('knowledge.searchCount', { count: searchResults.length }))}</div>` +
    searchResults
      .map(
        (item) => `<article class="kb-result">
          <div class="kb-result-head">
            <span class="kb-result-number">[${Number(item.number) || 0}]</span>
            <span class="kb-result-title">${ctx.escapeHtml(item.title || '')}</span>
            ${item.page ? `<span class="kb-result-page">${ctx.escapeHtml(t('chat.sourcePage', { page: item.page }))}</span>` : ''}
            <span class="kb-result-score">${Number(item.score || 0).toFixed(3)}</span>
          </div>
          <div class="kb-result-snippet">${ctx.escapeHtml(item.snippet || '')}</div>
          <div class="kb-result-path">${ctx.escapeHtml(item.heading || item.path || '')}</div>
        </article>`
      )
      .join('');
}

async function unloadServices() {
  try {
    const response = await ctx.Api.unloadKnowledgeServices();
    ctx.showToast(t('knowledge.unloaded', { count: response.released || 0 }), 'success');
  } catch (error) {
    ctx.showToast(`${t('knowledge.unloadFailed')}: ${localizeBackendError(error.message)}`, 'error');
  }
}

async function browseField(key) {
  try {
    const path = await tauriInvoke('open_file_dialog');
    const input = detailEl.querySelector(`[data-field="${key}"]`);
    if (path && input) {
      input.value = path;
      settingsDirty = true;
    }
  } catch (error) {
    ctx.showToast(`${t('params.dialogFailed')}: ${error.message || error}`, 'error');
  }
}

function onClick(event) {
  const browse = event.target.closest('[data-browse]');
  if (browse) {
    browseField(browse.dataset.browse);
    return;
  }
  const target = event.target.closest('[data-action]');
  if (!target) return;
  const action = target.dataset.action;
  if (action === 'add-file') pickAndAdd('file');
  else if (action === 'add-folder') pickAndAdd('folder');
  else if (action === 'add-paths') openPathsModal();
  else if (action === 'save-settings') saveSettings(target);
  else if (action === 'reindex') scheduleIndex(false);
  else if (action === 'reindex-force') scheduleIndex(true);
  else if (action === 'delete-base') deleteBase();
  else if (action === 'cancel-index') cancelIndex();
  else if (action === 'preview-doc') previewDocument(target.dataset.id);
  else if (action === 'delete-doc') deleteDocument(target.dataset.id);
}

function schedulePoll() {
  clearPoll();
  const busy =
    detail?.status === 'indexing' ||
    documents.some((doc) => ['pending', 'indexing', 'queued', 'staged'].includes(doc.status)) ||
    bases.some((base) => base.status === 'indexing');
  if (!busy) return;
  pollTimer = setTimeout(() => {
    pollTimer = null;
    loadDetail({ full: false, quiet: true });
    refreshBasesQuietly();
  }, POLL_MS);
}

async function refreshBasesQuietly() {
  try {
    const response = await ctx.Api.listKnowledge();
    bases = Array.isArray(response?.bases) ? response.bases : [];
    ctx.refreshList?.();
  } catch {
    /* the next poll retries */
  }
}

function clearPoll() {
  if (pollTimer) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

