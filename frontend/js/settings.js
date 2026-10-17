// 设置（模态浮层）
// 左侧分类导航 + 右侧单分类表单。全局配置编辑 + 保存 + 重启后端提示
//
// 为什么是浮层而不是页面：设置不是"目的地"，而是随时召唤的覆盖层。
// app.js 按 kind === 'modal' 把它渲染成侧栏动作项，点击时打开浮层、不改变当前路由。

import { tauriInvoke } from './api.js?v=1';
import { t, localizeBackendError, getLocale, setLocale, LOCALE_NAMES } from './i18n.mjs';

export const id = 'settings';
export const title = '设置';
export const kind = 'modal';

let ctx = null;
let root = null;       // 浮层内的内容容器，每次 open() 重新赋值
let navEl = null;
let config = {};       // 后端返回的已保存配置
let draft = null;      // 未保存的编辑缓冲（切分类时必须靠它保住用户的输入）
let activeGroup = 'general';

export function open(appCtx) {
  ctx = appCtx;
  const wrap = document.createElement('div');
  wrap.className = 'settings-layout';
  wrap.innerHTML = `
    <nav class="settings-nav" id="settings-nav"></nav>
    <div class="settings-pane" id="settings-pane">
      <div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(t('settings.loading'))}</div>
    </div>
  `;
  const overlay = ctx.showModal(t('settings.title'), wrap, [
    {
      label: t('settings.reload'),
      class: 'btn-secondary',
      size: 'lg',
      onClick: () => {
        load();
        return false;
      },
    },
    {
      label: t('settings.save'),
      class: 'btn-primary',
      size: 'lg',
      onClick: () => {
        save();
        return false;
      },
    },
  ]);
  overlay.querySelector('.modal').classList.add('modal-settings');
  root = overlay.querySelector('#settings-pane');
  navEl = overlay.querySelector('#settings-nav');
  activeGroup = 'general';
  renderNav();
  load();
}

function renderNav() {
  navEl.innerHTML = GROUPS.map(
    (g) => `
    <button type="button" class="settings-nav-item ${g.key === activeGroup ? 'active' : ''}" data-cat="${ctx.escapeHtml(g.key)}">
      ${ctx.escapeHtml(t(g.labelKey))}
    </button>`
  ).join('');
  navEl.querySelectorAll('[data-cat]').forEach((btn) => {
    btn.addEventListener('click', () => switchGroup(btn.dataset.cat));
  });
}

// 切分类前必须先把当前输入收进 draft，否则未保存的编辑会被下一次渲染丢掉
function switchGroup(key) {
  if (key === activeGroup) return;
  collect();
  activeGroup = key;
  renderNav();
  renderPane();
}

