// 模型库页面
// 二级面板 = 模型列表（扫描目录 + 搜索 + 能力徽章）
// 正文 = 选中模型的能力详情与操作
//
// 数据分两层，成本差一百多倍，刻意不混用：
//   * 列表用扫描接口附带的**能力摘要**（快速探测，0.6~0.9 ms/个，一次请求拿全）
//   * 详情在选中时单独请求 `/api/system/capabilities?model=`（会探测同目录配套文件，
//     才能判断"独立 MTP 头"还是"普通草稿模型"）
// 列表若去付详情的代价，一个几百模型的目录会卡几十秒。

import { t, localizeBackendError } from './i18n.mjs';
import { groupModelsByKey, instanceNameFromFilename } from './runtime.mjs';
import { confirmLaunch } from './preflight.mjs';
import { renderList, emptyStateHtml } from './panel_views.mjs';

export const id = 'models';
export const title = '模型库';

let ctx = null;
let root = null;
let models = [];
let directory = '';
let selectedPath = null;
let searchKeyword = '';
let truncated = false;
let loading = false;
// 详情的能力结果缓存：切换列表项来回点时不必重发请求
const detailCache = new Map();

// mmproj / 推测解码头**不是能加载的模型**，与可加载模型并列会误导，
// 而且它们的标签会把 252px 的面板挤成三行。默认收起，可切换显示。
const SHOW_ALL_KEY = 'lunarshroud_models_show_all';
const GROUP_MODELS_KEY = 'lunarshroud_models_group_by_model';
let showAll = readShowAll();
let groupByModel = readGroupByModel();
let recentPaths = new Set();

function readShowAll() {
  try {
    return localStorage.getItem(SHOW_ALL_KEY) === '1';
  } catch {
    return false;
  }
}


function readGroupByModel() {
  try {
    return localStorage.getItem(GROUP_MODELS_KEY) === '1';
  } catch {
    return false;
  }
}

function writeShowAll(value) {
  try {
    localStorage.setItem(SHOW_ALL_KEY, value ? '1' : '0');
  } catch {
    // 忽略持久化失败
  }
}

function writeGroupByModel(value) {
  try {
    localStorage.setItem(GROUP_MODELS_KEY, value ? '1' : '0');
  } catch {
    // 忽略持久化失败
  }
}

function manifestKey(model) {
  const manifest = model && model.manifest;
  return manifest && manifest.found && manifest.model_key ? String(manifest.model_key) : '';
}

const DIR_GROUP_PREFIX = 'dir:';

function relativeDirectory(modelPath) {
  const normalized = String(modelPath || '').replace(/\\/g, '/');
  const scanRoot = String(directory || '').replace(/\\/g, '/').replace(/\/+$/, '');
  let relative = normalized;
  if (scanRoot && normalized.toLowerCase().startsWith(`${scanRoot.toLowerCase()}/`)) {
    relative = normalized.slice(scanRoot.length + 1);
  }
  const parts = relative.split('/');
  parts.pop();
  return parts.join('/') || '.';
}

/**
 * Real model folders often contain several quants (or an unkeyed mmproj)
 * without a model.yaml. Group those by their folder so the toggle still has
 * value; a flat directory of one-file models stays flat.
 */
function groupKeyFactory(list) {
  const dirCounts = new Map();
  for (const model of list) {
    if (manifestKey(model)) continue;
    const dir = relativeDirectory(model.path);
    dirCounts.set(dir, (dirCounts.get(dir) || 0) + 1);
  }
  return (model) => {
    const key = manifestKey(model);
    if (key) return key;
    const dir = relativeDirectory(model.path);
    return (dirCounts.get(dir) || 0) > 1 ? `${DIR_GROUP_PREFIX}${dir}` : '';
  };
}

function hasGroupableModels(list) {
  if (list.some((model) => manifestKey(model))) return true;
  const dirCounts = new Map();
  for (const model of list) {
    if (manifestKey(model)) continue;
    const dir = relativeDirectory(model.path);
    dirCounts.set(dir, (dirCounts.get(dir) || 0) + 1);
    if ((dirCounts.get(dir) || 0) > 1) return true;
  }
  return false;
}

function groupLabel(key) {
  if (!key) return t('models.groupUngrouped');
  if (key.startsWith(DIR_GROUP_PREFIX)) {
    return t('models.groupDirectory', { name: key.slice(DIR_GROUP_PREFIX.length) });
  }
  return key;
}

