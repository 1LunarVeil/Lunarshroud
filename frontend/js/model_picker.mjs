// 顶栏的模型选择器（Ctrl+L）
//
// 与「模型库」页共用同一份扫描结果（`/api/models/scan?with_capabilities=true`），
// 但只做一件事：**快速挑一个模型进参数编辑器**。留在独立模块里而不是塞进
// app.js —— 后者是外壳（路由 / Toast / Modal / 实例状态中心），放业务会越滚越大。
//
// 为什么不直接复用模型库页：那页是"浏览 + 比较"（列表 + 详情 + 标签筛选），
// 而这里要的是"敲两个字母就选中"，两者的取舍不同（这里不显示配套文件、
// 不做筛选标签）。共享的是**数据源与跨页传参约定**，不是 UI。

import { t } from './i18n.mjs';

const MAX_ROWS = 300;

/** 选中项是否"可加载"：mmproj / assistant 头不是能独立加载的模型 */
function isLoadable(model) {
  const caps = model.capabilities || {};
  return !(caps.is_mmproj || caps.is_assistant_model);
}

function matches(model, keyword) {
  if (!keyword) return true;
  const haystack = [
    model.filename,
    model.path,
    (model.capabilities || {}).architecture,
    (model.capabilities || {}).file_type_name,
  ]
    .filter(Boolean)
    .join(' ')
    .toLowerCase();
  return haystack.includes(keyword);
}

/**
 * 打开选择器。
 * @param {object} ctx app 上下文（见 app.js 的 buildAppContext）
 */
export async function open(ctx) {
  const state = { models: [], rows: [], selected: null, keyword: '' };

  const wrap = document.createElement('div');
  wrap.className = 'model-picker';
  wrap.innerHTML = `
    <input type="text" class="input model-picker-search" id="picker-search"
           placeholder="${ctx.escapeHtml(t('picker.searchHint'))}" />
    <div class="model-picker-list" id="picker-list"></div>
    <div class="model-picker-foot" id="picker-foot"></div>
  `;

  const overlay = ctx.showModal(t('picker.title'), wrap, [
    { label: t('app.cancel'), class: 'btn-secondary' },
    {
      label: t('picker.configure'),
      class: 'btn-primary',
      onClick: () => {
        if (!state.selected) return false; // 没选中就不关窗
        useInEditor(ctx, state.selected);
      },
    },
  ]);

  const listEl = wrap.querySelector('#picker-list');
  const footEl = wrap.querySelector('#picker-foot');
  const searchEl = wrap.querySelector('#picker-search');

  function paint() {
    const keyword = state.keyword.trim().toLowerCase();
    state.rows = state.models.filter((m) => matches(m, keyword)).slice(0, MAX_ROWS);

    if (!state.rows.some((m) => m.path === state.selected)) {
      state.selected = state.rows.length ? state.rows[0].path : null;
    }

    if (!state.rows.length) {
      listEl.innerHTML = `<div class="model-picker-empty">${ctx.escapeHtml(
        state.models.length ? t('picker.noMatch') : t('picker.empty')
      )}</div>`;
    } else {
      listEl.innerHTML = state.rows.map(rowHtml).join('');
      // 选中态视觉（state.selected 已定，行渲染后同步类；键盘 ↑↓ 与 Enter 都读它）
      listEl.querySelectorAll('.model-picker-row').forEach((el) => {
        el.classList.toggle('selected', el.dataset.path === state.selected);
      });
      listEl.querySelectorAll('.model-picker-row').forEach((el) => {
        // 单击 = 选中并直接切换到编辑器浮层（选择器选完模型的目标只有一个：
        // 配参数，中间的"再点一次打开"是纯损耗）。取消按钮保留退出路径。
        el.addEventListener('click', () => useInEditor(ctx, el.dataset.path));
      });
      // 键盘导航后的选中行要滚进视野
      const active = listEl.querySelector('.model-picker-row.selected');
      if (active) active.scrollIntoView({ block: 'nearest' });
    }
    paintFoot();
  }

  function moveSelection(delta) {
    if (!state.rows.length) return;
    const idx = state.rows.findIndex((m) => m.path === state.selected);
    const next = Math.min(Math.max(idx + delta, 0), state.rows.length - 1);
    if (next === idx) return;
    state.selected = state.rows[next].path;
    // 只更新选中类，不重渲染列表（避免丢失滚动位置之外的整页重建）
    listEl.querySelectorAll('.model-picker-row').forEach((el) => {
      el.classList.toggle('selected', el.dataset.path === state.selected);
    });
    const active = listEl.querySelector('.model-picker-row.selected');
    if (active) active.scrollIntoView({ block: 'nearest' });
  }

  function paintFoot() {
    const count = state.rows.length;
    const truncated = state.models.length > count && !state.keyword;
    // 左侧快捷键提示（键盘用户的说明书），右侧计数
    footEl.innerHTML = `
      <span class="model-picker-kbd">${ctx.escapeHtml(t('picker.kbdHint'))}</span>
      <span>${ctx.escapeHtml(
        truncated
          ? t('picker.footTruncated', { shown: count, total: state.models.length })
          : t('picker.foot', { count })
      )}</span>
    `;
  }

  searchEl.addEventListener('input', () => {
    state.keyword = searchEl.value;
    paint();
  });
  // 键盘操作全在搜索框上：↑↓ 移动选中、Enter 打开、Esc 关闭（全局已有）
  searchEl.addEventListener('keydown', (e) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      moveSelection(1);
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      moveSelection(-1);
    } else if (e.key === 'Enter' && state.selected) {
      e.preventDefault();
      useInEditor(ctx, state.selected);
    }
  });

  searchEl.focus();
  listEl.innerHTML = `<div class="model-picker-empty">${ctx.escapeHtml(t('picker.loading'))}</div>`;

  let payload = null;
  try {
    payload = await ctx.Api.scanModels(undefined, true);
  } catch (e) {
    payload = null;
    listEl.innerHTML = `<div class="model-picker-empty">${ctx.escapeHtml(
      t('models.scanFailed')
    )}</div>`;
  }
  if (!overlay.isConnected) return; // 等待期间用户已经关掉了

  if (payload) {
    state.models = (Array.isArray(payload.models) ? payload.models : []).filter(isLoadable);
    paint();
  }
}

