// 参数编辑器（模态浮层，核心）
// 按 schema 动态渲染参数网格，支持联动、文件浏览、实时命令预览、保存预设、立即启动。
// 为什么是浮层：它不是"常驻地"而是"启动前的一次性配置"，形态上更像 LM Studio 的
// 加载浮层 —— 选模型 → 配参数 → 启动，全程不离开当前页面上下文（kind:'modal'，
// 与 settings 同款契约；showModal 单实例，二级交互用浮层内联展开区而非嵌套弹窗）。

import { tauriInvoke } from './api.js?v=1';
import {
  createRequestGate,
  diffModelDefaults,
  isParamEnabled,
  shouldAutoAssignPort,
  mergeUpstreamOptions,
  parseNumericInput,
} from './runtime.mjs';
import { t, localizeBackendError } from './i18n.mjs';
import { confirmLaunch } from './preflight.mjs';
import { createParamView } from './param_editor_view.mjs';

export const id = 'param_editor';
export const title = '参数编辑器';
export const kind = 'modal';

let ctx = null;
let root = null;          // 浮层 overlay；所有 id 查询（能力条/分组/命令条）都在它之下
let schema = null;
let defaults = {};
let currentValues = {};   // 当前参数值（唯一状态源：切分组重渲染不丢输入）
let schemaMap = {};      // key -> param def
let groups = [];
let presetCache = [];
let recipes = [];         // 用途配方（后端按本机构建标注 unsupported）
// 运行环境选择：'' = 跟随设置里的默认运行时；否则为已注册 profile id。
// schema / 能力 / 配方都按所选构建重新取，不能沿用上一个构建的 supported 标记。
let runtimeProfiles = [];
let runtimePreference = '';
let selectedRuntimeId = '';
let schemaRuntimeId = '';
let runtimesLoaded = false;
let runtimesPromise = null;
// 模型默认值（manifest < 用户保存）及其来源标签
let modelDefaults = null;
let lastDefaultsPath = null;
// Must mirror backend/core/config_resolver.py MANAGED_KEYS. Saving these as
// model defaults would redirect the model or the security boundary, so the UI
// filters them before the API even sees them.
const MANAGED_DEFAULT_KEYS = new Set([
  'model',
  'model_dir',
  'host',
  'port',
  'api_key',
  'path',
  'log_file',
  'extra_args',
]);
let locale = 'zh-CN';
let view = null; // pure HTML renderer, rebuilt on open
// 从模型库/顶栏选择器跳过来的目标模型；schema 未就绪时先存着，加载完成后再应用
let pendingModelPath = null;
// 模型能力（来自 P1 探测层）
let modelCaps = null;        // 当前模型的探测结果；null = 尚未探测或探测失败
let nativeCtxLimit = null;   // 原生上下文上限，用于收紧 ctx_size 的 max
let lastCardPath = null;     // 已渲染卡片对应的路径，避免重复请求/重绘
let _cardTimer = null;
// 本次预览里"本机构建不支持而被丢弃"的参数 key（后端 build-command 回传）
let lastSuppressed = [];
let lastCommand = '';        // 最近一次命令预览全文（内联确认区直接读它，不碰 DOM）
let activeGroupKey = null;   // 当前显示的参数分组（切换式导航，无 IntersectionObserver）
const capsCache = new Map(); // path -> caps，来回切换模型不重复请求 700ms 的 --help

export function open(appCtx) {
  ctx = appCtx;
  locale = document.documentElement.lang === 'en-US' ? 'en-US' : 'zh-CN';
  view = createParamView({
    escapeHtml: appCtx.escapeHtml,
    t,
    locale,
    getValues: () => currentValues,
  });
  // 每次打开都重新读运行环境：用户可能刚在「运行环境」页注册/移除过构建。
  runtimesLoaded = false;
  runtimesPromise = null;
  modelDefaults = null;
  lastDefaultsPath = null;
  // 浮层关闭会销毁卡片 DOM，但模块级 lastCardPath 还在；不置空的话重新打开时
  // refreshModelCard 会直接 return，模型能力卡与"应用模型默认值"按钮整块消失。
  lastCardPath = null;
  // 跨模块传参沿用既有 window.__pending* 约定（原 onShow 里的逻辑平移）
  if (window.__pendingModelPath) {
    pendingModelPath = window.__pendingModelPath;
    window.__pendingModelPath = null;
  }

  const wrap = document.createElement('div');
  wrap.className = 'param-editor-modal';
  wrap.innerHTML = `
    <!-- 能力条：由 refreshModelCard() 按当前 model 填充，只读展示 + 一键应用推荐 -->
    <div class="pe-caps" id="model-caps-card"></div>
    <!-- 用途配方：一键把面板切到某个用途（对话/嵌入/重排/只读工具/Agent） -->
    <div class="pe-recipes" id="pe-recipes" hidden></div>
    <div class="pe-main">
      <nav class="pe-nav panel-nav" id="pe-nav"></nav>
      <div class="pe-pane" id="param-groups"></div>
    </div>
  `;
  // 动作按钮全部收进底部命令条，不用 showModal 的 footer 机制。
  // 初始焦点放在编辑器容器上（内部控件尚未渲染，不能聚焦第一个按钮）。
  wrap.tabIndex = -1;
  const overlay = ctx.showModal(t('params.title'), wrap, [], { initialFocus: wrap });
  overlay.querySelector('.modal').classList.add('modal-xl', 'modal-param-editor');
  root = overlay;

  // 页头动作（加载预设 / 重置默认）挂到浮层标题栏，位于关闭按钮左侧
  overlay
    .querySelector('.modal-close')
    .insertAdjacentHTML(
      'beforebegin',
      `
    <div class="pe-header-actions">
      <select class="select" id="runtime-select" aria-label="${ctx.escapeHtml(t('params.runtimeSelect'))}" title="${ctx.escapeHtml(t('params.runtimeSelectHint'))}">
        <option value="">${ctx.escapeHtml(t('params.runtimeDefault'))}</option>
      </select>
      <select class="select" id="load-preset-select" aria-label="${ctx.escapeHtml(t('params.loadPreset'))}" min-w-preset>
        <option value="">${ctx.escapeHtml(t('params.loadPreset'))}</option>
      </select>
      <button class="btn btn-secondary btn-sm" id="btn-reset-defaults">${ctx.escapeHtml(t('params.resetDefaults'))}</button>
    </div>
  `
    );

  // 底部命令条：一行等宽摘要（点击展开全文）+ 被丢弃参数提示 + 保存预设 / 立即启动
  const footer = overlay.querySelector('.modal-footer');
  footer.classList.add('pe-cmdbar');
  footer.innerHTML = `
    <div class="inline-sheet" id="pe-sheet" hidden></div>
    <div class="pe-cmd-row">
      <button type="button" class="pe-cmd-toggle" id="pe-cmd-toggle" title="${ctx.escapeHtml(t('params.cmdToggle'))}" aria-expanded="false" aria-controls="cmd-preview">
        <span class="pe-cmd-chevron" aria-hidden="true">▸</span>
        <code class="pe-cmd-summary" id="pe-cmd-summary"></code>
      </button>
      <div class="cmd-suppressed" id="cmd-suppressed" hidden></div>
      <div class="pe-cmd-actions">
        <button class="btn btn-secondary btn-sm" id="btn-copy-cmd">${ctx.escapeHtml(t('params.copy'))}</button>
        <button class="btn btn-secondary" id="btn-save-preset">${ctx.escapeHtml(t('params.savePreset'))}</button>
        <button class="btn btn-primary" id="btn-launch-now" title="${ctx.escapeHtml(t('params.launchNowHint'))}">${ctx.escapeHtml(t('params.launchNow'))} <span class="kbd">Ctrl+Enter</span></button>
      </div>
    </div>
    <pre id="cmd-preview" class="pe-cmd-full" hidden></pre>
  `;

  bindEvents();
  refreshRuntimeSelect();
  // 运行环境列表与 schema 并行加载；若设置里的默认运行时变了，缓存 schema 需要重建
  ensureRuntimes().then(() => {
    refreshRuntimeSelect();
    if (schema && selectedRuntimeId !== schemaRuntimeId) {
      switchRuntime(selectedRuntimeId, { silent: true });
    }
  });
  // Ctrl+Enter 绑在浮层上（随浮层销毁自动解绑）；焦点放进浮层，键盘事件才能冒泡上来
  wrap.tabIndex = -1;
  wrap.focus({ preventScroll: true });
  overlay.addEventListener('keydown', onModalKeydown);

  if (schema) {
    activeGroupKey = activeGroupKey && groups.some((g) => g.key === activeGroupKey)
      ? activeGroupKey
      : (groups[0] && groups[0].key);
    renderNav();
    renderGroups();
    updateCommandPreview();
    refreshPresetDropdown();
    loadRecipes();
    applyPendingModel();
  } else {
    loadSchema();
  }
  updateModalTitle();
}

