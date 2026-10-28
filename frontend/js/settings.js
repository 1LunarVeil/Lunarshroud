// 设置（模态浮层）
// 左侧分类导航 + 右侧单分类表单。全局配置编辑 + 保存 + 重启后端提示
//
// 为什么是浮层而不是页面：设置不是"目的地"，而是随时召唤的覆盖层。
// app.js 按 kind === 'modal' 把它渲染成侧栏动作项，点击时打开浮层、不改变当前路由。

import { tauriInvoke } from './api.js?v=1';
import { t, localizeBackendError, getLocale, setLocale, LOCALE_NAMES } from './i18n.mjs';
import { hasUsableServer } from './configured_state.mjs';

export const id = 'settings';
export const title = '设置';
export const kind = 'modal';

let ctx = null;
let root = null;       // 浮层内的内容容器，每次 open() 重新赋值
let navEl = null;
let config = {};       // 后端返回的已保存配置
let draft = null;      // 未保存的编辑缓冲（切分类时必须靠它保住用户的输入）
let baseline = null;   // 打开时的展示基线（吸收 null→''、主题偏好等渲染归一化）
let normalizedGroups = new Set(); // 已建立基线的分类；首次进入才归一化
let activeGroup = 'general';
let allowClose = false; // 保存成功/明确放弃后跳过未保存提示

export function open(appCtx) {
  ctx = appCtx;
  const wrap = document.createElement('div');
  wrap.className = 'settings-layout';
  // 内容异步加载，初始焦点先落在容器上；否则会聚焦到页脚“保存设置”。
  wrap.tabIndex = -1;
  wrap.innerHTML = `
    <nav class="settings-nav" id="settings-nav"></nav>
    <div class="settings-pane" id="settings-pane">
      <div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(t('settings.loading'))}</div>
    </div>
  `;
  const overlay = ctx.showModal(
    t('settings.title'),
    wrap,
    [
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
    ],
    { initialFocus: wrap }
  );
  overlay.querySelector('.modal').classList.add('modal-settings');
  root = overlay.querySelector('#settings-pane');
  navEl = overlay.querySelector('#settings-nav');
  activeGroup = 'general';
  allowClose = false;
  baseline = null;
  normalizedGroups = new Set();
  interceptDirtyClose(overlay);
  renderNav();
  load();
}

/**
 * X / ESC / 遮罩关闭前检查未保存修改。
 *
 * showModal 的 onClose 在 DOM 移除后才执行，救不回草稿；因此在捕获阶段
 * 拦住这三个关闭入口，用叠加的确认框让用户选择。保存成功会设置 allowClose。
 */