function isLoadable(model) {
  const caps = model.capabilities || {};
  return !(caps.is_mmproj || caps.is_assistant_model);
}

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('models.title'))}</h1>
        <div class="page-subtitle" id="models-subtitle">${ctx.escapeHtml(t('models.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="btn-models-rescan">${ctx.escapeHtml(t('models.rescan'))}</button>
      </div>
    </div>
    <div id="model-detail"></div>
  `;
  root.querySelector('#btn-models-rescan').addEventListener('click', () => refresh());
  // 正文不再有模型列表：列表在侧栏。旧版的"卡片 / 表格"切换随重复列表一起
  // 删掉了 —— 同一批模型在左栏和正文各列一遍，用户要点两次才知道哪份可操作。
  // 详情里的"快速启动"按钮仍走委托（它嵌在详情卡里，与旧卡片无关）。
  root.addEventListener('click', (event) => {
    const quick = event.target.closest('[data-quick-launch]');
    if (quick) quickLaunch(quick.dataset.quickLaunch, quick);
  });
}

export function onShow() {
  refresh();
}

export function onHide() {}

// ========== 扫描 ==========

async function refresh() {
  if (loading) return;
  loading = true;
  try {
    const [data, recent] = await Promise.all([
      ctx.Api.scanModels(undefined, true, true),
      ctx.Api.listHistory(30).catch(() => []),
    ]);
    models = Array.isArray(data.models) ? data.models : [];
    directory = data.directory || '';
    recentPaths = new Set((Array.isArray(recent) ? recent : []).map((item) => item.model_path).filter(Boolean));
    truncated = !!(data.capabilities_truncated || data.manifests_truncated);

    const subtitle = root.querySelector('#models-subtitle');
    if (subtitle) {
      subtitle.textContent = directory
        ? t('models.subtitleWithDir', { count: models.length, dir: directory })
        : t('models.subtitleNoDir');
    }

    // 选中项失效则退回第一项
    if (!models.some((m) => m.path === selectedPath)) {
      selectedPath = models.length ? models[0].path : null;
    }
    ctx.refreshList?.();
    await renderDetail();
  } catch (e) {
    ctx.showToast(`${t('models.scanFailed')}: ${localizeBackendError(e.message)}`, 'error');
  } finally {
    loading = false;
  }
}

// ========== 二级面板：模型列表 ==========

/** 侧栏列表的标题。 */
export function sidebarListTitle() {
  return t('nav.models');
}

export function renderSidebarList(el) {
  const hiddenCount = models.filter((m) => !isLoadable(m)).length;
  const groupable = hasGroupableModels(showAll ? models : models.filter(isLoadable));
  el.innerHTML = `
    <div class="panel-search">
      <input type="search" id="model-search" class="input" placeholder="${ctx.escapeHtml(
        t('models.searchPh')
      )}" value="${ctx.escapeHtml(searchKeyword)}" autocomplete="off"/>
    </div>
    ${
      hiddenCount || groupable
        ? `<div class="panel-filter">
             ${
               hiddenCount
                 ? `<label class="panel-toggle">
               <input type="checkbox" id="model-show-all" ${showAll ? 'checked' : ''}/>
               <span>${ctx.escapeHtml(t('models.showCompanions', { count: hiddenCount }))}</span>
             </label>`
                 : ''
             }
             ${
               groupable
                 ? `<label class="panel-toggle">
               <input type="checkbox" id="model-group-toggle" ${groupByModel ? 'checked' : ''}/>
               <span>${ctx.escapeHtml(t('models.groupBy'))}</span>
             </label>`
                 : ''
             }
           </div>`
        : ''
    }
    <div id="model-list-body"></div>
  `;
  const toggle = el.querySelector('#model-show-all');
  if (toggle) {
    toggle.addEventListener('change', (e) => {
      showAll = e.target.checked;
      writeShowAll(showAll);
      renderSidebarList(el);
    });
  }
  const groupToggle = el.querySelector('#model-group-toggle');
  if (groupToggle) {
    groupToggle.addEventListener('change', (e) => {
      groupByModel = e.target.checked;
      writeGroupByModel(groupByModel);
      renderListInto(el.querySelector('#model-list-body'));
    });
  }
  const input = el.querySelector('#model-search');
  // 只重绘列表本身，输入框不动 —— 否则每敲一个字都会丢焦点
  input.addEventListener('input', () => {
    searchKeyword = input.value.trim().toLowerCase();
    renderListInto(el.querySelector('#model-list-body'));
  });
  renderListInto(el.querySelector('#model-list-body'));
}

function visibleModels() {
  const keyword = searchKeyword;
  const base = showAll ? models : models.filter(isLoadable);
  return keyword
    ? base.filter(
        (m) =>
          m.filename.toLowerCase().includes(keyword) ||
          String((m.capabilities && m.capabilities.architecture) || '').toLowerCase().includes(keyword)
      )
    : base;
}

async function quickLaunch(path, button) {
  const model = models.find((item) => item.path === path);
  if (!model || !isLoadable(model)) return;
  button.disabled = true;
  const original = button.textContent;
  button.textContent = t('models.preparing');
  try {
    const payload = await ctx.Api.getCapabilities(path);
    const caps = (payload && payload.model) || model.capabilities || {};
    if (selectedPath !== path) {
      selectedPath = path;
      ctx.refreshList?.();
      await renderDetail();
    }
    await launch(model, caps.probed ? caps : null, button);
  } catch (error) {
    ctx.showToast(`${t('models.capabilityFailed')}: ${localizeBackendError(error.message)}`, 'error');
  } finally {
    if (button.isConnected) {
      button.disabled = false;
      button.textContent = original;
    }
  }
}

function renderListInto(container) {
  if (!container) return;
  if (!models.length) {
    // 列表空态与详情空态共用统一组件（细分"目录未配置"与"无匹配"两种引导）
    container.innerHTML = emptyStateHtml(ctx, {
      icon: 'layers',
      title: t(directory ? 'models.empty' : 'models.noDir'),
      sub: t(directory ? 'models.emptySub' : 'models.noDirSub'),
    });
    return;
  }
  const visible = visibleModels();

  const listItem = (m) => {
    const caps = m.capabilities || {};
    return {
      key: m.path,
      label: m.filename,
      // 徽标放在元信息行：标签行被长文件名挤占后，徽标在那里会折行
      metaHtml: `${badgeHtml(caps)}<span class="panel-list-meta-text">${ctx.escapeHtml(
        summaryLine(m, caps)
      )}</span>`,
    };
  };

  let items = visible.map(listItem);
  const keyOf = groupKeyFactory(visible);
  if (groupByModel && visible.some((model) => keyOf(model))) {
    const groups = groupModelsByKey(visible, keyOf);
    // Manifest groups first, then folder groups, unkeyed files last.
    groups.sort((a, b) => {
      const aDir = a.key.startsWith(DIR_GROUP_PREFIX);
      const bDir = b.key.startsWith(DIR_GROUP_PREFIX);
      if (aDir !== bDir) return aDir ? 1 : -1;
      if (!a.key !== !b.key) return a.key ? -1 : 1;
      return groupLabel(a.key).localeCompare(groupLabel(b.key));
    });
    items = groups.flatMap((group) =>
      group.models.map((m) => ({
        ...listItem(m),
        group: group.key,
        groupLabel: groupLabel(group.key),
      }))
    );
  }

  renderList(container, ctx, {
    items,
    activeKey: selectedPath,
    onSelect: (key) => {
      selectedPath = key;
      ctx.refreshList?.();
      renderDetail();
    },
    emptyText: t('models.empty'),
  });
}

function summaryLine(model, caps) {
  const parts = [model.quant, model.size_human];
  if (caps.probed && caps.context_length) parts.push(`${caps.context_length} ctx`);
  if (truncated) parts.push(t('models.truncated'));
  return parts.filter(Boolean).join(' · ');
}

function badgeHtml(caps) {
  if (!caps || !caps.probed) return '';
  // .tag = 主色高亮（这是"能力"），.tag-default = 中性标记（这是"文件种类"）
  if (caps.is_mmproj) return `<span class="tag tag-default">${ctx.escapeHtml(t('models.badgeMmproj'))}</span>`;
  if (caps.is_assistant_model) {
    return `<span class="tag tag-default">${ctx.escapeHtml(t('models.badgeAssistant'))}</span>`;
  }
  const tags = [];
  if (caps.truncated) {
    tags.push(`<span class="tag tag-warn">${ctx.escapeHtml(t('models.badgeTruncated'))}</span>`);
  }
  if (caps.provides_builtin_mtp) {
    tags.push(`<span class="tag">${ctx.escapeHtml(t('models.badgeBuiltinMtp'))}</span>`);
  }
  if (caps.is_embedding_model) {
    tags.push(`<span class="tag tag-default">${ctx.escapeHtml(t('models.badgeEmbedding'))}</span>`);
  }
  if (caps.is_reranker_model) {
    tags.push(`<span class="tag tag-default">${ctx.escapeHtml(t('models.badgeReranker'))}</span>`);
  }
  return tags.join(' ');
}

// ========== 正文：选中模型的能力详情 ==========

async function renderDetail() {
  const el = root.querySelector('#model-detail');
  if (!el) return;

  const model = models.find((m) => m.path === selectedPath);
  if (!model) {
    el.innerHTML = `<div class="card">${emptyStateHtml(ctx, {
      icon: 'layers',
      title: t('models.selectHint'),
    })}</div>`;
    return;
  }

  const caps = model.capabilities || {};
  el.innerHTML =
    headerCard(model, caps) +
    `<div id="model-caps-body"><div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(
      t('models.loadingCaps')
    )}</div></div>` +
    `<div id="model-manifest-body"></div>`;

  el.querySelector('#btn-copy-model-path').addEventListener('click', () => {
    ctx.copyToClipboard(model.path);
  });

  // 清单是可选叠加信息，不能阻塞能力详情；异步渲染，失败按“未找到”展示。
  ctx.Api.getModelManifest(model.path)
    .then((payload) => (payload && payload.manifest) || { found: false })
    .catch(() => ({ found: false, error: t('models.manifestFailed') }))
    .then((manifest) => {
      if (selectedPath !== model.path) return;
      const slot = el.querySelector('#model-manifest-body');
      if (!slot) return;
      slot.innerHTML = manifestCardHtml(manifest);
      slot
        .querySelector('[data-edit-manifest]')
        ?.addEventListener('click', () => openManifestEditor(model));
    });

  // 行为**先绑、数据后取**：header 卡片是同步渲染的，而精确能力要等一次异步
  // 请求（冷启动时后端要跑一次 llama-server --help，约 0.7s）。等请求回来才绑
  // 的话，这段时间里点按钮是死的 —— 用户看到按钮却点不动。
  // 需要能力的那两个动作改为从 state 里**延迟读取**，并在必要时等请求结束。
  const state = { model, precise: null, pending: null };
  bindActions(el, state);

  // 详情要精确分类配套文件，所以按需请求一次
  let precise = detailCache.get(model.path);
  if (precise) {
    state.precise = precise;
  } else {
    state.pending = (async () => {
      try {
        const payload = await ctx.Api.getCapabilities(model.path);
        precise = (payload && payload.model) || null;
        if (precise) {
          // 上限防止长会话无界增长
          if (detailCache.size >= 64) detailCache.clear();
          detailCache.set(model.path, precise);
        }
      } catch (e) {
        precise = null;
        ctx.showToast(`${t('models.capabilityFailed')}: ${localizeBackendError(e.message)}`, 'warning');
      }
      state.precise = precise;
      return precise;
    })();
    await state.pending;
  }

  // 等待期间用户可能已经切走
  if (selectedPath !== model.path) return;
  const body = el.querySelector('#model-caps-body');
  if (!body) return;
  // probed=false（探测失败）与 null 都走"不可解析"分支：把未探测结果当能力卡
  // 渲染会显示误导性的空能力。
  body.innerHTML = precise && precise.probed ? capabilitiesHtml(precise) : unparsableHtml(caps);
}

function headerCard(model, caps) {
  const canLaunch = !caps.is_mmproj && !caps.is_assistant_model;
  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(model.filename)}</span>
        <div class="card-actions">
          <button class="btn btn-sm btn-secondary" id="btn-copy-model-path">${ctx.escapeHtml(
            t('models.copyPath')
          )}</button>
        </div>
      </div>
      <div class="text-mono text-sm" break-all text-tertiary>${ctx.escapeHtml(
        model.path
      )}</div>
      ${
        canLaunch
          ? `<div class="page-actions" mt-3>
               <button class="btn btn-secondary" id="btn-use-in-editor">${ctx.escapeHtml(
                 t('models.useInEditor')
               )}</button>
               <button class="btn btn-primary" id="btn-launch-model">${ctx.escapeHtml(
                 t('models.launchNow')
               )}</button>
             </div>`
          : `<div class="form-help" mt-3>${ctx.escapeHtml(
              caps.is_mmproj ? t('models.mmprojNotLaunchable') : t('models.assistantNotLaunchable')
            )}</div>`
      }
    </div>
  `;
}