async function load() {
  try {
    config = await ctx.Api.getSystemConfig();
    // 编辑缓冲从后端值起步
    draft = { ...config };
    renderPane();
  } catch (e) {
    ctx.showToast(`${t('settings.loadFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}

// ========== 分类导航 ==========

// 分类顺序 = 左侧导航顺序。用 key → 分类的映射表分组，
// 而不是给 13 条字段定义逐个加标签 —— 改动面更小，字段定义保持原样。
const GROUPS = [
  { key: 'general', labelKey: 'settings.catGeneral' },
  { key: 'paths', labelKey: 'settings.catPaths' },
  { key: 'server', labelKey: 'settings.catServer' },
  { key: 'runtime', labelKey: 'settings.catRuntime' },
  { key: 'storage', labelKey: 'settings.catStorage' },
  { key: 'advanced', labelKey: 'settings.catAdvanced' },
];

const FIELD_GROUP_OF = {
  locale: 'general',
  theme: 'general',
  llama_server_path: 'paths',
  default_model_dir: 'paths',
  launcher_host: 'server',
  launcher_port: 'server',
  instance_port_start: 'server',
  instance_port_end: 'server',
  startup_timeout: 'runtime',
  health_check_interval_ms: 'runtime',
  monitor_interval_seconds: 'runtime',
  monitor_history_seconds: 'runtime',
  log_buffer_lines: 'storage',
  history_max_records: 'storage',
  persist_instance_logs: 'storage',
};

// 把当前分类的输入收进 draft。切分类和保存前都要调一次。
function collect() {
  if (!root || !draft) return;
  root.querySelectorAll('[data-field]').forEach((el) => {
    const k = el.dataset.field;
    if (el.type === 'checkbox') {
      draft[k] = el.checked;
      return;
    }
    if (el.type === 'number') {
      // 留空则保留原值，避免 null 写进配置
      if (el.value === '') return;
      const n = Number(el.value);
      if (!Number.isNaN(n)) draft[k] = n;
      return;
    }
    draft[k] = el.value;
  });
  const themeRadio = root.querySelector('input[name="theme"]:checked');
  if (themeRadio) draft.theme = themeRadio.value;
}

// 高级分类：重启说明（原来是弹窗套弹窗，现在直接铺在面板里）
function renderAdvancedPane() {
  root.innerHTML = `
    <div class="form-help mb-2">${ctx.escapeHtml(t('settings.restartHint'))}</div>
    <div class="card-title mb-2">${ctx.escapeHtml(t('settings.restartTitle'))}</div>
    <ul class="settings-restart-list">
      ${[1, 2, 3, 4, 5, 6].map((i) => `<li>${ctx.escapeHtml(t(`settings.restartItem${i}`))}</li>`).join('')}
    </ul>
    <p style="margin-top: 12px;">${ctx.escapeHtml(t('settings.restartFooter'))}</p>
  `;
}

function renderPane() {
  if (!root || !draft) return;
  if (activeGroup === 'advanced') {
    renderAdvancedPane();
    return;
  }
  const fields = [
    {
      key: 'llama_server_path',
      label: t('settings.serverPath'),
      type: 'file',
      help: t('settings.serverPathHelp'),
      placeholder: 'D:\\llama.cpp\\build\\bin\\Release\\llama-server.exe',
    },
    {
      key: 'default_model_dir',
      label: t('settings.modelDir'),
      type: 'dir',
      help: t('settings.modelDirHelp'),
      placeholder: 'D:\\models',
    },
    {
      key: 'launcher_host',
      label: t('settings.launcherHost'),
      // 只读：后端硬性要求 127.0.0.1（backend/config.py），可编辑=必然 400
      type: 'readonly',
      help: t('settings.launcherHostHelp'),
    },
    {
      key: 'launcher_port',
      label: t('settings.launcherPort'),
      type: 'number',
      help: t('settings.launcherPortHelp'),
      placeholder: '17520',
    },
    {
      key: 'instance_port_start',
      label: t('settings.portStart'),
      type: 'number',
      help: t('settings.portStartHelp'),
      placeholder: '8080',
    },
    {
      key: 'instance_port_end',
      label: t('settings.portEnd'),
      type: 'number',
      help: t('settings.portEndHelp'),
      placeholder: '8090',
    },
    {
      key: 'startup_timeout',
      label: t('settings.startupTimeout'),
      type: 'number',
      help: t('settings.startupTimeoutHelp'),
      placeholder: '60',
    },
    {
      key: 'health_check_interval_ms',
      label: t('settings.healthInterval'),
      type: 'number',
      help: t('settings.healthIntervalHelp'),
      placeholder: '500',
    },
    {
      key: 'monitor_interval_seconds',
      label: t('settings.monitorInterval'),
      type: 'number',
      help: t('settings.monitorIntervalHelp'),
      placeholder: '5',
    },
    {
      key: 'monitor_history_seconds',
      label: t('settings.monitorHistory'),
      type: 'number',
      help: t('settings.monitorHistoryHelp'),
      placeholder: '60',
    },
    {
      key: 'log_buffer_lines',
      label: t('settings.logBuffer'),
      type: 'number',
      help: t('settings.logBufferHelp'),
      placeholder: '5000',
    },
    {
      key: 'history_max_records',
      label: t('settings.historyMax'),
      type: 'number',
      help: t('settings.historyMaxHelp'),
      placeholder: '1000',
    },
    {
      key: 'persist_instance_logs',
      label: t('settings.persistLogs'),
      type: 'checkbox',
      help: t('settings.persistLogsHelp'),
    },
    {
      key: 'locale',
      label: t('settings.language'),
      type: 'locale',
      help: t('settings.languageHelp'),
    },
    {
      key: 'theme',
      label: t('settings.theme'),
      type: 'theme',
      help: t('settings.themeHelp'),
    },
  ];
  root.innerHTML = fields
    .filter((f) => FIELD_GROUP_OF[f.key] === activeGroup)
    .map(
      (f) => `
      <div class="param-field">
        <div class="param-field-label"><span>${ctx.escapeHtml(f.label)}</span></div>
        <div class="param-field-control">${controlFor(f, draft[f.key])}</div>
        <div class="param-field-help">${ctx.escapeHtml(f.help || '')}</div>
      </div>`
    )
    .join('');

  // 主题切换实时预览
  root.querySelectorAll('input[name="theme"]').forEach((r) => {
    r.addEventListener('change', (e) => ctx.setTheme(e.target.value));
  });
  // 语言在界面层，写入 localStorage 后需刷新页面才生效
  root.querySelectorAll('input[name="locale"]').forEach((r) => {
    r.addEventListener('change', (e) => {
      if (setLocale(e.target.value)) {
        ctx.showToast(t('app.langSwitched'), 'info', 600);
        setTimeout(() => location.reload(), 500);
      }
    });
  });
  // 浏览按钮
  root.querySelectorAll('[data-browse]').forEach((btn) => {
    btn.addEventListener('click', () => openBrowse(btn.dataset.browse, btn.dataset.browseType));
  });
}

function controlFor(f, value) {
  const v = value === null || value === undefined ? '' : value;
  const ph = f.placeholder ? `placeholder="${ctx.escapeHtml(f.placeholder)}"` : '';
  if (f.type === 'locale') {
    const current = getLocale();
    return Object.entries(LOCALE_NAMES)
      .map(
        ([code, name]) => `
      <label class="settings-radio">
        <input type="radio" name="locale" value="${ctx.escapeHtml(code)}" ${code === current ? 'checked' : ''}/> ${ctx.escapeHtml(name)}
      </label>`
      )
      .join('');
  }
  if (f.type === 'theme') {
    return `
      <label class="settings-radio">
        <input type="radio" name="theme" value="light" ${v === 'light' ? 'checked' : ''}/> ${ctx.escapeHtml(t('settings.themeLight'))}
      </label>
      <label class="settings-radio">
        <input type="radio" name="theme" value="dark" ${v === 'light' ? '' : 'checked'}/> ${ctx.escapeHtml(t('settings.themeDark'))}
      </label>`;
  }
  if (f.type === 'readonly') {
    // 后端硬性要求 launcher_host == 127.0.0.1（backend/config.py 的校验，
    // 且 test_regressions 专门守护），改任何值保存必 400 —— 所以只读展示。
    return `<input type="text" class="input" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" readonly/>`;
  }
  if (f.type === 'file' || f.type === 'dir') {
    return `<input type="text" class="input" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}/><button class="btn btn-secondary btn-sm" data-browse="${f.key}" data-browse-type="${f.type}">${ctx.escapeHtml(t('params.browse'))}</button>`;
  }
  if (f.type === 'checkbox') {
    return `<label class="switch"><input type="checkbox" data-field="${f.key}" ${v ? 'checked' : ''}/><span class="switch-slider"></span></label>`;
  }
  if (f.type === 'number') {
    return `<input type="number" class="input" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}/>`;
  }
  return `<input type="text" class="input" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}/>`;
}

async function openBrowse(key, type) {
  try {
    // 检查是否在 Tauri 环境中（Tauri v2 中 invoke 在 core 下）
    if (window.__TAURI__) {
      let path;
      if (type === 'dir') {
        // 选择目录
        path = await tauriInvoke('open_directory_dialog');
      } else {
        path = await tauriInvoke('open_file_dialog');
      }

      if (path) {
        const inp = root.querySelector(`input[data-field="${key}"]`);
        if (inp) inp.value = path;
        ctx.showToast(t('params.selected', { path }), 'success', 1500);
      }
    } else {
      // 非 Tauri 环境，提示手动输入
      ctx.showToast(t('params.manualPathHint'), 'warning', 3000);
    }
  } catch (e) {
    console.error('open file dialog failed:', e);
    const errorMsg = e && e.message ? e.message : (e ? String(e) : t('params.unknownError'));
    ctx.showToast(`${t('params.dialogFailed')}: ${errorMsg}`, 'error');
  }
}

async function save() {
  // 先把当前分类的输入收进 draft（其它分类的值在上次切换时已收好）
  collect();
  const updates = { ...draft };
  // launcher_host 是只读项：后端只接受 127.0.0.1，提交它没有任何意义
  delete updates.launcher_host;

  // 简单校验
  if (!updates.llama_server_path) {
    ctx.showToast(t('settings.serverPathRequired'), 'warning');
    return;
  }
  if (updates.launcher_port && (updates.launcher_port < 1 || updates.launcher_port > 65535)) {
    ctx.showToast(t('settings.portRangeWarn'), 'warning');
    return;
  }
  if (updates.instance_port_start && updates.instance_port_end &&
      updates.instance_port_start > updates.instance_port_end) {
    ctx.showToast(t('settings.portOrderWarn'), 'warning');
    return;
  }

  try {
    await ctx.Api.updateSystemConfig(updates);
    config = { ...config, ...updates };
    ctx.showToast(t('settings.saved'), 'success');
    // 主题立即应用
    if (updates.theme) ctx.setTheme(updates.theme);
    // 保存成功即关闭浮层（对齐 LM Studio 的行为）
    ctx.closeModal();
  } catch (e) {
    ctx.showToast(`${t('settings.saveFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}