/** 浮层标题：加载模型 · <文件名>（无模型时退回页面名） */
function updateModalTitle() {
  const el = root && root.querySelector('.modal-title');
  if (!el) return;
  const path = String(currentValues.model || '').trim();
  el.textContent = path
    ? `${t('params.modalTitle')} · ${ctx.basename(path)}`
    : t('params.title');
}

const paramLabel = (p) => view.paramLabel(p);
const paramHelp = (p) => view.paramHelp(p);
const paramControlId = (p) => view.paramControlId(p);
const groupLabel = (g) => view.groupLabel(g);

function bindEvents() {
  // 重置确认也走内联 sheet：showConfirm 会顶掉编辑器浮层（showModal 单实例）
  root.querySelector('#btn-reset-defaults').addEventListener('click', () => openInlineSheet('reset'));
  root.querySelector('#runtime-select').addEventListener('change', (event) => {
    switchRuntime(event.target.value);
  });
  root.querySelector('#btn-copy-cmd').addEventListener('click', () => {
    ctx.copyToClipboard(lastCommand);
  });
  root.querySelector('#btn-save-preset').addEventListener('click', () => openInlineSheet('preset'));
  root.querySelector('#btn-launch-now').addEventListener('click', () => openInlineSheet('launch'));
  // 命令条：点击摘要行展开/收起全文
  root.querySelector('#pe-cmd-toggle').addEventListener('click', (event) => {
    const full = root.querySelector('#cmd-preview');
    const chevron = root.querySelector('.pe-cmd-chevron');
    const expanded = !full.hidden;
    full.hidden = expanded;
    chevron.classList.toggle('open', !expanded);
    event.currentTarget.setAttribute('aria-expanded', String(!expanded));
  });
  root.querySelector('#load-preset-select').addEventListener('change', async (e) => {
    const id = e.target.value;
    if (!id) return;
    try {
      const p = await ctx.Api.getPreset(id);
      // 预设可以固定某个运行环境；目标构建不可用时保留当前选择并明确告警。
      if (p.runtime_id && p.runtime_id !== selectedRuntimeId) {
        const target = runtimeProfiles.find(
          (profile) => profile.id === p.runtime_id && profile.status === 'ok'
        );
        if (target) {
          if (!(await switchRuntime(target.id, { silent: true }))) {
            throw new Error(t('params.presetRuntimeUnavailable'));
          }
        } else {
          ctx.showToast(t('params.presetRuntimeMissing', { id: p.runtime_id }), 'warning');
        }
      }
      Object.assign(currentValues, JSON.parse(JSON.stringify(defaults)), p.params);
      refreshAllControls();
      updateCommandPreview();
      ctx.showToast(t('params.presetLoaded', { name: presetDisplayName(p) }), 'success', 1500);
    } catch (err) {
      ctx.showToast(`${t('params.presetLoadFailed')}: ${localizeBackendError(err.message)}`, 'error');
    }
    e.target.value = '';
  });
}

function ensureRuntimes() {
  if (runtimesLoaded) return Promise.resolve();
  if (!runtimesPromise) {
    runtimesPromise = loadRuntimes().finally(() => {
      runtimesPromise = null;
    });
  }
  return runtimesPromise;
}

async function loadRuntimes() {
  try {
    const payload = await ctx.Api.listRuntimes();
    runtimeProfiles = Array.isArray(payload && payload.runtimes) ? payload.runtimes : [];
    runtimePreference =
      payload && payload.preferences ? String(payload.preferences.gguf || '') : '';
  } catch {
    // 运行环境接口失败不能挡住调参：退回"仅用设置里的 llama-server 路径"
    runtimeProfiles = [];
    runtimePreference = '';
  }
  const usable = (id) =>
    Boolean(id) && runtimeProfiles.some((profile) => profile.id === id && profile.status === 'ok');
  if (selectedRuntimeId && !usable(selectedRuntimeId)) selectedRuntimeId = '';
  if (!selectedRuntimeId && usable(runtimePreference)) selectedRuntimeId = runtimePreference;
  runtimesLoaded = true;
  refreshRuntimeSelect();
}

function refreshRuntimeSelect() {
  const select = root && root.querySelector('#runtime-select');
  if (!select) return;
  const options = [`<option value="">${ctx.escapeHtml(t('params.runtimeDefault'))}</option>`];
  for (const profile of runtimeProfiles) {
    const name = profile.display_name || profile.id;
    const suffix = profile.status === 'ok' ? '' : ` (${t('runtimes.probeFailed')})`;
    options.push(
      `<option value="${ctx.escapeHtml(String(profile.id))}"${profile.status === 'ok' ? '' : ' disabled'}>${ctx.escapeHtml(
        name + suffix
      )}</option>`
    );
  }
  select.innerHTML = options.join('');
  select.value = selectedRuntimeId;
}

/**
 * 切换运行环境：schema 的 supported/upstream_options 是**按构建**的，必须整体
 * 重新取；再按新 schema 过滤用户当前值，避免把上一个构建的私有字段带进命令行。
 */
async function switchRuntime(nextId, { silent = false } = {}) {
  const target = String(nextId || '');
  if (schema && target === selectedRuntimeId) {
    refreshRuntimeSelect();
    return true;
  }
  const previousId = selectedRuntimeId;
  selectedRuntimeId = target;
  refreshRuntimeSelect();
  if (!schema) return true; // 首次加载：loadSchema 会带上这个选择

  const preserved = { ...currentValues };
  capsCache.clear();
  lastCardPath = null;
  modelCaps = null;
  nativeCtxLimit = null;
  modelDefaults = null;
  lastDefaultsPath = null;
  try {
    const nextSchema = await ctx.Api.getParamSchema(target || undefined);
    if (target !== selectedRuntimeId) return true; // 已被更晚的一次切换取代
    schema = nextSchema;
    schemaRuntimeId = target;
    groups = schema.groups || [];
    schemaMap = {};
    for (const p of schema.params || []) schemaMap[p.key] = p;
    const merged = JSON.parse(JSON.stringify(defaults));
    for (const key of Object.keys(schemaMap)) {
      if (Object.prototype.hasOwnProperty.call(preserved, key)) merged[key] = preserved[key];
    }
    currentValues = merged;
    if (activeGroupKey && !groups.some((g) => g.key === activeGroupKey)) {
      activeGroupKey = (groups[0] && groups[0].key) || null;
    }
    recipes = [];
    renderNav();
    renderGroups();
    await loadRecipes();
    updateCommandPreview();
    refreshModelCard();
    if (!silent) {
      const profile = runtimeProfiles.find((item) => item.id === target);
      ctx.showToast(
        t('params.runtimeSwitched', {
          name: profile ? profile.display_name || profile.id : t('params.runtimeDefault'),
        }),
        'success',
        1800
      );
    }
    return true;
  } catch (e) {
    if (target !== selectedRuntimeId) return true;
    selectedRuntimeId = previousId;
    refreshRuntimeSelect();
    if (!silent) {
      ctx.showToast(
        `${t('params.runtimeSwitchFailed')}: ${localizeBackendError(e.message)}`,
        'error'
      );
    }
    return false;
  }
}