function unparsableHtml(caps) {
  return `<div class="card"><div class="card-header"><span class="card-title">${ctx.escapeHtml(
    t('models.capsTitle')
  )}</span></div><div class="form-help">${ctx.escapeHtml(
    caps.error || t('models.capabilityFailed')
  )}</div></div>`;
}

function capabilitiesHtml(caps) {
  const rows = [
    [t('models.arch'), caps.architecture],
    [t('models.quant'), caps.file_type_name],
    [t('models.ctx'), caps.context_length],
    [t('models.layers'), caps.block_count],
    [t('models.embedding'), caps.embedding_length],
    [t('models.tensors'), caps.tensor_count],
    [t('models.ropeBase'), caps.rope_freq_base],
  ].filter(([, value]) => value !== null && value !== undefined && value !== '');

  const badges = [];
  if (caps.truncated) badges.push([t('models.badgeTruncated'), 'tag tag-warn']);
  if (caps.provides_builtin_mtp) badges.push([t('models.badgeBuiltinMtp'), 'tag']);
  if (caps.is_embedding_model) badges.push([t('models.badgeEmbedding'), 'tag tag-default']);
  if (caps.is_reranker_model) badges.push([t('models.badgeReranker'), 'tag tag-default']);
  if (caps.has_vision_encoder) badges.push([t('models.badgeVision'), 'tag']);
  if (caps.is_assistant_model) badges.push([t('models.badgeAssistant'), 'tag tag-default']);
  if (caps.is_mmproj) badges.push([t('models.badgeMmproj'), 'tag tag-default']);

  return `
    <div class="card">
      <div class="card-header"><span class="card-title">${ctx.escapeHtml(t('models.capsTitle'))}</span></div>
      ${
        badges.length
          ? `<div class="mb-2">${badges
              .map(([label, cls]) => `<span class="${cls}">${ctx.escapeHtml(label)}</span>`)
              .join('')}</div>`
          : ''
      }
      <div class="table-wrap">
        <table class="table">
          <tbody>
            ${rows
              .map(
                ([label, value]) =>
                  `<tr><td class="text-muted">${ctx.escapeHtml(label)}</td><td class="text-mono">${ctx.escapeHtml(
                    String(value)
                  )}</td></tr>`
              )
              .join('')}
          </tbody>
        </table>
      </div>
    </div>
    ${companionsHtml(caps)}
    ${specModesHtml(caps)}
  `;
}