function rowHtml(model) {
  const caps = model.capabilities || {};
  const badges = [];
  if (caps.provides_builtin_mtp) badges.push(t('models.badgeBuiltinMtp'));
  if (caps.has_nextn_weights && !caps.provides_builtin_mtp) badges.push(t('models.badgeAssistant'));
  if (caps.has_vision_encoder) badges.push(t('models.badgeVision'));

  const facts = [
    caps.file_type_name,
    caps.context_length ? `${Math.round(caps.context_length / 1024)}K ctx` : null,
    model.size_human,
  ].filter(Boolean);

  return `
    <div class="model-picker-row" data-path="${escapeAttr(model.path)}">
      <div class="model-picker-main">
        <div class="model-picker-name">${escapeText(model.filename)}</div>
        <div class="model-picker-facts">${escapeText(facts.join(' · '))}</div>
      </div>
      ${
        badges.length
          ? `<div class="model-picker-badges">${badges
              .map((b) => `<span class="tag">${escapeText(b)}</span>`)
              .join('')}</div>`
          : ''
      }
    </div>
  `;
}

function useInEditor(ctx, path) {
  if (!path) return;
  // 跨模块传参沿用既有约定（models.js 的"在参数编辑器中配置"也是这一条）。
  // param_editor 已是模态型模块：closeModal() 关掉选择器，navigate() 打开编辑器浮层，
  // showModal 单实例保证无缝替换。
  window.__pendingModelPath = path;
  ctx.closeModal();
  ctx.navigate('param_editor');
}

// 这里的转义刻意不依赖 ctx：rowHtml 是模块内的纯函数，少一个隐式依赖就少一处
// "换个 ctx 就不安全"的可能。规则与 app.js 的 escapeHtml 保持一致。
function escapeText(value) {
  return String(value === null || value === undefined ? '' : value)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function escapeAttr(value) {
  return escapeText(value);
}