async function loadSchema() {
  try {
    await ensureRuntimes();
    const requested = selectedRuntimeId;
    const loadedSchema = await ctx.Api.getParamSchema(requested || undefined);
    if (requested !== selectedRuntimeId) return; // 等待期间用户又切换了运行环境
    schema = loadedSchema;
    schemaRuntimeId = requested;
    const d = await ctx.Api.getParamDefaults();
    defaults = d || {};
    groups = schema.groups || [];
    schemaMap = {};
    for (const p of schema.params || []) schemaMap[p.key] = p;
    // 初始化当前值为默认
    currentValues = JSON.parse(JSON.stringify(defaults));
    activeGroupKey = (groups[0] && groups[0].key) || null;
    renderNav();
    renderGroups();
    updateCommandPreview();
    refreshPresetDropdown();
    loadRecipes();
    // 从模型库/选择器跳过来的目标模型也等 schema 就绪后再应用
    applyPendingModel();
  } catch (e) {
    ctx.showToast(`${t('params.schemaFailed')}: ${localizeBackendError(e.message)}`, 'error');
    root.querySelector('#param-groups').innerHTML = `<div class="empty-state">${ctx.escapeHtml(t('params.loadFailed'))}: ${ctx.escapeHtml(localizeBackendError(e.message))}</div>`;
  }
}

function presetDisplayName(p) {
  return (locale === 'en-US' && p.name_en) ? p.name_en : p.name;
}

async function refreshPresetDropdown() {
  try {
    presetCache = await ctx.Api.listPresets();
  } catch { presetCache = []; }
  const sel = root.querySelector('#load-preset-select');
  sel.innerHTML = `<option value="">${ctx.escapeHtml(t('params.loadPreset'))}</option>` +
    presetCache.map((p) => `<option value="${ctx.escapeHtml(String(p.id))}">${ctx.escapeHtml(presetDisplayName(p))}</option>`).join('');
}

/** 左侧分组导航（切换式）：点击即整体重渲染该分组，无 IntersectionObserver。
    currentValues 是唯一状态源，每个输入事件都即时写回，切分组不丢输入。 */
function renderNav() {
  const nav = root.querySelector('#pe-nav');
  if (!nav) return;
  const counts = {};
  for (const p of schema.params || []) counts[p.group] = (counts[p.group] || 0) + 1;
  nav.innerHTML = groups
    .map(
      (g) => `
      <button type="button" class="panel-nav-item${g.key === activeGroupKey ? ' active' : ''}" data-goto-group="${ctx.escapeHtml(g.key)}" title="${ctx.escapeHtml(groupLabel(g))}">
        <span class="panel-nav-label">${ctx.escapeHtml(groupLabel(g))}</span>
        <span class="panel-nav-count">${counts[g.key] || 0}</span>
      </button>`
    )
    .join('');
  nav.querySelectorAll('[data-goto-group]').forEach((btn) => {
    btn.addEventListener('click', () => switchGroup(btn.dataset.gotoGroup));
  });
}

function switchGroup(key) {
  if (key === activeGroupKey) return;
  activeGroupKey = key;
  renderNav();
  renderGroups();
}

function renderGroups() {
  const wrap = root.querySelector('#param-groups');
  // 切换式：内容区一次只渲染当前分组（currentValues 保状态，重渲染无损耗）
  const visible = groups.filter((g) => g.key === activeGroupKey);
  wrap.innerHTML = visible.map((g) => groupHtml(g)).join('');
  // 渲染该组的参数
  for (const g of visible) {
    const body = wrap.querySelector(`[data-group-body="${g.key}"]`);
    const params = (schema.params || []).filter((p) => p.group === g.key).sort((a, b) => (a.order || 0) - (b.order || 0));
    body.innerHTML = params.map((p) => paramHtml(p)).join('');
  }
  // 绑定事件
  wrap.querySelectorAll('[data-param]').forEach((el) => {
    bindParamControl(el);
  });
  // 折叠面板（浮层内单分组始终展开，头部的开合保留为可用交互）
  wrap.querySelectorAll('.collapse-header').forEach((h) => {
    h.addEventListener('click', () => h.parentElement.classList.toggle('open'));
  });
  refreshAllControls();
}

const groupHtml = (g) => view.groupHtml(g);
const paramHtml = (p) => view.paramHtml(p);

/** 同步卡片的选中样式与"已选 N 项"计数（不依赖 CSS :has，兼容旧 WebView2） */
function syncOptionCards(container) {
  const boxes = Array.from(container.querySelectorAll('input[data-opt]'));
  let count = 0;
  for (const box of boxes) {
    const selected = box.checked;
    const card = box.closest('.option-card');
    if (card) card.classList.toggle('selected', selected);
    if (selected) count += 1;
  }
  const counter = container.querySelector('[data-count]');
  if (counter) {
    counter.textContent = count
      ? t('params.multiSelected', { count })
      : t('params.multiNone');
  }
}

function bindParamControl(fieldEl) {
  const key = fieldEl.dataset.key;
  const p = schemaMap[key];
  if (!p) return;

  // 卡片式多选：容器上监听 change（子复选框的事件会冒泡上来）
  if (p.multiple) {
    const box = fieldEl.querySelector(`.option-cards[data-key="${key}"]`);
    if (!box) return;
    const sync = () => {
      currentValues[key] = Array.from(box.querySelectorAll('input[data-opt]'))
        .filter((el) => el.checked)
        .map((el) => el.dataset.opt);
      syncOptionCards(box);
      refreshAllControls();         // 联动重算
      scheduleCommandPreview();     // 命令预览刷新（防抖）
    };
    box.addEventListener('change', sync);
    const clearBtn = box.querySelector('[data-clear]');
    if (clearBtn) {
      clearBtn.addEventListener('click', (event) => {
        event.preventDefault();
        box.querySelectorAll('input[data-opt]').forEach((el) => { el.checked = false; });
        sync();
      });
    }
    return;
  }

  const input = fieldEl.querySelector(`[data-key="${key}"]`);
  if (!input) return;
  const handler = () => {
    if (p.type === 'number' || p.type === 'float') {
      const parsed = parseNumericInput(input.value, p.type);
      // 输入中间态（"-"、"0."）不能回写输入框：等到 blur/change 再规范化。
      // 无效中间态保留上一个有效值，命令预览不会因为一个 "-" 跳成空。
      if (parsed.valid) currentValues[key] = parsed.value;
      refreshAllControls({ skipKey: key });
      scheduleCommandPreview();
      return;
    }
    const val = p.type === 'switch' ? input.checked : input.value;
    currentValues[key] = val;
    refreshAllControls({ skipKey: key }); // 联动重算，但不回写正在编辑的控件
    scheduleCommandPreview();             // 命令预览刷新（防抖）
  };
  input.addEventListener('input', handler);
  input.addEventListener('change', handler);
  input.addEventListener('blur', () => {
    if (p.type === 'number' || p.type === 'float') {
      const parsed = parseNumericInput(input.value, p.type);
      currentValues[key] = parsed.valid ? parsed.value : null;
      if (!parsed.valid) input.value = ''; // 非数字文本不留在输入框里
    }
    refreshAllControls(); // 失焦时统一规范化并同步联动
    scheduleCommandPreview();
  });
  // 浏览按钮：使用原生文件选择对话框
  const browseBtn = fieldEl.querySelector(`[data-browse="${key}"]`);
  if (browseBtn) {
    browseBtn.addEventListener('click', () => openNativeFileDialog(p, browseBtn.dataset.browseType));
  }
  // ? 提示圈聚焦时 ESC 只收起 tooltip（stopPropagation 拦住全局 ESC，不连带关浮层/切页）
  const tip = fieldEl.querySelector('.param-help-tip');
  if (tip) {
    tip.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') {
        e.stopPropagation();
        tip.blur();
      }
    });
  }
}