function manifestCardHtml(manifest) {
  const title = t('models.manifestTitle');
  if (!manifest || !manifest.found) {
    const detail = manifest && manifest.error ? manifest.error : t('models.manifestNone');
    return `<div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(title)}</span>
        <div class="card-actions">
          <button type="button" class="btn btn-sm btn-secondary" data-edit-manifest>${ctx.escapeHtml(
            t('models.manifestEdit')
          )}</button>
          <span class="tag tag-default">${ctx.escapeHtml(t('models.manifestNoneBadge'))}</span>
        </div>
      </div>
      <div class="form-help">${ctx.escapeHtml(detail)}</div>
    </div>`;
  }
  const meta = manifest.metadata_overrides || {};
  const requires = manifest.requires || {};
  const companions = manifest.companions || {};
  const warnings = Array.isArray(manifest.warnings) ? manifest.warnings : [];
  const companionRows = Object.entries(companions).flatMap(([kind, files]) =>
    (Array.isArray(files) ? files : []).map(
      (file) => `<tr><td class="text-muted">${ctx.escapeHtml(kind)}</td><td class="text-mono">${ctx.escapeHtml(file)}</td></tr>`
    )
  );
  const requireRows = Object.entries(requires)
    .filter(([, value]) => value !== null && value !== undefined && value !== '')
    .map(
      ([key, value]) =>
        `<tr><td class="text-muted">${ctx.escapeHtml(key)}</td><td class="text-mono">${ctx.escapeHtml(
          Array.isArray(value) ? value.join(', ') : String(value)
        )}</td></tr>`
    );
  const rows = [
    [t('models.manifestKey'), manifest.model_key || '-'],
    [t('models.manifestSource'), manifest.source || '-'],
    [t('models.arch'), (meta.architectures || []).join(', ') || '-'],
    [t('models.ctx'), (meta.contextLengths || []).join(', ') || '-'],
    [t('models.manifestParams'), (meta.paramsStrings || []).join(', ') || '-'],
    [t('models.manifestVision'), meta.vision === undefined ? '-' : String(meta.vision)],
    [t('models.manifestToolUse'), meta.trainedForToolUse === undefined ? '-' : String(meta.trainedForToolUse)],
    [t('models.manifestMinMemory'), meta.minMemoryUsageBytes ? ctx.formatBytes(meta.minMemoryUsageBytes) : '-'],
  ];
  return `<div class="card">
    <div class="card-header">
      <span class="card-title">${ctx.escapeHtml(title)}</span>
      <div class="card-actions">
        <button type="button" class="btn btn-sm btn-secondary" data-edit-manifest>${ctx.escapeHtml(
          t('models.manifestEdit')
        )}</button>
        <span class="tag">${ctx.escapeHtml(manifest.source || 'manifest')}</span>
      </div>
    </div>
    <div class="table-wrap">
      <table class="table"><tbody>
        ${rows
          .map(
            ([label, value]) =>
              `<tr><td class="text-muted">${ctx.escapeHtml(label)}</td><td class="text-mono">${ctx.escapeHtml(
                String(value)
              )}</td></tr>`
          )
          .join('')}
      </tbody></table>
    </div>
    ${companionRows.length ? `<div class="mt-2 text-sm"><strong>${ctx.escapeHtml(t('models.manifestCompanions'))}</strong></div><div class="table-wrap mt-2"><table class="table"><tbody>${companionRows.join('')}</tbody></table></div>` : ''}
    ${requireRows.length ? `<div class="mt-2 text-sm"><strong>${ctx.escapeHtml(t('models.manifestRequires'))}</strong></div><div class="table-wrap mt-2"><table class="table"><tbody>${requireRows.join('')}</tbody></table></div>` : ''}
    ${warnings.length ? `<div class="param-field-warn mt-2">${warnings.map((w) => ctx.escapeHtml(w)).join('<br/>')}</div>` : ''}
    <div class="form-help mt-2">${ctx.escapeHtml(t('models.manifestOverlayHint'))}</div>
  </div>`;
}