function interceptDirtyClose(overlay) {
  const closeButton = overlay.querySelector('.modal-close');
  if (closeButton) {
    closeButton.addEventListener(
      'click',
      (event) => {
        if (!isDirty()) return;
        event.preventDefault();
        event.stopImmediatePropagation();
        askDiscard();
      },
      true
    );
  }
  overlay.addEventListener(
    'click',
    (event) => {
      if (event.target !== overlay || !isDirty()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      askDiscard();
    },
    true
  );
  overlay.addEventListener(
    'keydown',
    (event) => {
      if (event.key !== 'Escape' || !isDirty()) return;
      event.preventDefault();
      event.stopPropagation();
      askDiscard();
    },
    true
  );
  // Ctrl+L (model picker) and Ctrl+K (command palette) open another modal,
  // which replaces all modals and would discard the draft without passing
  // through the close button. Catch both while focus is inside Settings.
  overlay.addEventListener(
    'keydown',
    (event) => {
      if (!(event.ctrlKey || event.metaKey)) return;
      if (!['l', 'k'].includes(event.key.toLowerCase())) return;
      if (!isDirty()) return;
      event.preventDefault();
      event.stopImmediatePropagation();
      askDiscard();
    },
    true
  );
}

function isDirty() {
  if (allowClose || !draft || !baseline) return false;
  collect();
  try {
    return JSON.stringify(draft) !== JSON.stringify(baseline);
  } catch {
    return true;
  }
}

/**
 * 记录当前分类首次渲染后的展示值作为基线。
 *
 * 不能用后端 config 直接当基线：输入框会把 null 显示成空串，主题单选项来自
 * 前端的 localStorage 偏好，语言单选项来自当前界面语言 —— 这些渲染归一化
 * 会让“打开后立刻关闭”也误报未保存修改。首次进入分类时把控件回读值写入
 * baseline，之后只有用户真的改动才算 dirty。
 */
function captureGroupBaseline() {
  if (!draft || !baseline || normalizedGroups.has(activeGroup)) return;
  collect();
  root.querySelectorAll('[data-field]').forEach((el) => {
    baseline[el.dataset.field] = draft[el.dataset.field];
  });
  const themeRadio = root.querySelector('input[name="theme"]:checked');
  if (themeRadio) baseline.theme = themeRadio.value;
  normalizedGroups.add(activeGroup);
}

function askDiscard() {
  ctx.showModal(
    t('settings.unsavedTitle'),
    `<div class="diag-intro">${ctx.escapeHtml(t('settings.unsavedBody'))}</div>`,
    [
      { label: t('settings.keepEditing'), class: 'btn-secondary' },
      {
        label: t('settings.discard'),
        class: 'btn-danger',
        onClick: () => {
          allowClose = true;
          ctx.closeModal(); // 确认框
          ctx.closeModal(); // 设置浮层
        },
      },
    ],
    { stack: true }
  );
}

function renderNav() {
  navEl.innerHTML = GROUPS.map(
    (g) => `
    <button type="button" class="settings-nav-item ${g.key === activeGroup ? 'active' : ''}" data-cat="${ctx.escapeHtml(g.key)}"${g.key === activeGroup ? ' aria-current="true"' : ''}>
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
    // 编辑缓冲从后端值起步；基线也从此起步，再由首次渲染归一化各分类
    draft = { ...config };
    baseline = { ...config };
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
  { key: 'backup', labelKey: 'settings.catBackup' },
  { key: 'advanced', labelKey: 'settings.catAdvanced' },
];

const FIELD_GROUP_OF = {
  locale: 'general',
  theme: 'general',
  close_to_tray: 'general',
  chat_reasoning: 'general',
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
  launch_guardrail: 'runtime',
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

// ========== 数据备份 ==========

function backupTime(epochSeconds) {
  const value = Number(epochSeconds);
  if (!Number.isFinite(value) || value <= 0) return '-';
  try {
    return new Date(value * 1000).toLocaleString();
  } catch {
    return '-';
  }
}

function askBackup(message, title) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    ctx.showModal(
      title || t('backup.confirmTitle'),
      `<div class="diag-intro">${ctx.escapeHtml(message)}</div>`,
      [
        { label: t('app.cancel'), class: 'btn-secondary', onClick: () => done(false) },
        { label: t('app.ok'), class: 'btn-primary', onClick: () => done(true) },
      ],
      { stack: true, onClose: () => done(false) }
    );
  });
}

async function renderBackupPane() {
  if (!root) return;
  root.innerHTML = `<div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(
    t('settings.loading')
  )}</div>`;
  try {
    const data = await ctx.Api.listBackups();
    if (activeGroup !== 'backup' || !root?.isConnected) return;
    renderBackupContent(data);
  } catch (error) {
    if (activeGroup !== 'backup' || !root?.isConnected) return;
    root.innerHTML = `<div class="empty-state">${ctx.escapeHtml(
      t('backup.loadFailed')
    )}: ${ctx.escapeHtml(localizeBackendError(error.message))}</div>`;
  }
}

function renderBackupContent(data) {
  const backups = Array.isArray(data?.backups) ? data.backups : [];
  const pending = data?.pending || null;
  const items = backups
    .map((backup) => {
      const name = String(backup.name || '');
      const valid = backup.valid !== false;
      const meta = [
        backupTime(backup.created_at),
        t('backup.files', { count: Number(backup.files) || 0 }),
        ctx.formatBytes(Number(backup.bytes) || 0),
        valid ? '' : t('backup.invalid'),
      ]
        .filter(Boolean)
        .join(' · ');
      return `
        <div class="backup-item ${valid ? '' : 'invalid'}">
          <div class="backup-item-main">
            <div class="backup-name">${ctx.escapeHtml(name)}</div>
            <div class="backup-meta">${ctx.escapeHtml(meta)}</div>
          </div>
          <div class="backup-item-actions">
            <button type="button" class="btn btn-sm btn-secondary" data-backup-action="verify" data-backup-name="${ctx.escapeHtml(
              name
            )}" ${valid ? '' : 'disabled'}>${ctx.escapeHtml(t('backup.verify'))}</button>
            <button type="button" class="btn btn-sm btn-secondary" data-backup-action="restore" data-backup-name="${ctx.escapeHtml(
              name
            )}" ${valid ? '' : 'disabled'}>${ctx.escapeHtml(t('backup.restore'))}</button>
            <button type="button" class="btn btn-sm btn-ghost backup-delete" data-backup-action="delete" data-backup-name="${ctx.escapeHtml(
              name
            )}">${ctx.escapeHtml(t('backup.delete'))}</button>
          </div>
        </div>`;
    })
    .join('');

  root.innerHTML = `
    <div class="backup-pane">
      <div class="backup-head">
        <div>
          <div class="card-title">${ctx.escapeHtml(t('backup.title'))}</div>
          <div class="form-help">${ctx.escapeHtml(t('backup.hint'))}</div>
        </div>
        <div class="backup-actions">
          <button type="button" class="btn btn-secondary btn-sm" id="btn-backup-refresh">${ctx.escapeHtml(
            t('backup.refresh')
          )}</button>
          <button type="button" class="btn btn-primary btn-sm" id="btn-backup-create">${ctx.escapeHtml(
            t('backup.create')
          )}</button>
        </div>
      </div>
      ${
        pending
          ? `<div class="backup-pending">
               <div><strong>${ctx.escapeHtml(t('backup.pending'))}</strong> ${ctx.escapeHtml(
                 String(pending.name || '')
               )}<div class="form-help">${ctx.escapeHtml(t('backup.pendingHint'))}</div></div>
               <button type="button" class="btn btn-sm btn-secondary" data-backup-action="cancel-pending">${ctx.escapeHtml(
                 t('backup.cancelPending')
               )}</button>
             </div>`
          : ''
      }
      ${
        items
          ? `<div class="backup-list">${items}</div>`
          : `<div class="empty-state"><div>${ctx.escapeHtml(
              t('backup.empty')
            )}</div><div class="form-help">${ctx.escapeHtml(t('backup.emptyHint'))}</div></div>`
      }
    </div>`;

  const refresh = () => renderBackupPane();
  root.querySelector('#btn-backup-refresh')?.addEventListener('click', refresh);
  root.querySelector('#btn-backup-create')?.addEventListener('click', async (event) => {
    const button = event.currentTarget;
    button.disabled = true;
    try {
      const created = await ctx.Api.createBackup({ include_logs: false });
      ctx.showToast(t('backup.created', { name: created.name }), 'success');
      await refresh();
    } catch (error) {
      ctx.showToast(`${t('backup.createFailed')}: ${localizeBackendError(error.message)}`, 'error');
    } finally {
      button.disabled = false;
    }
  });

  root.querySelectorAll('[data-backup-action]').forEach((button) => {
    button.addEventListener('click', async () => {
      const action = button.dataset.backupAction;
      const name = button.dataset.backupName || '';
      if (action === 'cancel-pending') {
        try {
          await ctx.Api.cancelPendingRestore();
          ctx.showToast(t('backup.pendingCancelled'), 'success');
          await refresh();
        } catch (error) {
          ctx.showToast(`${t('backup.pendingCancelled')}: ${localizeBackendError(error.message)}`, 'error');
        }
        return;
      }
      if (action === 'verify') {
        try {
          const summary = await ctx.Api.inspectBackup(name);
          ctx.showToast(
            t('backup.verified', {
              files: summary.files,
              size: ctx.formatBytes(summary.bytes),
            }),
            'success'
          );
        } catch (error) {
          ctx.showToast(`${t('backup.invalid')}: ${localizeBackendError(error.message)}`, 'error');
        }
        return;
      }
      if (action === 'delete') {
        if (!(await askBackup(t('backup.deleteConfirm', { name }), t('backup.delete')))) return;
        try {
          await ctx.Api.deleteBackup(name);
          ctx.showToast(t('backup.deleted'), 'success');
          await refresh();
        } catch (error) {
          ctx.showToast(`${t('backup.delete')}: ${localizeBackendError(error.message)}`, 'error');
        }
        return;
      }
      if (action === 'restore') {
        if (!(await askBackup(t('backup.restoreConfirm', { name }), t('backup.restore')))) return;
        try {
          const result = await ctx.Api.restoreBackup(name);
          ctx.showToast(t('backup.restoreStaged'), 'success');
          await refresh();
          if (result.restart_required) {
            const quitNow = await askBackup(
              t('backup.restartPrompt'),
              t('backup.restoreStaged')
            );
            if (quitNow && window.__TAURI__) await tauriInvoke('quit_app');
          }
        } catch (error) {
          ctx.showToast(`${t('backup.restoreFailed')}: ${localizeBackendError(error.message)}`, 'error');
        }
      }
    });
  });
}

// 高级分类：重启说明（原来是弹窗套弹窗，现在直接铺在面板里）
function renderAdvancedPane() {
  root.innerHTML = `
    <div class="form-help mb-2">${ctx.escapeHtml(t('settings.restartHint'))}</div>
    <div class="card-title mb-2">${ctx.escapeHtml(t('settings.restartTitle'))}</div>
    <ul class="settings-restart-list">
      ${[1, 2, 3, 4, 5, 6].map((i) => `<li>${ctx.escapeHtml(t(`settings.restartItem${i}`))}</li>`).join('')}
    </ul>
    <p mt-3>${ctx.escapeHtml(t('settings.restartFooter'))}</p>
  `;
}

function renderPane() {
  if (!root || !draft) return;
  if (activeGroup === 'advanced') {
    renderAdvancedPane();
    return;
  }
  if (activeGroup === 'backup') {
    renderBackupPane();
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
      key: 'launch_guardrail',
      label: t('settings.guardrail'),
      type: 'select',
      options: [
        ['off', t('settings.guardrailOff')],
        ['warn', t('settings.guardrailWarn')],
        ['strict', t('settings.guardrailStrict')],
      ],
      help: t('settings.guardrailHelp'),
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
    {
      key: 'close_to_tray',
      label: t('settings.closeToTray'),
      type: 'checkbox',
      help: t('settings.closeToTrayHelp'),
    },
    {
      key: 'chat_reasoning',
      label: t('settings.chatReasoning'),
      type: 'select',
      options: [
        ['auto', t('settings.chatReasoningAuto')],
        ['expand', t('settings.chatReasoningExpand')],
        ['collapse', t('settings.chatReasoningCollapse')],
      ],
      help: t('settings.chatReasoningHelp'),
    },
  ];
  root.innerHTML = fields
    .filter((f) => FIELD_GROUP_OF[f.key] === activeGroup)
    .map((f) => {
      const fieldId = `settings-${f.key}`;
      const grouped = f.type === 'locale' || f.type === 'theme' || f.type === 'checkbox';
      const labelOpen = grouped
        ? `<div class="param-field-label" id="${fieldId}-label">`
        : `<label class="param-field-label" for="${fieldId}">`;
      const labelClose = grouped ? '</div>' : '</label>';
      return `
      <div class="param-field">
        ${labelOpen}<span>${ctx.escapeHtml(f.label)}</span>${labelClose}
        <div class="param-field-control">${controlFor(f, draft[f.key], fieldId)}</div>
        <div class="param-field-help" id="${fieldId}-help">${ctx.escapeHtml(f.help || '')}</div>
      </div>`;
    })
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
  captureGroupBaseline();
}

function controlFor(f, value, fieldId) {
  const v = value === null || value === undefined ? '' : value;
  const ph = f.placeholder ? `placeholder="${ctx.escapeHtml(f.placeholder)}"` : '';
  const describedBy = f.help ? ` aria-describedby="${fieldId}-help"` : '';
  if (f.type === 'locale') {
    const current = getLocale();
    return `<div class="settings-radios" role="radiogroup" aria-labelledby="${fieldId}-label">${Object.entries(
      LOCALE_NAMES
    )
      .map(
        ([code, name]) => `
      <label class="settings-radio">
        <input type="radio" name="locale" value="${ctx.escapeHtml(code)}" ${code === current ? 'checked' : ''}/> ${ctx.escapeHtml(name)}
      </label>`
      )
      .join('')}</div>`;
  }
  if (f.type === 'select') {
    return `<select class="select" id="${fieldId}" data-field="${f.key}"${describedBy}>${(
      f.options || []
    )
      .map(
        ([value, label]) =>
          `<option value="${ctx.escapeHtml(String(value))}" ${String(v) === String(value) ? 'selected' : ''}>${ctx.escapeHtml(
            String(label)
          )}</option>`
      )
      .join('')}</select>`;
  }
  if (f.type === 'theme') {
    const pref =
      typeof ctx.getThemePreference === 'function'
        ? ctx.getThemePreference()
        : v === 'light'
          ? 'light'
          : 'dark';
    return `<div class="settings-radios" role="radiogroup" aria-labelledby="${fieldId}-label">
      <label class="settings-radio">
        <input type="radio" name="theme" value="system" ${pref === 'system' ? 'checked' : ''}/> ${ctx.escapeHtml(t('settings.themeSystem'))}
      </label>
      <label class="settings-radio">
        <input type="radio" name="theme" value="light" ${pref === 'light' ? 'checked' : ''}/> ${ctx.escapeHtml(t('settings.themeLight'))}
      </label>
      <label class="settings-radio">
        <input type="radio" name="theme" value="dark" ${pref === 'dark' ? 'checked' : ''}/> ${ctx.escapeHtml(t('settings.themeDark'))}
      </label>
    </div>`;
  }
  if (f.type === 'readonly') {
    // 后端硬性要求 launcher_host == 127.0.0.1（backend/config.py 的校验，
    // 且 test_regressions 专门守护），改任何值保存必 400 —— 所以只读展示。
    return `<input type="text" class="input" id="${fieldId}" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}"${describedBy} readonly/>`;
  }
  if (f.type === 'file' || f.type === 'dir') {
    return `<input type="text" class="input" id="${fieldId}" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}${describedBy}/><button class="btn btn-secondary btn-sm" data-browse="${f.key}" data-browse-type="${f.type}" aria-label="${ctx.escapeHtml(
      `${t('params.browse')} ${f.label}`
    )}">${ctx.escapeHtml(t('params.browse'))}</button>`;
  }
  if (f.type === 'checkbox') {
    return `<label class="switch"><input type="checkbox" id="${fieldId}" data-field="${f.key}" aria-labelledby="${fieldId}-label"${describedBy} ${v ? 'checked' : ''}/><span class="switch-slider"></span></label>`;
  }
  if (f.type === 'number') {
    return `<input type="number" class="input" id="${fieldId}" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}${describedBy}/>`;
  }
  return `<input type="text" class="input" id="${fieldId}" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${ph}${describedBy}/>`;
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
    // 全局路径为空**不等于**没配置：启动接受 `server_path or cfg[...]`，所以注册过
    // 运行环境的人完全能正常用。旧代码在这里直接 return，拦掉的是整个保存动作，
    // 于是他的端口、主题、托盘开关全都存不下去。只有"既没填路径、也没有任何已注册
    // 构建"才真的该提示。
    let runtimes = [];
    try {
      const data = await ctx.Api.listRuntimes();
      runtimes = (data && data.runtimes) || [];
    } catch (error) {
      // 查不到就当没配置：宁可多问一句，也不要放行一个真的跑不起来的状态
      console.warn('listRuntimes failed:', error);
    }
    if (!hasUsableServer({ serverPath: updates.llama_server_path, runtimes })) {
      ctx.showToast(t('settings.serverPathRequired'), 'warning');
      return;
    }
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
    if (window.__TAURI__ && Object.prototype.hasOwnProperty.call(updates, 'close_to_tray')) {
      try {
        const effective = await tauriInvoke('set_close_to_tray', {
          enabled: Boolean(updates.close_to_tray),
        });
        if (updates.close_to_tray && !effective) {
          ctx.showToast(t('settings.trayUnavailable'), 'warning');
        }
      } catch (error) {
        // 托盘不可用时外壳返回 false；配置仍保存，重启后行为以托盘可用性为准。
        console.warn('set_close_to_tray failed:', error);
      }
    }
    config = { ...config, ...updates };
    ctx.showToast(t('settings.saved'), 'success');
    // 主题立即应用
    if (updates.theme) ctx.setTheme(updates.theme);
    // 保存成功即关闭浮层（对齐 LM Studio 的行为）
    allowClose = true;
    ctx.closeModal();
  } catch (e) {
    ctx.showToast(`${t('settings.saveFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}