function refreshAllControls({ skipKey = null } = {}) {
  for (const key in schemaMap) {
    const p = schemaMap[key];
    const field = root.querySelector(`.param-field[data-key="${key}"]`);
    if (!field) continue;
    const enabled = isParamEnabled(p, currentValues);
    field.classList.toggle('disabled', !enabled);
    // 把 currentValues 回填到控件（用于预设加载/重置默认/浏览选择后同步显示）。
    // 正在编辑的控件（skipKey）不回写：数字输入的 "-" / "0." 中间态必须原样保留。
    const input = field.querySelector(`[data-key="${key}"]`);
    if (!input) continue;
    const v = currentValues[key];
    if (p.type === 'switch') {
      if (key !== skipKey) input.checked = !!v;
    } else if (p.multiple) {
      if (key !== skipKey) {
        const selected = Array.isArray(v) ? v.map(String) : [];
        input.querySelectorAll('input[data-opt]').forEach((box) => {
          box.checked = selected.includes(box.dataset.opt);
        });
      }
      syncOptionCards(input);
    } else if (key !== skipKey) {
      input.value = (v === null || v === undefined) ? '' : String(v);
    }
  }
  // 能力卡片与 ctx 上限也跟着值变。挂在这里是因为 refreshAllControls 是
  // "值变化后的统一出口"：载入预设、重置默认、浏览选择、字段编辑全汇聚到它，
  // 卡片只要跟住这一处就不会漏。探测本身防抖，见 scheduleModelCardRefresh。
  scheduleModelCardRefresh();
  applyCtxLimit();
}

// ========== 模型能力卡片 ==========
// 数据来自 P1 的能力探测层（/api/system/capabilities?model=…）：
// 它读 GGUF 头拿到**模型原生上下文上限**、是否自带 MTP 头、同目录配套文件，
// 这些是 schema 里给不出的（schema 的 ctx_size max=1048576 只是个通用天花板）。

function scheduleModelCardRefresh() {
  // 模型路径是文本框，逐字符输入都会走到 refreshAllControls；ctx_size / ngl /
  // 缓存类型 / 并行数也都会改变显存估算。不防抖就变成每敲一个字发两次 HTTP。
  if (_cardTimer) clearTimeout(_cardTimer);
  _cardTimer = setTimeout(() => {
    updateModalTitle(); // 标题里的模型文件名跟着 model 字段走
    refreshModelCard();
    refreshVramEstimate();
  }, 350);
}

async function refreshModelCard() {
  const slot = root && root.querySelector('#model-caps-card');
  if (!slot || !schema) return;
  const path = String(currentValues.model || '').trim();
  if (path === lastCardPath) return;

  modelCaps = null;
  nativeCtxLimit = null;
  if (!path) {
    lastCardPath = null;
    slot.innerHTML = '';
    applyCtxLimit();
    return;
  }

  // 能力是「构建 × 模型」的组合：换运行时必须重新探测，缓存键要带 runtime id。
  const cacheKey = `${selectedRuntimeId}\u0000${path}`;
  refreshModelDefaults(path);
  const cached = capsCache.get(cacheKey);
  if (cached) {
    lastCardPath = path;
    modelCaps = cached;
    nativeCtxLimit = cached.context_length || null;
    paintModelCard(slot, cached);
    refreshVramEstimate();
    applyCtxLimit();
    return;
  }

  slot.innerHTML = `<div class="card model-caps-placeholder"><span class="loading"></span> ${ctx.escapeHtml(
    t('params.capsLoading')
  )}</div>`;
  let caps = null;
  try {
    const payload = await ctx.Api.getCapabilities(path, selectedRuntimeId || undefined);
    caps = payload && payload.model;
  } catch {
    caps = null;
  }
  // 等待期间路径或运行环境可能已经改了，此时结果作废
  if (String(currentValues.model || '').trim() !== path) return;
  if (`${selectedRuntimeId}\u0000${path}` !== cacheKey) return;

  if (caps && caps.probed) {
    lastCardPath = path;
    // 上限防止长会话无界增长
    if (capsCache.size >= 64) capsCache.clear();
    capsCache.set(cacheKey, caps);
    modelCaps = caps;
    nativeCtxLimit = caps.context_length || null;
    paintModelCard(slot, caps);
    refreshVramEstimate();
  } else {
    // 探测失败不能挡住调参。lastCardPath 留在 null，这样后续任意一次改动
    // （比如后端刚起来）都会自动重试，不用用户重新选一遍模型。
    slot.innerHTML = `<div class="card model-caps-placeholder">${ctx.escapeHtml(t('params.capsFailed'))}</div>`;
    refreshVramEstimate();
  }
  applyCtxLimit();
}

/**
 * 显存预估：按**当前参数值**算，所以挂在 refreshAllControls 的统一出口上。
 *
 * 它是估算而不是承诺（llama.cpp 的真实占用还含随 batch 变化的 compute buffer、
 * 驱动预留），所以文案里必须带"估算"二字 —— 它更适合看**变化量**：把 ctx 从
 * 8K 调到 16K 会多占多少。
 */
function vramSignature() {
  return JSON.stringify([
    String(currentValues.model || '').trim(),
    currentValues.ctx_size,
    currentValues.n_gpu_layers,
    currentValues.cache_type_k,
    currentValues.cache_type_v,
    currentValues.parallel,
    currentValues.mmproj,
  ]);
}

async function refreshVramEstimate() {
  const slot = root && root.querySelector('#vram-estimate');
  if (!slot) return;
  const path = String(currentValues.model || '').trim();
  if (!path || !modelCaps) {
    vramEstimateGate.cancel();
    slot.innerHTML = '';
    return;
  }
  const request = vramEstimateGate.begin();
  const signature = vramSignature();

  let payload = null;
  try {
    payload = await ctx.Api.estimateVram({
      model: path,
      ctx_size: currentValues.ctx_size,
      n_gpu_layers: currentValues.n_gpu_layers,
      cache_type_k: currentValues.cache_type_k,
      cache_type_v: currentValues.cache_type_v,
      parallel: currentValues.parallel,
      mmproj: currentValues.mmproj,
    });
  } catch {
    payload = null;
  }
  // 等待期间参数可能又变了；旧请求不得覆盖新参数的估算。
  if (!vramEstimateGate.isCurrent(request) || signature !== vramSignature()) return;
  if (!slot.isConnected) return;

  if (!payload || !payload.probed) {
    slot.innerHTML = '';
    return;
  }

  // 这些数字来自后端 JSON，虽然当前都是 round() 后的数值，但它们直接进
  // innerHTML —— 与全仓库其余 HTML sink 一样过一道转义，别留例外。
  const num = (v) => ctx.escapeHtml(String(v ?? ''));
  const parts = [
    `${t('params.vramGpu')} <b>${num(payload.gpu_gib)} GB</b>`,
    `${t('params.vramTotal')} ${num(payload.total_gib)} GB`,
    `${t('params.vramBreakdown', {
      weights: num(payload.weights_gib),
      kv: num(payload.kv_cache_gib),
    })}`,
  ];
  if (payload.cpu_gib > 0.01) {
    parts.push(`${t('params.vramCpu')} ${num(payload.cpu_gib)} GB`);
  }
  // 估算值与本机显卡容量的对照结论。后端只给分级、不给阻断，这里也只告警：
  // 估算精度只到数量级，硬拦会误伤本来能跑的配置。
  // `over` 是最值得喊的一种 —— 它意味着物理上装不下，而现象偏偏是
  // "加载成功、就绪通过、第一次发言才崩"，用户根本想不到是显存。
  const fit = payload.fit || {};
  let fitNote = '';
  if (fit.level === 'over') {
    fitNote = `<span class="danger">${ctx.escapeHtml(t('params.vramFitOver', {
      need: num(payload.gpu_gib), total: num(fit.gpu_total_gib),
    }))}</span>`;
  } else if (fit.level === 'tight') {
    fitNote = `<span class="warn">${ctx.escapeHtml(t('params.vramFitTight', {
      need: num(payload.gpu_gib), free: num(fit.gpu_free_gib),
    }))}</span>`;
  }
  if (fit.gpu_total_gib != null) {
    parts.push(`${t('params.vramGpuCap')} ${num(fit.gpu_total_gib)} GB`);
  }
  // 估算的三种可信度要分开说：缺 KV 头数是"偏保守"，算不出 KV 才是"只有权重"
  let partial = '';
  if (payload.kv_source === 'unavailable') {
    partial = `<span class="warn">${ctx.escapeHtml(t('params.vramPartialWeights'))}</span>`;
  } else if (payload.kv_source === 'derived') {
    partial = `<span class="warn">${ctx.escapeHtml(t('params.vramPartialDerived'))}</span>`;
  }
  slot.innerHTML = `<span class="model-caps-vram-label">${ctx.escapeHtml(t('params.vramLabel'))}</span>
    <span class="model-caps-vram-value">${parts.join(' · ')}</span>${partial}${fitNote}
    <span class="model-caps-vram-note">${ctx.escapeHtml(t('params.vramNote'))}</span>`;
}