function manifestTemplate(model, format) {
  const stem = String(model.filename || model.path || 'model')
    .replace(/\.gguf$/i, '')
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, '-')
    .replace(/^-+|-+$/g, '') || 'model';
  const key = `local/${stem}`;
  if (format === 'json') {
    return JSON.stringify({ model: key }, null, 2) + '\n';
  }
  return `model: ${key}\n# 可选项：\n# metadataOverrides:\n#   architectures: [qwen35]\n#   contextLengths: [32768]\n# x-lunarshroud:\n#   companions:\n#     draft: draft.gguf\n#   requires:\n#     min_llama_cpp_build: 1\n`;
}

function askManifest(message, title) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    ctx.showModal(
      title || t('models.manifestEditor'),
      `<div class="diag-intro">${ctx.escapeHtml(message)}</div>`,
      [
        { label: t('app.cancel'), class: 'btn-secondary', onClick: () => done(false) },
        { label: t('app.ok'), class: 'btn-primary', onClick: () => done(true) },
      ],
      { stack: true, onClose: () => done(false) }
    );
  });
}

function manifestPreviewHtml(validation) {
  const warnings = Array.isArray(validation?.warnings) ? validation.warnings : [];
  const manifest = validation?.manifest || {};
  return `
    <div class="manifest-preview ${validation?.found ? 'ok' : 'bad'}">
      <div><strong>${ctx.escapeHtml(t('models.manifestTarget'))}</strong>
        <span class="text-mono">${ctx.escapeHtml(validation?.name || '')}</span></div>
      <div><strong>${ctx.escapeHtml(t('models.manifestKey'))}</strong>
        <span class="text-mono">${ctx.escapeHtml(manifest.model_key || '-')}</span></div>
      ${
        warnings.length
          ? `<div class="manifest-preview-warnings"><strong>${ctx.escapeHtml(
              t('models.manifestWarnings')
            )}</strong><ul>${warnings
              .map((item) => `<li>${ctx.escapeHtml(item)}</li>`)
              .join('')}</ul></div>`
          : ''
      }
      <div class="form-help">${ctx.escapeHtml(t('models.manifestEditorHint'))}</div>
    </div>`;
}

