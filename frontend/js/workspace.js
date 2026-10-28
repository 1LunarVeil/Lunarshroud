// 模型工作区：把模型库、运行环境、预设收进同一个一级入口。
// 子页面仍保持自己的状态与生命周期；这里只负责 tab 切换、把子页面的操作按钮
// 提升到统一工具栏，以及让二级面板继续显示当前 tab 的列表。

import { t } from './i18n.mjs';
import * as models from './models.js?v=1';
import * as runtimes from './runtimes.js?v=1';
import * as presets from './presets.js?v=1';

export const id = 'workspace';
export const title = '模型工作区';

const TABS = [
  { id: 'models', labelKey: 'nav.models', module: models },
  { id: 'runtimes', labelKey: 'nav.runtimes', module: runtimes },
  { id: 'presets', labelKey: 'nav.presets', module: presets },
];
const STORAGE_KEY = 'lunarshroud_workspace_tab';

let ctx = null;
let root = null;
let actionHost = null;
let activeId = '';

export function init(el, appCtx) {
  ctx = appCtx;
  root = el;
  root.innerHTML = `
    <div class="page-header">
      <div>
        <h1 class="page-title">${ctx.escapeHtml(t('workspace.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('workspace.subtitle'))}</div>
      </div>
      <div class="page-actions" id="workspace-actions"></div>
    </div>
    <div class="subnav" role="tablist" aria-label="${ctx.escapeHtml(t('workspace.title'))}">
      ${TABS.map((tab) => `
        <button type="button" class="subnav-btn" role="tab" id="workspace-tab-${tab.id}"
          data-workspace-tab="${tab.id}" aria-controls="workspace-panel-${tab.id}" aria-selected="false">
          ${ctx.escapeHtml(t(tab.labelKey))}
        </button>`).join('')}
    </div>
    <div class="workspace-body">
      ${TABS.map((tab) => `
        <section class="workspace-panel" id="workspace-panel-${tab.id}" role="tabpanel"
          aria-labelledby="workspace-tab-${tab.id}" tabindex="-1" hidden></section>`).join('')}
    </div>
  `;
  actionHost = root.querySelector('#workspace-actions');
  for (const tab of TABS) {
    const section = root.querySelector(`#workspace-panel-${tab.id}`);
    tab.module.init(section, ctx);
    const header = section.querySelector('.page-header');
    const actions = header?.querySelector('.page-actions');
    if (actions) {
      actions.dataset.tabActions = tab.id;
      actions.hidden = true;
      actionHost.appendChild(actions);
    }
    header?.remove();
  }
  root.addEventListener('click', (event) => {
    const button = event.target.closest('[data-workspace-tab]');
    if (button) selectTab(button.dataset.workspaceTab);
  });
  let saved = '';
  try {
    saved = localStorage.getItem(STORAGE_KEY) || '';
  } catch {
    /* storage unavailable */
  }
  const pending = window.__pendingWorkspaceTab;
  delete window.__pendingWorkspaceTab;
  if (pending && TABS.some((tab) => tab.id === pending)) saved = pending;
  activeId = TABS.some((tab) => tab.id === saved) ? saved : TABS[0].id;
  showTab(activeId);
}

export function onShow() {
  const tab = TABS.find((item) => item.id === activeId);
  tab?.module.onShow?.();
}

export function onHide() {
  const tab = TABS.find((item) => item.id === activeId);
  tab?.module.onHide?.();
}

export function hasPanel() {
  return Boolean(TABS.find((tab) => tab.id === activeId)?.module.renderPanel);
}

export function renderPanel(el) {
  const tab = TABS.find((item) => item.id === activeId);
  if (tab?.module.renderPanel) {
    tab.module.renderPanel(el, ctx);
    return;
  }
  el.innerHTML = `<div class="panel-empty">${ctx.escapeHtml(t('workspace.noPanel'))}</div>`;
}

export function selectTab(tabId) {
  if (!TABS.some((tab) => tab.id === tabId) || tabId === activeId) return;
  const previous = TABS.find((tab) => tab.id === activeId);
  previous?.module.onHide?.();
  activeId = tabId;
  showTab(tabId);
  try {
    localStorage.setItem(STORAGE_KEY, tabId);
  } catch {
    /* storage unavailable */
  }
  const next = TABS.find((tab) => tab.id === tabId);
  next?.module.onShow?.();
  ctx.refreshPanel();
}

function showTab(tabId) {
  for (const tab of TABS) {
    const selected = tab.id === tabId;
    root.querySelector(`#workspace-panel-${tab.id}`).hidden = !selected;
    const button = root.querySelector(`#workspace-tab-${tab.id}`);
    button.setAttribute('aria-selected', String(selected));
    button.classList.toggle('active', selected);
  }
  actionHost.querySelectorAll('[data-tab-actions]').forEach((actions) => {
    actions.hidden = actions.dataset.tabActions !== tabId;
  });
}
