// 设置（模态浮层）
// 左侧分类导航 + 右侧单分类表单。全局配置编辑 + 保存 + 重启后端提示
//
// 为什么是浮层而不是页面：设置不是"目的地"，而是随时召唤的覆盖层。
// app.js 按 kind === 'modal' 把它渲染成侧栏动作项，点击时打开浮层、不改变当前路由。

import { tauriInvoke } from './api.js?v=1';
import {
  AVATAR_ACCEPT,
  AVATAR_ENCODE_CANDIDATES,
  AVATAR_ENCODE_QUALITIES,
  AVATAR_MAX_BYTES,
  byteLength,
  classifyAvatar,
  fitAvatarSize,
  pickEncodedAvatar,
} from './avatar.mjs';
import { t, localizeBackendError, getLocale, setLocale, LOCALE_NAMES } from './i18n.mjs';
import {
  BG_ACCEPT,
  BG_MAX_BYTES,
  BG_OPACITY_DEFAULT,
  BG_QUALITY,
  classifyBackground,
  clampBackgroundBlur,
  fitBackgroundSize,
  pickBackgroundEncoding,
} from './chat_background.mjs';
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
  // 本地 API 服务的配置放这里：侧栏那一页是"用它、看日志"的地方，
  // 而配置属于设置。两者改的是同一份 config.json 键。
  { key: 'apiserver', labelKey: 'settings.catApiServer' },
  { key: 'runtime', labelKey: 'settings.catRuntime' },
  // 硬件与开发者是**只读信息面板**（对标 LM Studio 的同名分类）：
  // 它们没有可编辑的配置键，所以不登记进 FIELD_GROUP_OF。
  { key: 'hardware', labelKey: 'settings.catHardware' },
  { key: 'developer', labelKey: 'settings.catDeveloper' },
  { key: 'storage', labelKey: 'settings.catStorage' },
  { key: 'backup', labelKey: 'settings.catBackup' },
  { key: 'advanced', labelKey: 'settings.catAdvanced' },
];