async function openManifestEditor(model) {
  let raw = { found: false, format: 'yaml', content: '' };
  try {
    raw = await ctx.Api.getModelManifestRaw(model.path);
  } catch (error) {
    ctx.showToast(`${t('models.manifestReadFailed')}: ${localizeBackendError(error.message)}`, 'error');
    return;
  }
  const format = raw.found && raw.format === 'json' ? 'json' : 'yaml';
  const content = raw.found ? raw.content : manifestTemplate(model, format);
  const body = document.createElement('div');
  body.className = 'manifest-editor';
  body.innerHTML = `
    <div class="manifest-editor-toolbar">
      <label class="manifest-format">
        <span>${ctx.escapeHtml(t('models.manifestFormat'))}</span>
        <select id="manifest-format" class="select" ${raw.found ? 'disabled' : ''}>
          <option value="yaml" ${format === 'yaml' ? 'selected' : ''}>YAML</option>
          <option value="json" ${format === 'json' ? 'selected' : ''}>JSON</option>
        </select>
      </label>
      <span class="manifest-target text-mono" id="manifest-target">${ctx.escapeHtml(
        raw.name || (format === 'json' ? 'manifest.json' : 'model.yaml')
      )}</span>
      <button type="button" class="btn btn-sm btn-secondary" id="btn-manifest-template">${ctx.escapeHtml(
        t('models.manifestTemplate')
      )}</button>
      <button type="button" class="btn btn-sm btn-secondary" id="btn-manifest-validate">${ctx.escapeHtml(
        t('models.manifestValidate')
      )}</button>
    </div>
    <textarea id="manifest-text" class="input manifest-text" spellcheck="false">${ctx.escapeHtml(
      content
    )}</textarea>
    <div id="manifest-preview"></div>
    <div class="form-help">${ctx.escapeHtml(t('models.manifestNote'))}${
      raw.found ? ` ${ctx.escapeHtml(t('models.manifestExistingFormat'))}` : ''
    }</div>
  `;

  const overlay = ctx.showModal(
    t('models.manifestEditor'),
    body,
    [
      { label: t('app.cancel'), class: 'btn-secondary' },
      ...(raw.found
        ? [
            {
              label: t('models.manifestDelete'),
              class: 'btn-danger',
              keepOpen: true,
              onClick: async () => {
                const confirmed = await askManifest(
                  t('models.manifestDeleteConfirm', { name: raw.name || 'model.yaml' }),
                  t('models.manifestDelete')
                );
                if (!confirmed) return false;
                try {
                  await ctx.Api.deleteModelManifest(model.path, raw.format || format);
                  ctx.showToast(t('models.manifestDeleted'), 'success');
                  ctx.closeModal();
                  await refresh();
                } catch (error) {
                  ctx.showToast(
                    `${t('models.manifestSaveFailed')}: ${localizeBackendError(error.message)}`,
                    'error'
                  );
                }
                return false;
              },
            },
          ]
        : []),
      {
        label: t('app.save'),
        class: 'btn-primary',
        size: 'lg',
        keepOpen: true,
        onClick: async () => {
          const text = overlay.querySelector('#manifest-text').value;
          const fmt = overlay.querySelector('#manifest-format').value;
          try {
            const validation = await ctx.Api.validateModelManifest({
              model: model.path,
              content: text,
              format: fmt,
            });
            overlay.querySelector('#manifest-preview').innerHTML = manifestPreviewHtml(validation);
            if (!validation.found) {
              ctx.showToast(t('models.manifestInvalid'), 'error');
              return false;
            }
            const saved = await ctx.Api.saveModelManifest({
              model: model.path,
              content: text,
              format: fmt,
              overwrite: Boolean(raw.found),
            });
            const warnings = saved.manifest?.warnings || [];
            ctx.showToast(
              warnings.length ? t('models.manifestSavedWarnings') : t('models.manifestSaved'),
              warnings.length ? 'warning' : 'success'
            );
            ctx.closeModal();
            await refresh();
          } catch (error) {
            ctx.showToast(
              `${t('models.manifestSaveFailed')}: ${localizeBackendError(error.message)}`,
              'error'
            );
          }
          return false;
        },
      },
    ]
  );
  overlay.querySelector('.modal').classList.add('manifest-modal');

  const textarea = body.querySelector('#manifest-text');
  const select = body.querySelector('#manifest-format');
  const target = body.querySelector('#manifest-target');
  const preview = body.querySelector('#manifest-preview');
  const validate = async () => {
    const button = body.querySelector('#btn-manifest-validate');
    button.disabled = true;
    try {
      const result = await ctx.Api.validateModelManifest({
        model: model.path,
        content: textarea.value,
        format: select.value,
      });
      preview.innerHTML = manifestPreviewHtml(result);
    } catch (error) {
      preview.innerHTML = `<div class="manifest-preview bad">${ctx.escapeHtml(
        localizeBackendError(error.message)
      )}</div>`;
    } finally {
      button.disabled = false;
    }
  };
  body.querySelector('#btn-manifest-validate').addEventListener('click', validate);
  body.querySelector('#btn-manifest-template').addEventListener('click', () => {
    textarea.value = manifestTemplate(model, select.value);
    preview.innerHTML = '';
  });
  select.addEventListener('change', () => {
    target.textContent = select.value === 'json' ? 'manifest.json' : 'model.yaml';
    preview.innerHTML = '';
  });
  textarea.focus();
  await validate();
}