function paintModelCard(slot, caps) {
  const facts = [
    [t('models.arch'), caps.architecture],
    [t('models.quant'), caps.file_type_name],
    [t('models.ctx'), caps.context_length],
    [t('models.layers'), caps.block_count],
  ].filter(([, v]) => v !== null && v !== undefined && v !== '');

  // 徽标文案复用模型库的键：同一个概念不该有两份翻译
  const badges = [];
  if (caps.provides_builtin_mtp) badges.push([t('models.badgeBuiltinMtp'), 'tag']);
  if (caps.has_vision_encoder) badges.push([t('models.badgeVision'), 'tag']);
  if (caps.is_assistant_model) badges.push([t('models.badgeAssistant'), 'tag tag-default']);
  if (caps.is_mmproj) badges.push([t('models.badgeMmproj'), 'tag tag-default']);

  const comps = [];
  const compKeys = [
    ['mmproj', 'models.companionMmproj'],
    ['assistant_head', 'models.companionHead'],
    ['draft', 'models.companionDraft'],
  ];
  for (const [key, labelKey] of compKeys) {
    for (const item of (caps.companions && caps.companions[key]) || []) {
      comps.push(`${t(labelKey)}：${item.filename}`);
    }
  }

  slot.innerHTML = `
    <div class="card model-caps-card-body">
      <div class="card-header">
        <span class="card-title">${ctx.escapeHtml(t('params.capsTitle'))}</span>
        <div class="card-actions">
          <button class="btn btn-sm btn-secondary" id="btn-apply-model-defaults" disabled>${ctx.escapeHtml(
            t('params.modelDefaultsApply')
          )}</button>
          <button class="btn btn-sm btn-secondary" id="btn-apply-recommended">${ctx.escapeHtml(
            t('params.capsApply')
          )}</button>
        </div>
      </div>
      <div class="model-caps-defaults" id="model-defaults-note" hidden></div>
      <div class="model-caps-defaults-actions" id="model-defaults-actions" hidden>
        <button type="button" class="btn btn-sm btn-secondary" id="btn-save-model-defaults">${ctx.escapeHtml(
          t('params.modelDefaultsSave')
        )}</button>
        <button type="button" class="btn btn-sm btn-ghost" id="btn-clear-model-defaults" disabled>${ctx.escapeHtml(
          t('params.modelDefaultsClear')
        )}</button>
      </div>
      ${
        badges.length
          ? `<div class="mb-2">${badges
              .map(([label, cls]) => `<span class="${cls}">${ctx.escapeHtml(label)}</span>`)
              .join('')}</div>`
          : ''
      }
      <div class="model-caps-facts">
        ${facts
          .map(
            ([label, value]) =>
              `<span><em>${ctx.escapeHtml(label)}</em>${ctx.escapeHtml(String(value))}</span>`
          )
          .join('')}
      </div>
      ${comps.length ? `<div class="model-caps-comps">${comps.map((c) => `<div>${ctx.escapeHtml(c)}</div>`).join('')}</div>` : ''}
      <div class="model-caps-vram" id="vram-estimate"></div>
      ${
        caps.recommended_spec_type
          ? `<div class="model-caps-rec">${ctx.escapeHtml(
              t('params.capsRecommended', { mode: caps.recommended_spec_type })
            )}</div>`
          : ''
      }
    </div>
  `;
  const btn = slot.querySelector('#btn-apply-recommended');
  if (btn) btn.addEventListener('click', applyRecommended);
  const defaultsBtn = slot.querySelector('#btn-apply-model-defaults');
  if (defaultsBtn) defaultsBtn.addEventListener('click', applyModelDefaults);
  const saveDefaultsBtn = slot.querySelector('#btn-save-model-defaults');
  if (saveDefaultsBtn) saveDefaultsBtn.addEventListener('click', saveModelDefaults);
  const clearDefaultsBtn = slot.querySelector('#btn-clear-model-defaults');
  if (clearDefaultsBtn) clearDefaultsBtn.addEventListener('click', clearModelDefaults);
  updateModelDefaultsUI();
}

/** 读取该模型的默认值（manifest 与用户保存），只用于展示与一键应用。 */
async function refreshModelDefaults(path) {
  if (path === lastDefaultsPath) return;
  lastDefaultsPath = path;
  modelDefaults = null;
  updateModelDefaultsUI();
  let payload = null;
  try {
    payload = await ctx.Api.getModelDefaults(path);
  } catch {
    // 默认值读取失败不影响调参；保持按钮不可用即可
    payload = null;
  }
  if (!root || String(currentValues.model || '').trim() !== path) return;
  modelDefaults = payload;
  updateModelDefaultsUI();
}

function modelDefaultsCounts() {
  const sources = (modelDefaults && modelDefaults.sources) || {};
  let manifest = 0;
  let user = 0;
  for (const source of Object.values(sources)) {
    if (source === 'manifest') manifest += 1;
    else if (source === 'user') user += 1;
  }
  return { manifest, user, total: manifest + user };
}

function updateModelDefaultsUI() {
  const btn = root && root.querySelector('#btn-apply-model-defaults');
  const note = root && root.querySelector('#model-defaults-note');
  if (!btn || !note) return;
  const { manifest, user, total } = modelDefaultsCounts();
  btn.disabled = total === 0;
  const path = String(currentValues.model || '').trim();
  const actions = root.querySelector('#model-defaults-actions');
  const saveBtn = root.querySelector('#btn-save-model-defaults');
  const clearBtn = root.querySelector('#btn-clear-model-defaults');
  const canEdit = Boolean(path && modelDefaults);
  if (actions) actions.hidden = !canEdit;
  if (saveBtn) saveBtn.disabled = !canEdit;
  if (clearBtn) clearBtn.disabled = !canEdit || user === 0;
  if (!total) {
    note.hidden = true;
    note.textContent = '';
    btn.title = '';
    return;
  }
  note.hidden = false;
  const entries = Object.entries((modelDefaults && modelDefaults.sources) || {})
    .filter(([, source]) => source !== 'global')
    .map(([key, source]) => ({
      label: schemaMap[key] ? paramLabel(schemaMap[key]) : key,
      source: source === 'user' ? t('params.sourceUser') : t('params.sourceManifest'),
    }));
  const shown = entries
    .slice(0, 6)
    .map((entry) => `${entry.label}(${entry.source})`)
    .join(' · ');
  const more = entries.length > 6 ? ` +${entries.length - 6}` : '';
  note.textContent = `${t('params.modelDefaultsNote', { manifest, user })} · ${shown}${more}`;
  btn.title = t('params.modelDefaultsApplyHint', {
    list: entries.map((entry) => entry.label).join(', '),
  });
}

