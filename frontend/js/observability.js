// 可观测性：日志、资源监控、启动历史合并成一个一级入口。
// 三个子页面各自管理 WebSocket/轮询生命周期；切换 tab 时只暂停当前页。

import { t } from './i18n.mjs';
import * as logs from './logs.js?v=1';
import * as monitor from './monitor.js?v=1';
import * as history from './history.js?v=1';

export const id = 'observability';
export const title = '可观测性';

const TABS = [
  { id: 'logs', labelKey: 'nav.logs', module: logs },
  { id: 'monitor', labelKey: 'nav.monitor', module: monitor },
  { id: 'history', labelKey: 'nav.history', module: history },
];
const STORAGE_KEY = 'lunarshroud_observability_tab';

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
        <h1 class="page-title">${ctx.escapeHtml(t('observability.title'))}</h1>
        <div class="page-subtitle">${ctx.escapeHtml(t('observability.subtitle'))}</div>
      </div>
      <div class="page-actions" id="observability-actions"></div>
    </div>
    <div class="subnav" role="tablist" aria-label="${ctx.escapeHtml(t('observability.title'))}">
      ${TABS.map((tab) => `
        <button type="button" class="subnav-btn" role="tab" id="observability-tab-${tab.id}"
          data-observability-tab="${tab.id}" aria-controls="observability-panel-${tab.id}" aria-selected="false">
          ${ctx.escapeHtml(t(tab.labelKey))}
        </button>`).join('')}
    </div>
    <div class="workspace-body">
      ${TABS.map((tab) => `
        <section class="workspace-panel" id="observability-panel-${tab.id}" role="tabpanel"
          aria-labelledby="observability-tab-${tab.id}" tabindex="-1" hidden></section>`).join('')}
    </div>
  `;
  actionHost = root.querySelector('#observability-actions');
  for (const tab of TABS) {
    const section = root.querySelector(`#observability-panel-${tab.id}`);
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
    const button = event.target.closest('[data-observability-tab]');
    if (button) selectTab(button.dataset.observabilityTab);
  });
  let saved = '';
  try {
    saved = localStorage.getItem(STORAGE_KEY) || '';
  } catch {
    /* storage unavailable */
  }
  const pending = window.__pendingObservabilityTab;
  delete window.__pendingObservabilityTab;
  if (pending && TABS.some((tab) => tab.id === pending)) saved = pending;
  activeId = TABS.some((tab) => tab.id === saved) ? saved : TABS[0].id;
  showTab(activeId);
}

export function onShow() {
  TABS.find((tab) => tab.id === activeId)?.module.onShow?.();
}

export function onHide() {
  TABS.find((tab) => tab.id === activeId)?.module.onHide?.();
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
  el.innerHTML = `<div class="panel-empty">${ctx.escapeHtml(t('observability.noPanel'))}</div>`;
}

export function selectTab(tabId) {
  if (!TABS.some((tab) => tab.id === tabId) || tabId === activeId) return;
  TABS.find((tab) => tab.id === activeId)?.module.onHide?.();
  activeId = tabId;
  showTab(tabId);
  try {
    localStorage.setItem(STORAGE_KEY, tabId);
  } catch {
    /* storage unavailable */
  }
  TABS.find((tab) => tab.id === tabId)?.module.onShow?.();
  ctx.refreshPanel();
}

function showTab(tabId) {
  for (const tab of TABS) {
    const selected = tab.id === tabId;
    root.querySelector(`#observability-panel-${tab.id}`).hidden = !selected;
    const button = root.querySelector(`#observability-tab-${tab.id}`);
    button.setAttribute('aria-selected', String(selected));
    button.classList.toggle('active', selected);
  }
  actionHost.querySelectorAll('[data-tab-actions]').forEach((actions) => {
    actions.hidden = actions.dataset.tabActions !== tabId;
  });
}