function companionsHtml(caps) {
  const groups = [
    [t('models.companionMmproj'), caps.companions.mmproj],
    [t('models.companionHead'), caps.companions.assistant_head],
    [t('models.companionDraft'), caps.companions.draft],
  ].filter(([, items]) => items && items.length);

  if (!groups.length) return '';
  return `
    <div class="card">
      <div class="card-header"><span class="card-title">${ctx.escapeHtml(t('models.companions'))}</span></div>
      <div class="table-wrap">
        <table class="table">
          <tbody>
            ${groups
              .map(([label, items]) =>
                items
                  .map(
                    (item, index) =>
                      `<tr><td class="text-muted">${index === 0 ? ctx.escapeHtml(label) : ''}</td><td class="text-mono">${ctx.escapeHtml(
                        item.filename
                      )} <span class="text-muted">${ctx.escapeHtml(item.size_human || '')}</span></td></tr>`
                  )
                  .join('')
              )
              .join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

function specModesHtml(caps) {
  if (!caps.spec_modes || !caps.spec_modes.length) return '';
  const recommended = caps.recommended_spec_type;
  return `
    <div class="card">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(t('models.specModes'))}</span>
        ${
          recommended
            ? `<div class="card-actions"><span class="tag">${ctx.escapeHtml(
                t('models.recommended', { mode: recommended })
              )}</span></div>`
            : ''
        }
      </div>
      <div class="table-wrap">
        <table class="table">
          <tbody>
            ${caps.spec_modes
              .map(
                (mode) => `<tr>
                  <td class="text-mono">${ctx.escapeHtml(mode.spec_type)}</td>
                  <td>${ctx.escapeHtml(
                    mode.available ? t('models.specAvailable') : t('models.specUnavailable')
                  )}</td>
                  <td class="text-sm text-muted">${ctx.escapeHtml(mode.reason)}</td>
                </tr>`
              )
              .join('')}
          </tbody>
        </table>
      </div>
    </div>
  `;
}

// ========== 操作 ==========

function bindActions(el, state) {
  const useBtn = el.querySelector('#btn-use-in-editor');
  if (useBtn) {
    useBtn.addEventListener('click', () => {
      // 暂存目标模型，param_editor 的 onShow 会读取（既有跨页传参约定）
      window.__pendingModelPath = state.model.path;
      ctx.navigate('param_editor');
    });
  }

  const launchBtn = el.querySelector('#btn-launch-model');
  if (launchBtn) {
    launchBtn.addEventListener('click', async () => {
      // 精确能力还在路上时先等它回来 —— 否则会用一份"没有推荐值"的空 caps 启动，
      // 静默丢掉 ctx 与推测解码的推荐
      if (state.pending) {
        launchBtn.disabled = true;
        try {
          await state.pending;
        } finally {
          launchBtn.disabled = false;
        }
      }
      launch(state.model, state.precise, launchBtn);
    });
  }
}

async function launch(model, caps, button) {
  // 推荐 spec 模式来自探测结果；没有就退回"不启用"（空值即不发 --spec-type）
  const mode = (caps && caps.recommended_spec_type) || null;
  const suggestedDraft = (() => {
    if (!caps || !caps.spec_modes) return null;
    const hit = caps.spec_modes.find((m) => m.spec_type === mode && m.suggested_model_draft);
    return hit ? hit.suggested_model_draft : null;
  })();

  const overrides = { model: model.path, port: 0 };
  if (mode) overrides.spec_type = [mode];
  if (suggestedDraft) overrides.model_draft = suggestedDraft;
  // 原生上下文是硬上限，用它作为初始 ctx 比默认值更贴近模型真实能力
  if (caps && caps.context_length) overrides.ctx_size = caps.context_length;
  // 直接启动嵌入/重排模型时补上它需要的端点开关。这不是"猜参数"：
  // is_embedding_model 来自 GGUF 的 causal/pooling 元数据，而缺 --embeddings
  // 的单槽位组合在部分 llama.cpp 版本上会触发上游断言。
  if (caps && caps.is_embedding_model) overrides.embeddings = true;
  if (caps && caps.is_reranker_model) overrides.rerank = true;

  button.disabled = true;
  try {
    const defaults = await ctx.Api.getParamDefaults();
    const body = {
      params: defaults,
      param_overrides: overrides,
      name: instanceNameFromFilename(model.filename),
    };
    if (!(await confirmLaunch(ctx, body))) return;
    const instance = await ctx.Api.createInstance(body);
    ctx.showToast(t('models.launched', { name: instance.name, port: instance.port }), 'success');
  } catch (e) {
    ctx.showToast(`${t('models.launchFailed')}: ${localizeBackendError(e.message)}`, 'error');
  } finally {
    button.disabled = false;
  }
}