/** 应用模型默认值：只覆盖 manifest/用户显式设置的键，其余保持当前值。 */
function applyModelDefaults() {
  if (!modelDefaults) return;
  const applied = [];
  for (const [key, source] of Object.entries(modelDefaults.sources || {})) {
    if (source === 'global') continue;
    const def = schemaMap[key];
    if (!def) continue;
    currentValues[key] = modelDefaults.params[key];
    applied.push(paramLabel(def));
  }
  if (!applied.length) {
    ctx.showToast(t('params.modelDefaultsNothing'), 'info');
    return;
  }
  refreshAllControls();
  updateCommandPreview();
  const sep = locale === 'en-US' ? ', ' : '、';
  ctx.showToast(t('params.modelDefaultsApplied', { list: applied.join(sep) }), 'success');
}

/**
 * 保存“当前参数中与全局默认不同”的键为用户模型默认值。
 *
 * 不保存整份默认字典：那会把 schema 的每个默认值都变成用户覆盖，未来
 * 上游默认值调整会被无声挡住。空差异视为“没有用户覆盖”，走清除接口。
 */
async function saveModelDefaults() {
  const path = String(currentValues.model || '').trim();
  if (!path || !modelDefaults) return;
  const button = root && root.querySelector('#btn-save-model-defaults');
  if (button && button.disabled) return;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  }
  const current = collectParams();
  const params = diffModelDefaults(current, defaults, MANAGED_DEFAULT_KEYS);
  try {
    const payload =
      Object.keys(params).length > 0
        ? await ctx.Api.saveModelDefaults(path, params)
        : await ctx.Api.clearModelDefaults(path);
    modelDefaults = payload;
    lastDefaultsPath = path;
    updateModelDefaultsUI();
    const count = Object.keys(params).length;
    ctx.showToast(
      count > 0
        ? t('params.modelDefaultsSaved', { count })
        : t('params.modelDefaultsCleared'),
      'success'
    );
  } catch (e) {
    ctx.showToast(
      `${t('params.modelDefaultsSaveFailed')}: ${localizeBackendError(e.message)}`,
      'error'
    );
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
  }
}

async function clearModelDefaults() {
  const path = String(currentValues.model || '').trim();
  if (!path || !modelDefaults) return;
  const button = root && root.querySelector('#btn-clear-model-defaults');
  if (button && button.disabled) return;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  }
  try {
    modelDefaults = await ctx.Api.clearModelDefaults(path);
    lastDefaultsPath = path;
    updateModelDefaultsUI();
    ctx.showToast(t('params.modelDefaultsCleared'), 'success');
  } catch (e) {
    ctx.showToast(
      `${t('params.modelDefaultsSaveFailed')}: ${localizeBackendError(e.message)}`,
      'error'
    );
  } finally {
    if (button && button.isConnected) {
      button.disabled = false;
      button.removeAttribute('aria-busy');
    }
    updateModelDefaultsUI();
  }
}

/** 一键应用探测出来的推荐值。只动这三项，其余参数保持用户的选择。 */
function applyRecommended() {
  const caps = modelCaps;
  if (!caps) return;
  const applied = [];

  // 上下文：用模型原生上限，并夹到 schema 允许的范围内
  const ctxDef = schemaMap.ctx_size;
  if (caps.context_length && ctxDef) {
    let v = caps.context_length;
    if (ctxDef.min != null) v = Math.max(v, ctxDef.min);
    if (ctxDef.max != null) v = Math.min(v, ctxDef.max);
    currentValues.ctx_size = v;
    applied.push(t('params.ctxLabel', { n: v }));
  }

  // 推测解码：推荐模式；若该模式需要独立草稿文件，一并填入探测到的路径
  const rec = caps.recommended_spec_type;
  if (rec && schemaMap.spec_type) {
    currentValues.spec_type = [rec];
    applied.push(t('params.specLabel', { mode: rec }));
    const hit = (caps.spec_modes || []).find(
      (m) => m.spec_type === rec && m.suggested_model_draft
    );
    if (hit && schemaMap.model_draft) {
      currentValues.model_draft = hit.suggested_model_draft;
      applied.push(t('params.draftLabel', { name: ctx.basename(hit.suggested_model_draft) }));
    }
  }

  // mmproj：后端按 general.name 配好对才会给（对不上就是 null），所以这里
  // 只在用户还没选的时候动手 —— 自动填错会让 llama-server 启动失败
  if (caps.suggested_mmproj && schemaMap.mmproj && !String(currentValues.mmproj || '').trim()) {
    currentValues.mmproj = caps.suggested_mmproj;
    applied.push(t('params.mmprojLabel', { name: ctx.basename(caps.suggested_mmproj) }));
  }

  if (!applied.length) {
    ctx.showToast(t('params.capsNothing'), 'info');
    return;
  }
  refreshAllControls();
  updateCommandPreview();
  ctx.showToast(t('params.capsApplied', { list: applied.join('、') }), 'success');
}

/**
 * 用途配方：一键把面板切到某个用途。
 *
 * 与"应用推荐"的分工：推荐是**按模型**给出调优值（ctx / 推测解码 / mmproj），
 * 配方是**按用途**给出意图值（这是嵌入服务、那是 Agent 服务），不碰任何调优
 * 数字。配方内容来自后端（`/api/params/recipes`），并按本机构建标注了
 * `unsupported` —— 界面置灰与命令行丢弃用的是同一条判定。
 */
async function loadRecipes() {
  if (recipes.length) {
    renderRecipes();
    return;
  }
  try {
    const payload = await ctx.Api.getParamRecipes(selectedRuntimeId || undefined);
    recipes = (payload && payload.recipes) || [];
  } catch {
    // 配方是便利功能，拉不到就不显示这一行，绝不挡住调参
    recipes = [];
  }
  renderRecipes();
}

function renderRecipes() {
  const slot = root && root.querySelector('#pe-recipes');
  if (!slot) return;
  if (!recipes.length) {
    slot.hidden = true;
    slot.innerHTML = '';
    return;
  }
  const buttons = recipes
    .map((r) => {
      const label = locale === 'en-US' && r.label_en ? r.label_en : r.label;
      const hint = locale === 'en-US' && r.hint_en ? r.hint_en : r.hint;
      const missing = (Array.isArray(r.unsupported) ? r.unsupported : []).map(
        (key) => (schemaMap[key] ? paramLabel(schemaMap[key]) : key)
      );
      const title = missing.length
        ? `${hint}\n${t('params.recipeUnsupported', { list: missing.join(', ') })}`
        : hint;
      return `<button type="button" class="pe-recipe" data-recipe="${ctx.escapeHtml(
        r.id
      )}" title="${ctx.escapeHtml(title)}"${missing.length ? ' disabled' : ''}>${ctx.escapeHtml(label)}</button>`;
    })
    .join('');
  slot.hidden = false;
  slot.innerHTML = `
    <span class="pe-recipes-label" title="${ctx.escapeHtml(t('params.recipesHint'))}">${ctx.escapeHtml(t('params.recipesLabel'))}</span>
    <div class="pe-recipes-list">${buttons}</div>
  `;
  slot.querySelectorAll('[data-recipe]').forEach((btn) => {
    btn.addEventListener('click', () => {
      const recipe = recipes.find((r) => r.id === btn.dataset.recipe);
      if (recipe) applyRecipe(recipe);
    });
  });
}

function applyRecipe(recipe) {
  if (!schema) return;
  const applied = [];
  let firstGroup = null;
  for (const [key, value] of Object.entries(recipe.values || {})) {
    const def = schemaMap[key];
    // 后端已经过滤过未知键，这里再挡一道：界面渲染不出来的键不许写进 params，
    // 否则会出现"值改了、面板上没有对应控件"的隐形状态。
    if (!def) continue;
    currentValues[key] = value;
    applied.push(paramLabel(def));
    if (!firstGroup) firstGroup = def.group;
  }
  const name = locale === 'en-US' && recipe.label_en ? recipe.label_en : recipe.label;
  if (!applied.length) {
    ctx.showToast(t('params.recipeNothing', { name }), 'info');
    return;
  }
  refreshAllControls();
  updateCommandPreview();
  // 跳到第一个被改动的分组：点了按钮必须**看得见**发生了变化
  if (firstGroup) switchGroup(firstGroup);
  const sep = locale === 'en-US' ? ', ' : '、';
  ctx.showToast(t('params.recipeApplied', { name, list: applied.join(sep) }), 'success');
}