const FIELD_GROUP_OF = {
  locale: 'general',
  theme: 'general',
  close_to_tray: 'general',
  chat_reasoning: 'general',
  chat_assistant_name: 'general',
  chat_assistant_avatar: 'general',
  chat_user_name: 'general',
  chat_user_avatar: 'general',
  chat_background_image: 'general',
  chat_background_opacity: 'general',
  chat_background_blur: 'general',
  /* 侧栏/面板宽度此前**只能靠拖分隔条改**：config.json 有两个键、后端有夹取
     范围校验，但设置页从来没有对应输入框 —— 键盘用户无法设到确切的像素值，
     而产品自己的门禁（deep-paths "每个配置键都要能从 UI 改"）因此一直是红的。
     拖拽仍是主路径，这里是精确输入；0 表示"跟随默认"。 */
  chat_sidebar_width: 'general',
  chat_panel_width: 'general',
  llama_server_path: 'paths',
  default_model_dir: 'paths',
  launcher_host: 'server',
  launcher_port: 'server',
  instance_port_start: 'server',
  instance_port_end: 'server',
  /* 本地 API 服务（OpenAI 兼容网关）。五个键都有对应的 UI 控件 ——
     deep-paths 的门禁要求"每个配置键都能从 UI 改"，新增配置键必须同步
     在这里登记，否则那条门禁会列出未覆盖的键名。 */
  api_server_enabled: 'apiserver',
  api_server_port: 'apiserver',
  api_server_cors_origins: 'apiserver',
  api_server_key: 'apiserver',
  api_server_log_requests: 'apiserver',
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
/** 背景图字段：图片走上传，文本框（hidden）只是值载体。 */
const BACKGROUND_FIELD = 'chat_background_image';

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
    // 背景图字段：文本框只是显示用的，真实值在 draft 里（upload 直接写 draft）。
    // 头像不再需要特判 —— 它也用 hidden 承载真实值，走下面的通用路径。
    if (k === BACKGROUND_FIELD) {
      if (el.value === '' && classifyBackground(draft[k]).kind === 'image') return;
      draft[k] = el.value;
      return;
    }
    // 背景图同理：它的文本框只是显示用的，真实值在 draft 里。
    if (k === BACKGROUND_FIELD) {
      if (el.value === '' && classifyBackground(draft[k]).kind === 'image') return;
      draft[k] = el.value;
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
      <div class="backup-lifecycle">
        <div class="card-title">${ctx.escapeHtml(t('app.lifecycleTitle'))}</div>
        <div class="form-help">${ctx.escapeHtml(t('app.lifecycleHint'))}</div>
        <div class="backup-actions">
          <button type="button" class="btn btn-secondary btn-sm" id="btn-app-quit">${ctx.escapeHtml(
            t('app.quit')
          )}</button>
        </div>
      </div>
    </div>`;

  // 可见的退出入口。
  //
  // 2026-11-03 实测缺陷：`close_to_tray` 打开后，点窗口 X 只是隐藏到托盘，
  // 而界面上**没有任何**退出按钮 —— 唯一入口是 Ctrl+K 命令面板里的「退出应用」。
  // 用户点完 X 看到窗口消失、进程还在，结论就是"关不掉"。一个必须靠隐藏快捷键
  // 才能完成的操作，等于没有实现。
  root.querySelector('#btn-app-quit')?.addEventListener('click', async () => {
    if (!window.__TAURI__) {
      ctx.showToast(t('app.quitUnavailable'), 'warning');
      return;
    }
    const confirmed = await ctx.showConfirm(t('app.quitConfirm'));
    if (!confirmed) return;
    try {
      await tauriInvoke('quit_app');
    } catch (error) {
      ctx.showToast(`${t('app.quitFailed')}: ${String(error)}`, 'error');
    }
  });

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

/**
 * 硬件信息面板（只读）。
 *
 * 对标 LM Studio 的 Hardware 分类：用户要在这里回答"我这台机器能跑多大模型"。
 * 数据全部来自已有的 `/api/system/overview`（CPU/内存/GPU/进程），
 * 不新增采集逻辑 —— 监控页用的就是同一份快照。
 *
 * 与 `backup` / `advanced` 一样走自定义渲染：这里没有可编辑的配置键，
 * 所以**不带 `data-field`**（`collect()` 会把它们写进 draft，
 * 而 `update_config` 拒绝未知键 → 保存必 400）。
 */
async function renderHardwarePane() {
  if (!root) return;
  root.innerHTML = `<div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(
    t('settings.loading')
  )}</div>`;
  let overview = null;
  try {
    overview = await ctx.Api.getSystemOverview();
  } catch {
    overview = null;
  }
  // 面板可能在 await 期间被切走 —— 没有这层判断会把别分类的内容覆盖掉。
  if (!root || activeGroup !== 'hardware') return;

  if (!overview) {
    root.innerHTML = `<div class="kb-error">${ctx.escapeHtml(t('settings.hardwareUnavailable'))}</div>`;
    return;
  }

  const gpus = Array.isArray(overview.gpus) ? overview.gpus : [];
  const processes = Array.isArray(overview.gpu_processes) ? overview.gpu_processes : [];
  const row = (label, value, mono = false) => `
    <div class="settings-info-row">
      <span class="settings-info-key">${ctx.escapeHtml(label)}</span>
      <span class="settings-info-value${mono ? ' text-mono' : ''}">${ctx.escapeHtml(
        String(value ?? '—')
      )}</span>
    </div>`;

  root.innerHTML = `
    <div class="settings-info-card">
      <div class="settings-info-title">${ctx.escapeHtml(t('settings.hwOverview'))}</div>
      ${row(t('sidebar.chipCpu'), `${Math.round(Number(overview.cpu_percent) || 0)}%`, true)}
      ${row(
        t('settings.hwMemory'),
        `${Number(overview.memory_used_gb || 0).toFixed(1)} / ${Number(
          overview.memory_total_gb || 0
        ).toFixed(1)} GB`,
        true
      )}
    </div>

    <div class="settings-info-card">
      <div class="settings-info-title">${ctx.escapeHtml(t('settings.hwGpus'))}</div>
      ${
        gpus.length
          ? gpus
              .map(
                (gpu) => `
        <div class="settings-info-gpu">
          <div class="settings-info-gpu-name">${ctx.escapeHtml(String(gpu.name || 'GPU'))}</div>
          ${row(
            t('settings.hwVram'),
            `${(Number(gpu.memory_used_mb || 0) / 1024).toFixed(1)} / ${(
              Number(gpu.memory_total_mb || 0) / 1024
            ).toFixed(1)} GB`,
            true
          )}
          ${row(t('settings.hwUtil'), `${gpu.utilization ?? '—'}%`, true)}
          ${row(t('settings.hwTemp'), gpu.temperature != null ? `${gpu.temperature}°C` : '—', true)}
        </div>`
              )
              .join('')
          : `<div class="settings-info-empty">${ctx.escapeHtml(t('sidebar.chipNoGpu'))}</div>`
      }
    </div>

    ${
      processes.length
        ? `<div class="settings-info-card">
            <div class="settings-info-title">${ctx.escapeHtml(t('settings.hwProcesses'))}</div>
            ${processes
              .map(
                (proc) => `
              <div class="settings-info-row">
                <span class="settings-info-key">
                  <span class="text-mono">${ctx.escapeHtml(String(proc.pid ?? '?'))}</span>
                  ${ctx.escapeHtml(String(proc.name || ''))}
                </span>
                <span class="settings-info-value text-mono">${(
                  Number(proc.memory_used_mb || 0) / 1024
                ).toFixed(1)} GB${
                  proc.managed
                    ? ` <span class="tag">${ctx.escapeHtml(t('settings.hwManaged'))}</span>`
                    : ''
                }</span>
              </div>`
              )
              .join('')}
          </div>`
        : ''
    }
  `;
}

/**
 * 开发者信息面板（只读 + 一个可编辑项）。
 *
 * 对标 LM Studio 的 Developer 分类。内容分三块：版本/平台、路径、运行环境。
 * 路径来自 `/api/system/paths`（新接口），运行环境来自 `/api/runtimes`。
 *
 * 这里刻意**不**做"日志级别"之类的可写项：后端日志目前固定 INFO，
 * 加一个假开关比不加更糟（点了没反应）。要做就先把 `LOG_LEVEL` 配置键
 * 与 `configure_file_logging` 的读取路径一起加上。
 */
async function renderDeveloperPane() {
  if (!root) return;
  root.innerHTML = `<div class="empty-state"><span class="loading"></span> ${ctx.escapeHtml(
    t('settings.loading')
  )}</div>`;
  const [paths, runtimesPayload] = await Promise.all([
    ctx.Api.getSystemPaths().catch(() => null),
    ctx.Api.listRuntimes().catch(() => null),
  ]);
  if (!root || activeGroup !== 'developer') return;

  const row = (label, value, mono = true) => `
    <div class="settings-info-row">
      <span class="settings-info-key">${ctx.escapeHtml(label)}</span>
      <span class="settings-info-value${mono ? ' text-mono' : ''}">${ctx.escapeHtml(
        String(value ?? '—')
      )}</span>
    </div>`;

  const runtimes = runtimesPayload && Array.isArray(runtimesPayload.runtimes)
    ? runtimesPayload.runtimes
    : [];

  root.innerHTML = `
    <div class="settings-info-card">
      <div class="settings-info-title">${ctx.escapeHtml(t('settings.devRuntimeInfo'))}</div>
      ${row(t('settings.devVersion'), paths?.app_version || t('settings.devUnknown'))}
      ${row(t('settings.devPython'), paths?.python_version || t('settings.devUnknown'))}
      ${row(t('settings.devPlatform'), paths?.platform || t('settings.devUnknown'), false)}
      ${row(t('settings.devLauncherPort'), paths?.listening_port ?? '—')}
      ${row(t('settings.devBackendConfig'), paths?.config_file || '—')}
    </div>

    <div class="settings-info-card">
      <div class="settings-info-title">${ctx.escapeHtml(t('settings.devPaths'))}</div>
      ${row(t('settings.devDataDir'), paths?.data_dir || '—')}
      ${row(t('settings.devLogsDir'), paths?.logs_dir || '—')}
      ${row(t('settings.devBackendLog'), paths?.backend_log || '—')}
      ${row(t('settings.devInstanceLogs'), paths?.instances_log_dir || '—')}
    </div>

    <div class="settings-info-card">
      <div class="settings-info-title">${ctx.escapeHtml(t('settings.devRuntimes'))}</div>
      ${
        runtimes.length
          ? runtimes
              .map((rt) => {
                const backend = rt.backend && rt.backend.family ? rt.backend.family : 'unknown';
                const version = [rt.version, rt.build ? `b${rt.build}` : '']
                  .filter(Boolean)
                  .join(' · ');
                return `
          <div class="settings-info-gpu">
            <div class="settings-info-gpu-name">${ctx.escapeHtml(
              rt.display_name || rt.id || 'runtime'
            )}</div>
            ${row(t('settings.devRuntimeBackend'), backend.toUpperCase())}
            ${row(t('settings.devRuntimeVersion'), version || t('runtimes.unknown'))}
            ${row(
              t('settings.devRuntimeProbe'),
              rt.probe && rt.probe.probed
                ? `${rt.probe.option_count ?? 0} ${t('settings.devOptions')}`
                : t('settings.devNotProbed'),
              false
            )}
          </div>`;
              })
              .join('')
          : `<div class="settings-info-empty">${ctx.escapeHtml(t('settings.devNoRuntimes'))}</div>`
      }
    </div>
  `;
}

function renderPane() {
  if (!root || !draft) return;
  if (activeGroup === 'advanced') {
    renderAdvancedPane();
    return;
  }
  if (activeGroup === 'hardware') {
    renderHardwarePane();
    return;
  }
  if (activeGroup === 'developer') {
    renderDeveloperPane();
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
    // ---------- 本地 API 服务 ----------
    {
      key: 'api_server_enabled',
      label: t('settings.apiServerEnabled'),
      type: 'checkbox',
      help: t('settings.apiServerEnabledHelp'),
    },
    {
      key: 'api_server_port',
      label: t('settings.apiServerPort'),
      type: 'number',
      help: t('settings.apiServerPortHelp'),
      placeholder: '1234',
    },
    {
      key: 'api_server_cors_origins',
      label: t('settings.apiServerCors'),
      type: 'text',
      help: t('settings.apiServerCorsHelp'),
      placeholder: 'http://localhost:3000',
    },
    {
      key: 'api_server_key',
      // 密码型输入：值与"是否已设置"分开表达。后端不回传明文，
      // 所以这里展示空值 + 占位提示，留空即"保持不变"（见 save()）。
      label: t('settings.apiServerKey'),
      type: 'password',
      help: t('settings.apiServerKeyHelp'),
      placeholder: t('settings.apiServerKeyPh'),
    },
    {
      key: 'api_server_log_requests',
      label: t('settings.apiServerLogRequests'),
      type: 'checkbox',
      help: t('settings.apiServerLogRequestsHelp'),
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
    // 显示身份。只存短文本（名字 ≤ 32 字符、头像 ≤ 8 码位），不做文件上传：
    // CSP 是 script-src 'self'、零构建，接图片上传要同时解决大小、MIME、路径
    // 与清理，而用户真正要的是"换个图标、改个称呼"。emoji 头像足够用。
    {
      key: 'chat_assistant_name',
      label: t('settings.chatAssistantName'),
      type: 'text',
      placeholder: t('settings.chatAssistantNamePlaceholder'),
      help: t('settings.chatAssistantNameHelp'),
    },
    {
      key: 'chat_assistant_avatar',
      label: t('settings.chatAssistantAvatar'),
      type: 'avatar',
      placeholder: '🦙',
      help: t('settings.chatAvatarHelp'),
    },
    {
      key: 'chat_user_name',
      label: t('settings.chatUserName'),
      type: 'text',
      placeholder: t('settings.chatUserNamePlaceholder'),
      help: t('settings.chatUserNameHelp'),
    },
    {
      key: 'chat_user_avatar',
      label: t('settings.chatUserAvatar'),
      type: 'avatar',
      placeholder: '🙂',
      help: t('settings.chatAvatarHelp'),
    },
    // 整页背景图。三项共享一个专用控件组（上传 + 预览 + 不透明度 + 模糊），
    // 因为它们是同一次视觉决定：只给图片不给遮罩，深色主题下的正文会读不清。
    {
      key: 'chat_background_image',
      label: t('settings.chatBackground'),
      type: 'background',
      help: t('settings.chatBackgroundHelp'),
    },
    {
      key: 'chat_background_opacity',
      label: t('settings.chatBackgroundOpacity'),
      type: 'number',
      min: 0,
      max: 1,
      step: 0.05,
      help: t('settings.chatBackgroundOpacityHelp'),
    },
    {
      key: 'chat_background_blur',
      label: t('settings.chatBackgroundBlur'),
      type: 'number',
      min: 0,
      max: 40,
      step: 1,
      help: t('settings.chatBackgroundBlurHelp'),
    },
    {
      key: 'chat_sidebar_width',
      label: t('settings.chatSidebarWidth'),
      type: 'number',
      min: 0,
      max: 420,
      step: 1,
      help: t('settings.chatSidebarWidthHelp'),
    },
    {
      key: 'chat_panel_width',
      label: t('settings.chatPanelWidth'),
      type: 'number',
      min: 0,
      // 上限必须与后端 CHAT_PANEL_WIDTH_RANGE (260, 420) 一致。
      // 旧值 520 会让 421–520 的输入通过前端校验、却在保存时被后端 400
      // —— 用户看到"保存失败"却不知道是哪个字段越界。
      max: 420,
      step: 1,
      help: t('settings.chatPanelWidthHelp'),
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
  bindAvatarControls();
  bindBackgroundControls();
  captureGroupBaseline();
}

/**
 * 头像上传：选图 → 前端缩放 → 存成 data URL → 写进 draft。
 *
 * 缩放放在前端是必须的：用户截的图常是几 MB，直接进 config 会让每次
 * GET /api/system/config 都拖着一个巨大的字符串，而且会挤爆后端的
 * 64 KiB 上限（那时用户只看到一句"保存失败"，不知道为什么）。
 */
function bindAvatarControls() {
  root.querySelectorAll('[data-avatar-upload]').forEach((btn) => {
    const key = btn.dataset.avatarUpload;
    const fileInput = root.querySelector(`[data-avatar-file="${key}"]`);
    if (!fileInput) return;
    btn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      try {
        const dataUrl = await fileToAvatarDataUrl(file);
        draft[key] = dataUrl;
        const preview = root.querySelector(`[data-avatar-preview="${key}"]`);
        if (preview) {
          preview.innerHTML = `<img src="${ctx.escapeHtml(dataUrl)}" alt=""/>`;
          preview.classList.add('has-image');
        }
        // hidden 承载真实值（与背景图同一模式）。
        const hidden = root.querySelector(`input[type="hidden"][data-field="${key}"]`);
        if (hidden) hidden.value = dataUrl;
        // 清空按钮此时才有意义。
        const clearBtn = root.querySelector(`[data-avatar-clear="${key}"]`);
        if (clearBtn) clearBtn.disabled = false;
      } catch (error) {
        ctx.showToast(
          `${t('settings.chatAvatarUpload')}: ${localizeBackendError(error && error.message)}`,
          'error'
        );
      }
    });
  });
  root.querySelectorAll('[data-avatar-clear]').forEach((btn) => {
    const key = btn.dataset.avatarClear;
    btn.addEventListener('click', () => {
      draft[key] = '';
      const preview = root.querySelector(`[data-avatar-preview="${key}"]`);
      if (preview) {
        // 回到内置默认头像，而不是留一个空圆框 —— 空框看起来像加载失败。
        preview.innerHTML = defaultAvatarSvg(key.includes('user') ? 'user' : 'assistant');
        preview.classList.remove('has-image');
      }
      const hidden = root.querySelector(`input[type="hidden"][data-field="${key}"]`);
      if (hidden) hidden.value = '';
      btn.disabled = true;
    });
  });
}

/**
 * 内置默认头像（内联 SVG，随主题着色）。
 *
 * 为什么不用图片文件：默认头像只有两个（助手/用户），做成 SVG 字符串既可以
 * 跟随 `currentColor` 换主题，又不引额外的资源请求（CSP 是 `img-src 'self'`，
 * data URL 虽然允许，但内联更省事且不会与"用户上传的图"混淆）。
 */
function defaultAvatarSvg(kind) {
  const head = '<circle cx="12" cy="9" r="3.4"/>';
  const body = '<path d="M5.2 19.2a6.8 6.8 0 0 1 13.6 0"/>';
  const glyph =
    kind === 'user'
      ? head + body
      : // 助手：加一个小"星火"点，与用户的纯人形区分开
        head + body + '<circle cx="19" cy="6" r="1.5"/>';
  return `<svg viewBox="0 0 24 24" width="24" height="24" fill="none"
    stroke="currentColor" stroke-width="1.6" stroke-linecap="round"
    stroke-linejoin="round" aria-hidden="true">${glyph}</svg>`;
}

/** 读文件 → 等比缩放到 ≤128 → 逐级降质编码 → 返回 data URL。 */
async function fileToAvatarDataUrl(file) {
  if (!file.type || !file.type.startsWith('image/')) {
    throw new Error(t('settings.chatAvatarNotImage'));
  }
  const bitmap = await loadImage(file);
  const size = fitAvatarSize(bitmap.width, bitmap.height);
  const canvas = document.createElement('canvas');
  canvas.width = size.width;
  canvas.height = size.height;
  const g = canvas.getContext('2d');
  if (!g) throw new Error(t('settings.chatAvatarEncodeFailed'));
  g.drawImage(bitmap, 0, 0, size.width, size.height);

  const attempts = [];
  for (const type of AVATAR_ENCODE_CANDIDATES) {
    for (const quality of AVATAR_ENCODE_QUALITIES) {
      const dataUrl = canvas.toDataURL(type, quality);
      attempts.push({ type, quality, dataUrl });
      if (byteLength(dataUrl) <= AVATAR_MAX_BYTES) break; // 这一种格式够小了
    }
  }
  const picked = pickEncodedAvatar(attempts);
  if (!picked.ok) throw new Error(t('settings.chatAvatarEncodeFailed'));
  return picked.dataUrl;
}

function loadImage(file) {
  return new Promise((resolve, reject) => {
    const url = URL.createObjectURL(file);
    const img = new Image();
    img.onload = () => {
      URL.revokeObjectURL(url);
      resolve(img);
    };
    img.onerror = () => {
      URL.revokeObjectURL(url);
      reject(new Error(t('settings.chatAvatarNotImage')));
    };
    img.src = url;
  });
}

/**
 * 背景图上传：选图 → 等比缩到 ≤1920×1080 → 逐级降质编码 → 写进 draft。
 *
 * 与头像走**不同的**缩放与质量参数（见 chat_background.mjs 的注释）：
 * 128×128 / 64 KiB 那套会把整页背景糊成色块。这里先试 JPEG 0.82，超预算再
 * 降质，最后 PNG 兜底 —— 顺序由 `pickBackgroundEncoding` 逐条判定。
 */
function bindBackgroundControls() {
  root.querySelectorAll('[data-bg-upload]').forEach((btn) => {
    const key = btn.dataset.bgUpload;
    const fileInput = root.querySelector(`[data-bg-file="${key}"]`);
    if (!fileInput) return;
    btn.addEventListener('click', () => fileInput.click());
    fileInput.addEventListener('change', async () => {
      const file = fileInput.files && fileInput.files[0];
      fileInput.value = '';
      if (!file) return;
      try {
        const dataUrl = await fileToBackgroundDataUrl(file);
        draft[key] = dataUrl;
        const hidden = root.querySelector(`input[type="hidden"][data-field="${key}"]`);
        if (hidden) hidden.value = dataUrl;
        const preview = root.querySelector(`[data-bg-preview="${key}"]`);
        if (preview) {
          preview.classList.add('has-image');
          preview.innerHTML = `<img src="${ctx.escapeHtml(dataUrl)}" alt=""/>`;
        }
        ctx.showToast(t('settings.chatBackgroundUploaded'), 'success', 1500);
      } catch (error) {
        ctx.showToast(
          `${t('settings.chatBackgroundUpload')}: ${localizeBackendError(
            error && error.message
          )}`,
          'error'
        );
      }
    });
  });
  root.querySelectorAll('[data-bg-clear]').forEach((btn) => {
    const key = btn.dataset.bgClear;
    btn.addEventListener('click', () => {
      draft[key] = '';
      const hidden = root.querySelector(`input[type="hidden"][data-field="${key}"]`);
      if (hidden) hidden.value = '';
      const preview = root.querySelector(`[data-bg-preview="${key}"]`);
      if (preview) {
        preview.classList.remove('has-image');
        preview.innerHTML = `<span>${ctx.escapeHtml(t('settings.chatBackgroundNone'))}</span>`;
      }
    });
  });
}

async function fileToBackgroundDataUrl(file) {
  if (!file.type || !file.type.startsWith('image/')) {
    throw new Error(t('settings.chatBackgroundNotImage'));
  }
  // SVG 可以内嵌脚本：canvas 会把它栅格化，但先把非位图格式挡在门外更直白。
  if (file.type === 'image/svg+xml') {
    throw new Error(t('settings.chatBackgroundNotImage'));
  }
  const bitmap = await loadImage(file);
  const size = fitBackgroundSize(bitmap.width, bitmap.height);
  const canvas = document.createElement('canvas');
  canvas.width = size.width;
  canvas.height = size.height;
  const g = canvas.getContext('2d');
  if (!g) throw new Error(t('settings.chatBackgroundEncodeFailed'));
  g.drawImage(bitmap, 0, 0, size.width, size.height);

  const attempts = [];
  for (const quality of [BG_QUALITY, 0.7, 0.6, 0.5]) {
    const dataUrl = canvas.toDataURL('image/jpeg', quality);
    attempts.push({ type: 'image/jpeg', quality, dataUrl });
    if (dataUrl.length <= BG_MAX_BYTES) break;
  }
  // PNG 兜底：截屏一类的图 JPEG 压缩率很差，PNG 反而更小。
  attempts.push({ type: 'image/png', quality: null, dataUrl: canvas.toDataURL('image/png') });

  const picked = pickBackgroundEncoding(attempts, BG_MAX_BYTES);
  if (!picked.ok) throw new Error(t('settings.chatBackgroundEncodeFailed'));
  return picked.dataUrl;
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
  if (f.type === 'avatar') {
    // 头像控件：**只用图片**。没有文字输入框。
    //
    // 旧实现允许填 emoji / 首字母作为回退，但那让"头像"这个概念自相矛盾：
    // 一个头像要么是一张图，要么是没设置。现在未设置 → 内置默认头像图；
    // 已设置 → 用户选的图。数据仍然存在同一个 config 键里（data URL），
    // 旧配置里的 emoji 文本按"未设置"渲染（后端仍接受短文本，见
    // config.py 的兼容分支 —— update_config 整包校验，收紧会让老用户 400）。
    const classified = classifyAvatar(v);
    const hasImage = classified.kind === 'image';
    return `<div class="avatar-field">
      <div class="avatar-preview${hasImage ? ' has-image' : ''}" data-avatar-preview="${f.key}">${
        hasImage
          ? `<img src="${ctx.escapeHtml(classified.value)}" alt=""/>`
          : defaultAvatarSvg(f.key.includes('user') ? 'user' : 'assistant')
      }</div>
      <button type="button" class="btn btn-secondary btn-sm" data-avatar-upload="${f.key}">${ctx.escapeHtml(
        t('settings.chatAvatarUpload')
      )}</button>
      <button type="button" class="btn btn-ghost btn-sm" data-avatar-clear="${f.key}"${
        hasImage ? '' : ' disabled'
      }>${ctx.escapeHtml(t('settings.chatAvatarClear'))}</button>
      <input type="file" accept="${ctx.escapeHtml(AVATAR_ACCEPT)}" data-avatar-file="${f.key}" hidden/>
      <!-- hidden 承载真实值：collect() 与背景图走同一条"只读 hidden"路径。 -->
      <input type="hidden" data-field="${f.key}" value="${ctx.escapeHtml(
        hasImage ? classified.value : ''
      )}" />
    </div>`;
  }
  if (f.type === 'background') {
    // 背景图控件：缩略预览 + 上传/清除。
    // 刻意**不**给文本输入框：data URL 有几百 KB，粘进一个 input 既没意义
    // 又会让浏览器在每次重渲染时把它整段排版一遍。
    const classified = classifyBackground(v);
    return `<div class="chat-bg-field">
      <div class="chat-bg-preview${classified.kind === 'image' ? ' has-image' : ''}" data-bg-preview="${
        f.key
      }">${
        classified.kind === 'image'
          ? `<img src="${ctx.escapeHtml(classified.value)}" alt=""/>`
          : `<span>${ctx.escapeHtml(t('settings.chatBackgroundNone'))}</span>`
      }</div>
      <div class="chat-bg-buttons">
        <button type="button" class="btn btn-secondary btn-sm" data-bg-upload="${f.key}">${ctx.escapeHtml(
          t('settings.chatBackgroundUpload')
        )}</button>
        <button type="button" class="btn btn-ghost btn-sm" data-bg-clear="${f.key}">${ctx.escapeHtml(
          t('settings.chatAvatarClear')
        )}</button>
      </div>
      <input type="file" accept="${ctx.escapeHtml(BG_ACCEPT)}" data-bg-file="${f.key}" hidden/>
      <input type="hidden" data-field="${f.key}" value="${ctx.escapeHtml(
        classified.kind === 'image' ? classified.value : ''
      )}" />
    </div>`;
  }
  if (f.type === 'password') {
    // 敏感字段（目前只有网关访问密钥）。**永远不回填真实值**：后端也不回传它，
    // 而把一个凭证明文显示在一个可以被投屏/截图的设置页里没有收益。
    // 留空 = 保持不变（由 save() 负责不把空串写回去）。
    return `<input type="password" class="input" id="${fieldId}" data-field="${f.key}"
      value="" autocomplete="new-password" ${ph}${describedBy}/>`;
  }
  if (f.type === 'number') {
    // 可选的 min/max/step：给浏览器原生约束，用户越界时就能看到反馈，
    // 而不是等到点保存才被后端 400 打回来。
    const bounds = [
      f.min !== undefined ? `min="${ctx.escapeHtml(String(f.min))}"` : '',
      f.max !== undefined ? `max="${ctx.escapeHtml(String(f.max))}"` : '',
      f.step !== undefined ? `step="${ctx.escapeHtml(String(f.step))}"` : '',
    ]
      .filter(Boolean)
      .join(' ');
    return `<input type="number" class="input" id="${fieldId}" data-field="${f.key}" value="${ctx.escapeHtml(String(v))}" ${
      bounds ? `${bounds} ` : ''
    }${ph}${describedBy}/>`;
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
  // 网关密钥：空串 = "保持不变"，而不是"清空"。设置页的密码框永远不回填真实值
  // （后端也不回传），所以只改其它字段时它必然是空的 —— 直接把空串写回去
  // 会在用户毫无察觉的情况下把密钥抹掉，而网关一旦没密钥就等于对本机全开放。
  const apiServerKeyWasBlank =
    Object.prototype.hasOwnProperty.call(updates, 'api_server_key')
    && !String(updates.api_server_key || '').trim();
  if (apiServerKeyWasBlank) delete updates.api_server_key;

  // 网关是否**真的**需要重新对齐：比较前后值，而不是"draft 里有没有这个键"。
  //
  // 2026-11-03 实测缺陷：draft 是整份配置（`{...config}`），所以
  // `api_server_enabled` 永远在 updates 里 —— 于是**保存任何无关设置**
  // （改日志行数、改主题）都会触发一次 `/api/api-server/restart`。
  // 那是"另一个端口上的独立服务"的重启，耗时可达数秒，把"设置已保存"
  // 的提示推迟到浏览器测试的断言窗口之外（实测 toast 在 6 秒后才出现）。
  // 用户视角的后果是：改个主题要等几秒才有反馈。
  const apiServerChanged =
    (Object.prototype.hasOwnProperty.call(updates, 'api_server_enabled')
      && Boolean(updates.api_server_enabled) !== Boolean(config.api_server_enabled))
    || (Object.prototype.hasOwnProperty.call(updates, 'api_server_port')
      && Number(updates.api_server_port) !== Number(config.api_server_port));

  try {
    await ctx.Api.updateSystemConfig(updates);
    // 配置已经落库 —— 成功提示**立刻**给出来，不等网关重启。
    // 旧顺序把提示放在重启之后，而重启要起/停一个 uvicorn，实测能把反馈
    // 推迟好几秒；用户改个主题却要盯着没反应的界面等。
    config = { ...config, ...updates };
    ctx.showToast(t('settings.saved'), 'success');
    // 主题立即应用
    if (updates.theme) ctx.setTheme(updates.theme);
    // 保存成功即关闭浮层（对齐 LM Studio 的行为）
    allowClose = true;
    ctx.closeModal();

    // 网关是**另一个端口上的独立服务**，改开关/端口必须让它立即重新对齐；
    // 否则用户要重启整个启动器才能看到效果。放在提示与关窗之后，失败只
    // 记一条 warning —— 配置已经保存成功，重启属于次级动作。
    if (apiServerChanged) {
      try {
        const runtime = await ctx.Api.restartApiServer();
        if (updates.api_server_enabled && runtime && !runtime.running) {
          ctx.showToast(
            `${t('settings.apiServerFailed')}: ${runtime.error || ''}`.trim(),
            'warning'
          );
        }
      } catch (error) {
        console.warn('restartApiServer failed:', error);
      }
    }
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
    // 广播一次"配置已变"：显示身份（名字/头像）、思考默认开合、侧栏宽度
    // 都是从配置读的常驻页面状态，不广播的话要么得等切页、要么得重启。
    // 用 window 事件而不是模块间直接引用：settings 与 chat 互不依赖。
    //
    // 注意顺序：成功提示与关窗已经在 `updateSystemConfig` 之后立刻做了，
    // 这里只剩"通知常驻页面"。不要再加第二次 showToast/closeModal ——
    // 那会让提示重复、也会在网关重启期间把已经关掉的浮层再关一次。
    try {
      window.dispatchEvent(
        new CustomEvent('lunarshroud:config-saved', { detail: { updates } })
      );
    } catch {
      /* 事件构造失败不影响保存结果 */
    }
  } catch (e) {
    ctx.showToast(`${t('settings.saveFailed')}: ${localizeBackendError(e.message)}`, 'error');
  }
}
