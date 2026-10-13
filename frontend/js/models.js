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
let showAll = readShowAll();

function readShowAll() {
  try {
    return localStorage.getItem(SHOW_ALL_KEY) === '1';
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
        <div class="page-title">${ctx.escapeHtml(t('models.title'))}</div>
        <div class="page-subtitle" id="models-subtitle">${ctx.escapeHtml(t('models.subtitle'))}</div>
      </div>
      <div class="page-actions">
        <button class="btn btn-secondary" id="btn-models-rescan">${ctx.escapeHtml(t('models.rescan'))}</button>
      </div>
    </div>
    <div id="model-detail"></div>
  `;
  root.querySelector('#btn-models-rescan').addEventListener('click', () => refresh());
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
    const data = await ctx.Api.scanModels(undefined, true);
    models = Array.isArray(data.models) ? data.models : [];
    directory = data.directory || '';
    truncated = !!data.capabilities_truncated;

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
    ctx.refreshPanel();
    await renderDetail();
  } catch (e) {
    ctx.showToast(`${t('models.scanFailed')}: ${localizeBackendError(e.message)}`, 'error');
  } finally {
    loading = false;
  }
}

// ========== 二级面板：模型列表 ==========

export function renderPanel(el) {
  const hiddenCount = models.filter((m) => !isLoadable(m)).length;
  el.innerHTML = `
    <div class="panel-search">
      <input type="search" id="model-search" class="input" placeholder="${ctx.escapeHtml(
        t('models.searchPh')
      )}" value="${ctx.escapeHtml(searchKeyword)}" autocomplete="off"/>
    </div>
    ${
      hiddenCount
        ? `<div class="panel-filter">
             <label class="panel-toggle">
               <input type="checkbox" id="model-show-all" ${showAll ? 'checked' : ''}/>
               <span>${ctx.escapeHtml(t('models.showCompanions', { count: hiddenCount }))}</span>
             </label>
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
      renderPanel(el);
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
  const keyword = searchKeyword;
  const base = showAll ? models : models.filter(isLoadable);
  const visible = keyword
    ? base.filter(
        (m) =>
          m.filename.toLowerCase().includes(keyword) ||
          String((m.capabilities && m.capabilities.architecture) || '').toLowerCase().includes(keyword)
      )
    : base;

  renderList(container, ctx, {
    items: visible.map((m) => {
      const caps = m.capabilities || {};
      return {
        key: m.path,
        label: m.filename,
        // 徽标放在元信息行：标签行被长文件名挤占后，徽标在那里会折行
        metaHtml: `${badgeHtml(caps)}<span class="panel-list-meta-text">${ctx.escapeHtml(
          summaryLine(m, caps)
        )}</span>`,
      };
    }),
    activeKey: selectedPath,
    onSelect: (key) => {
      selectedPath = key;
      ctx.refreshPanel();
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
  if (caps.provides_builtin_mtp) {
    return `<span class="tag">${ctx.escapeHtml(t('models.badgeBuiltinMtp'))}</span>`;
  }
  return '';
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
  el.innerHTML = headerCard(model, caps) + `<div id="model-caps-body"><div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(
    t('models.loadingCaps')
  )}</div></div>`;

  el.querySelector('#btn-copy-model-path').addEventListener('click', () => {
    ctx.copyToClipboard(model.path);
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
        if (precise) detailCache.set(model.path, precise);
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
  body.innerHTML = precise ? capabilitiesHtml(precise) : unparsableHtml(caps);
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
      <div class="text-mono text-sm" style="word-break:break-all; color: var(--color-text-tertiary);">${ctx.escapeHtml(
        model.path
      )}</div>
      ${
        canLaunch
          ? `<div class="page-actions" style="margin-top:12px;">
               <button class="btn btn-secondary" id="btn-use-in-editor">${ctx.escapeHtml(
                 t('models.useInEditor')
               )}</button>
               <button class="btn btn-primary" id="btn-launch-model">${ctx.escapeHtml(
                 t('models.launchNow')
               )}</button>
             </div>`
          : `<div class="form-help" style="margin-top:12px;">${ctx.escapeHtml(
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
  if (caps.provides_builtin_mtp) badges.push([t('models.badgeBuiltinMtp'), 'tag']);
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

  button.disabled = true;
  try {
    const defaults = await ctx.Api.getParamDefaults();
    const instance = await ctx.Api.createInstance({
      params: defaults,
      param_overrides: overrides,
      name: model.filename,
    });
    ctx.showToast(t('models.launched', { name: instance.name, port: instance.port }), 'success');
  } catch (e) {
    ctx.showToast(`${t('models.launchFailed')}: ${localizeBackendError(e.message)}`, 'error');
  } finally {
    button.disabled = false;
  }
}