/**
 * 把 ctx_size 的上限收紧到模型原生上限，并在控件下方给出内联提示。
 * 原生上限是"状态"不是"说明"：只在接近（≥90%）或超过时出现在控件下方，
 * 不进 ? 提示圈 —— 静态说明归 tooltip，动态状态保持内联。
 */
function applyCtxLimit() {
  const field = root && root.querySelector('.param-field[data-key="ctx_size"]');
  if (!field) return;
  const input = field.querySelector('input[data-key="ctx_size"]');
  const schemaMax = schemaMap.ctx_size ? schemaMap.ctx_size.max : null;
  if (input) {
    // 无原生上限时回落到 schema 的通用天花板，不要留一个空 max
    input.max = String(nativeCtxLimit || schemaMax || '');
  }

  let note = field.querySelector('.native-ctx-note');
  if (!nativeCtxLimit) {
    if (note) note.remove();
    return;
  }
  const cur = Number(currentValues.ctx_size);
  const exceeded = Number.isFinite(cur) && cur > nativeCtxLimit;
  const near = Number.isFinite(cur) && !exceeded && cur >= nativeCtxLimit * 0.9;
  if (!exceeded && !near) {
    if (note) note.remove();
    return;
  }
  if (!note) {
    note = document.createElement('div');
    note.className = 'native-ctx-note';
    // 插在控件之后、unsupported 提示（若有）之前
    field.insertBefore(note, field.querySelector('.param-field-warn'));
  }
  note.classList.toggle('warn', exceeded);
  // 超限不阻止（有些模型配合 RoPE 缩放确实会用超过原生上限的值），但要明确提示
  note.textContent = exceeded
    ? t('params.nativeCtxExceeded', { n: nativeCtxLimit })
    : t('params.nativeCtx', { n: nativeCtxLimit });
}

// 防抖更新命令预览（连续输入时合并请求）
let _cmdTimer = null;
const commandPreviewGate = createRequestGate();
const vramEstimateGate = createRequestGate();

// 编辑器是单实例浮层：closeModal() 会整体移除 DOM，但两个防抖计时器挂在
// 模块作用域上 —— 不自清的话关窗后仍会触发一次请求。
document.addEventListener('modal-closed', () => {
  if (_cardTimer) {
    clearTimeout(_cardTimer);
    _cardTimer = null;
  }
  if (_cmdTimer) {
    clearTimeout(_cmdTimer);
    _cmdTimer = null;
  }
});

function scheduleCommandPreview() {
  if (_cmdTimer) clearTimeout(_cmdTimer);
  _cmdTimer = setTimeout(updateCommandPreview, 200);
}

async function updateCommandPreview() {
  // 跳过被禁用的参数（避免输出无效项）
  const params = collectParams();
  const request = commandPreviewGate.begin();
  try {
    const r = await ctx.Api.buildCommand(params, selectedRuntimeId || undefined);
    if (!commandPreviewGate.isCurrent(request)) return;
    lastCommand = r.command || '';
    lastSuppressed = Array.isArray(r.suppressed) ? r.suppressed : [];
  } catch (e) {
    if (!commandPreviewGate.isCurrent(request)) return;
    lastSuppressed = [];
    lastCommand = `// ${t('params.cmdBuildFailed')}: ${localizeBackendError(e.message)}`;
  }
  const failed = lastCommand.startsWith('//');
  const pre = root && root.querySelector('#cmd-preview');
  if (pre) {
    pre.textContent = lastCommand;
    pre.classList.toggle('cmd-error', failed);
  }
  // 命令条上的一行等宽摘要（CSS 截断，全文点开看）
  const summary = root && root.querySelector('#pe-cmd-summary');
  if (summary) {
    summary.textContent = lastCommand;
    summary.classList.toggle('cmd-error', failed);
  }
  const copyBtn = root && root.querySelector('#btn-copy-cmd');
  if (copyBtn) {
    copyBtn.disabled = failed;
    copyBtn.title = failed ? t('params.cmdCopyUnavailable') : '';
  }
  renderSuppressedHint();
}

/**
 * 把"被丢弃的参数"显式画出来。
 *
 * 后端必须丢弃本机构建不认的 flag（写下去必然启动失败），但丢弃**不能是静默的**
 * —— 否则用户勾了开关、命令行里没有、也不报错，等于功能悄悄消失。
 * 形态是 chip 组：每个被省略参数一枚小 chip，hover 出「全名 + flag」全称。
 */
function renderSuppressedHint() {
  if (!root) return;
  const box = root.querySelector('#cmd-suppressed');
  if (!box) return;
  if (!lastSuppressed.length) {
    box.hidden = true;
    box.textContent = '';
    return;
  }
  const chips = lastSuppressed.map((key) => {
    const p = schemaMap[key];
    const name = p ? paramLabel(p) : key;
    const full = p && p.cli ? `${name} ${p.cli}` : name;
    return `<span class="supp-chip" tabindex="0" data-tip="${ctx.escapeHtml(full)}">${ctx.escapeHtml(name)}</span>`;
  });
  box.hidden = false;
  box.innerHTML = `${ctx.escapeHtml(t('params.suppressedHint'))}：${chips.join('')}`;
}

function collectParams() {
  const params = {};
  for (const key in schemaMap) {
    const p = schemaMap[key];
    if (!isParamEnabled(p, currentValues)) continue;
    const v = currentValues[key];
    if (v === '' || v === null || v === undefined) continue;
    // 多值参数空选（如未选择任何推测解码实现）视为"未指定"，不发送
    if (Array.isArray(v) && v.length === 0) continue;
    params[key] = v;
  }
  return params;
}

// ========== 原生文件/目录选择（使用 Tauri 自定义命令） ==========

async function openNativeFileDialog(param, type) {
  try {
    // 检查是否在 Tauri 环境中（Tauri v2 中 invoke 在 core 下）
    if (window.__TAURI__) {
      let path;
      if (type === 'dir') {
        // 调用自定义目录选择命令（同步阻塞，返回 Option<String>）
        path = await tauriInvoke('open_directory_dialog');
      } else {
        // 调用自定义文件选择命令
        path = await tauriInvoke('open_file_dialog');
      }

      if (path) {
        currentValues[param.key] = path;
        refreshAllControls();
        updateCommandPreview();
        ctx.showToast(t('params.selected', { path }), 'success', 1500);
      }
    } else {
      // 非 Tauri 环境（如普通浏览器），降级为提示手动输入
      ctx.showToast(t('params.manualPathHint'), 'warning', 3000);
    }
  } catch (e) {
    console.error('open file dialog failed:', e);
    const errorMsg = e && e.message ? e.message : (e ? String(e) : t('params.unknownError'));
    ctx.showToast(`${t('params.dialogFailed')}: ${errorMsg}`, 'error');
  }
}

// ========== 浮层内联展开区（inline-sheet） ==========
// showModal 单实例、开头先 closeModal()，编辑器自身不能再用二级弹窗
// （否则一开确认框，编辑器浮层和里面的 currentValues 界面就没了）。
// 启动确认 / 保存预设 / 重置确认共用同一个 sheet 容器，同一时刻只开一个。

function openInlineSheet(kind) {
  const sheet = root && root.querySelector('#pe-sheet');
  if (!sheet) return;
  if (!sheet.hidden && sheet.dataset.kind === kind) {
    closeInlineSheet(); // 再点一次同一个按钮 = 收起
    return;
  }
  sheet.dataset.kind = kind;
  sheet.hidden = false;
  if (kind === 'launch') {
    // 检查必填项（原 openLaunchConfirm 的校验）
    for (const key in schemaMap) {
      const p = schemaMap[key];
      if (p.required && isParamEnabled(p, currentValues)) {
        const v = currentValues[key];
        if (v === '' || v === null || v === undefined) {
          ctx.showToast(t('params.requiredMissing', { label: paramLabel(p) }), 'warning');
          closeInlineSheet();
          return;
        }
      }
    }
    sheet.innerHTML = `
      <div class="inline-sheet-title">${ctx.escapeHtml(t('params.confirmLaunch'))}</div>
      <p class="inline-sheet-desc">${ctx.escapeHtml(t('params.launchDesc'))}</p>
      <pre class="pe-cmd-full">${ctx.escapeHtml(lastCommand)}</pre>
      <div class="form-group">
        <label class="form-label">${ctx.escapeHtml(t('dash.launchName'))}</label>
        <input type="text" class="input" id="launch-name" placeholder="${ctx.escapeHtml(t('dash.namePh'))}" />
      </div>
      <div class="inline-sheet-actions">
        <button class="btn btn-secondary" data-sheet-cancel>${ctx.escapeHtml(t('app.cancel'))}</button>
        <button class="btn btn-primary" data-sheet-confirm>${ctx.escapeHtml(t('dash.start'))}</button>
      </div>
    `;
    sheet.querySelector('[data-sheet-cancel]').addEventListener('click', closeInlineSheet);
    sheet.querySelector('[data-sheet-confirm]').addEventListener('click', (event) =>
      doLaunch(event.currentTarget)
    );
    sheet.querySelector('#launch-name').focus();
  } else if (kind === 'preset') {
    sheet.innerHTML = `
      <div class="inline-sheet-title">${ctx.escapeHtml(t('params.savePreset'))}</div>
      <div class="form-group">
        <label class="form-label">${ctx.escapeHtml(t('params.presetName'))} <span class="required">*</span></label>
        <input type="text" class="input" id="preset-name" placeholder="${ctx.escapeHtml(t('params.presetNamePh'))}"/>
      </div>
      <div class="form-group">
        <label class="form-label">${ctx.escapeHtml(t('params.description'))}</label>
        <textarea class="textarea" id="preset-desc" placeholder="${ctx.escapeHtml(t('params.descPh'))}"></textarea>
      </div>
      <div class="form-group">
        <label class="form-label">${ctx.escapeHtml(t('params.tags'))}</label>
        <input type="text" class="input" id="preset-tags" placeholder="${ctx.escapeHtml(t('params.tagsPh'))}"/>
      </div>
      <div class="inline-sheet-actions">
        <button class="btn btn-secondary" data-sheet-cancel>${ctx.escapeHtml(t('app.cancel'))}</button>
        <button class="btn btn-primary" data-sheet-confirm>${ctx.escapeHtml(t('params.save'))}</button>
      </div>
    `;
    sheet.querySelector('[data-sheet-cancel]').addEventListener('click', closeInlineSheet);
    sheet.querySelector('[data-sheet-confirm]').addEventListener('click', () => doSavePreset());
    sheet.querySelector('#preset-name').focus();
  } else if (kind === 'reset') {
    sheet.innerHTML = `
      <div class="inline-sheet-title">${ctx.escapeHtml(t('params.confirmReset'))}</div>
      <div class="inline-sheet-actions">
        <button class="btn btn-secondary" data-sheet-cancel>${ctx.escapeHtml(t('app.cancel'))}</button>
        <button class="btn btn-primary" data-sheet-confirm>${ctx.escapeHtml(t('params.resetDefaults'))}</button>
      </div>
    `;
    sheet.querySelector('[data-sheet-cancel]').addEventListener('click', closeInlineSheet);
    sheet.querySelector('[data-sheet-confirm]').addEventListener('click', () => {
      Object.assign(currentValues, JSON.parse(JSON.stringify(defaults)));
      refreshAllControls();
      updateCommandPreview();
      closeInlineSheet();
      ctx.showToast(t('params.resetDone'), 'info', 1500);
    });
  }
}

function closeInlineSheet() {
  const sheet = root && root.querySelector('#pe-sheet');
  if (!sheet) return;
  sheet.hidden = true;
  sheet.innerHTML = '';
  delete sheet.dataset.kind;
}

async function doSavePreset() {
  const sheet = root && root.querySelector('#pe-sheet');
  if (!sheet) return;
  const name = sheet.querySelector('#preset-name').value.trim();
  const desc = sheet.querySelector('#preset-desc').value.trim();
  const tagsStr = sheet.querySelector('#preset-tags').value.trim();
  if (!name) {
    ctx.showToast(t('params.nameRequired'), 'warning');
    return;
  }
  const tags = tagsStr ? tagsStr.split(/[,，]/).map((s) => s.trim()).filter(Boolean) : [];
  // 只保存启用的参数
  const params = collectParams();
  try {
    // runtime_id 随预设保存：同一组参数在不同构建上的 supported 集可能不同，
    // 重新载入预设时应恢复当时的运行环境选择。
    await ctx.Api.createPreset({
      name,
      description: desc,
      tags,
      params,
      ...(selectedRuntimeId ? { runtime_id: selectedRuntimeId } : {}),
    });
    ctx.showToast(t('params.presetSaved'), 'success');
    closeInlineSheet();
    refreshPresetDropdown();
  } catch (e) {
    ctx.showToast(`${t('params.saveFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

async function doLaunch(button) {
  if (button && button.disabled) return;
  const sheet = root && root.querySelector('#pe-sheet');
  if (!sheet) return;
  if (button) {
    button.disabled = true;
    button.setAttribute('aria-busy', 'true');
  }
  try {
    const params = collectParams();
    // 端口等于 schema 默认值时视为“未指定”，交给后端自动分配，
    // 避免连续启动第二个实例时撞默认端口（与仪表盘启动弹窗的行为一致）
    if (shouldAutoAssignPort(params, defaults)) params.port = 0;
    const name = sheet.querySelector('#launch-name').value.trim();
    const body = { params, name };
    if (selectedRuntimeId) body.runtime_id = selectedRuntimeId;
    if (!(await confirmLaunch(ctx, body))) return;
    try {
      const inst = await ctx.Api.createInstance(body);
      ctx.showToast(t('dash.launched', { name: inst.name, port: inst.port }), 'success');
      closeInlineSheet();
      ctx.closeModal(); // 启动完成即收起编辑器（与旧行为一致：确认弹窗关闭）
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

// ========== 快捷键与跨模块入口 ==========

/**
 * `Ctrl+Enter` = 打开启动确认（与 `#btn-launch-now` 是同一个动作）。
 * 绑在浮层 overlay 上：只有焦点在编辑器内才会触发（其他页面/浮层不抢键），
 * 且随浮层销毁自动解绑 —— 取代原 onShow/onHide 的全局绑定与解绑。
 */
function onModalKeydown(event) {
  // ESC 分层：先收起内联 sheet，再让全局 ESC 关闭编辑器。
  if (event.key === 'Escape') {
    const sheet = root && root.querySelector('#pe-sheet');
    if (sheet && !sheet.hidden) {
      event.preventDefault();
      event.stopPropagation();
      closeInlineSheet();
    }
    return;
  }
  if (!(event.ctrlKey || event.metaKey) || event.key !== 'Enter') return;
  event.preventDefault();
  openInlineSheet('launch');
}

/** 把模型库/顶栏选择器选中的模型填进 model 字段。 */
function applyPendingModel() {
  if (!pendingModelPath || !schema || !schemaMap.model) return;
  const path = pendingModelPath;
  pendingModelPath = null;
  currentValues.model = path;
  // refreshAllControls 本就负责把 currentValues 回填到控件（预设加载/重置默认也走它），
  // 所以这里不需要直接碰 DOM
  refreshAllControls();
  updateCommandPreview();
  updateModalTitle();
  ctx.showToast(t('params.modelPicked', { name: path.split(/[\\/]/).pop() }), 'success');
}
