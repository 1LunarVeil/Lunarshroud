// 二级左栏（上下文面板）的共享视图片段。
//
// 设计约束：
//   - 只做「渲染 + 回调」，**不持有任何状态**。状态永远留在各自页面模块里，
//     这样面板与页面正文读的是同一份数据 —— P0 那个「前后端各持一份字典」
//     的病根就是两份数据源，面板绝不能重蹈覆辙。
//   - 点击回调只回传 key，由页面自己决定选中语义（日志是切 WS、预设是换详情）。
//   - `raw` 字段用于把原始对象透传给调用方，避免调用方再去数组里 find 一遍。

import { t } from './i18n.mjs';

/**
 * 通用可选中列表。
 * @param {HTMLElement} el 面板容器
 * @param {object} ctx appCtx（提供 escapeHtml）
 * @param {{
 *   items: Array<{key: string, label: string, meta?: string, badge?: string, raw?: any}>,
 *   activeKey?: string|null,
 *   onSelect?: (key: string, item: object) => void,
 *   emptyText?: string,
 * }} opts
 *
 * item 可带：
 *   group / groupLabel  可选分组键/显示名；相邻且键变化时插入分组标题
 *   badge     原始 HTML，渲染在**标签行**右侧（适合"运行中"这类短徽标）
 *   meta      纯文本，会转义
 *   metaHtml  原始 HTML，渲染在**元信息行**（需要放徽标时用它 ——
 *             标签行被长文件名挤压后，徽标在那里会折成好几行）
 */
export function renderList(el, ctx, opts) {
  const { items = [], activeKey = null, onSelect, emptyText = '' } = opts;

  if (!items.length) {
    el.innerHTML = `<div class="panel-empty">${ctx.escapeHtml(emptyText)}</div>`;
    return;
  }

  let renderedGroup = null;
  el.innerHTML = `<div class="panel-list">${items
    .map((it) => {
      const meta = it.metaHtml
        ? it.metaHtml
        : it.meta
          ? `<span class="panel-list-meta-text">${ctx.escapeHtml(it.meta)}</span>`
          : '';
      const active = String(it.key) === String(activeKey);
      let header = '';
      if (it.group !== undefined && it.group !== null) {
        const groupKey = String(it.group);
        if (groupKey !== renderedGroup) {
          renderedGroup = groupKey;
          header = `<div class="panel-list-group">${ctx.escapeHtml(
            it.groupLabel || groupKey
          )}</div>`;
        }
      }
      return `${header}
    <button type="button" class="panel-list-item ${active ? 'active' : ''}" data-panel-key="${ctx.escapeHtml(
        String(it.key)
      )}"${active ? ' aria-current="true"' : ''}>
      <span class="panel-list-row">
        <span class="panel-list-label">${ctx.escapeHtml(it.label)}</span>
        ${it.badge || ''}
      </span>
      ${meta ? `<span class="panel-list-meta">${meta}</span>` : ''}
    </button>`;
    })
    .join('')}</div>`;

  if (typeof onSelect !== 'function') return;
  el.querySelectorAll('[data-panel-key]').forEach((node) => {
    node.addEventListener('click', () => {
      const key = node.dataset.panelKey;
      onSelect(key, items.find((x) => String(x.key) === key));
    });
  });
}

// 实例排序：运行中在前，已停止在后
const STATUS_ORDER = { running: 0, starting: 1, stopping: 2, failed: 3, stopped: 4 };

// 统一空态的细线图标（24 viewBox，stroke 用 currentColor → CSS 里取 tertiary）
const EMPTY_ICONS = {
  clock: '<circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/>',
  cpu: '<rect x="4" y="4" width="16" height="16" rx="2"/><rect x="9" y="9" width="6" height="6"/><line x1="9" y1="1" x2="9" y2="4"/><line x1="15" y1="1" x2="15" y2="4"/><line x1="9" y1="20" x2="9" y2="23"/><line x1="15" y1="20" x2="15" y2="23"/><line x1="20" y1="9" x2="23" y2="9"/><line x1="20" y1="14" x2="23" y2="14"/><line x1="1" y1="9" x2="4" y2="9"/><line x1="1" y1="14" x2="4" y2="14"/>',
  file: '<path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><polyline points="14 2 14 8 20 8"/><line x1="16" y1="13" x2="8" y2="13"/><line x1="16" y1="17" x2="8" y2="17"/>',
  layers: '<polygon points="12 2 2 7 12 12 22 7 12 2"/><polyline points="2 17 12 22 22 17"/><polyline points="2 12 12 17 22 12"/>',
  bookmark: '<path d="M19 21l-7-5-7 5V5a2 2 0 0 1 2-2h10a2 2 0 0 1 2 2z"/>',
};

/**
 * 统一空态（居中细线图标 + 主文案 + 引导副文案）。
 * 纯函数：只产 HTML，不持状态 —— 与本模块其余导出同一纪律。
 * @param {object} ctx appCtx（提供 escapeHtml）
 * @param {{icon: 'clock'|'cpu'|'file'|'layers'|'bookmark', title: string, sub?: string}} opts
 */
export function emptyStateHtml(ctx, opts) {
  const paths = EMPTY_ICONS[opts.icon] || EMPTY_ICONS.file;
  return `
    <div class="empty-state">
      <svg class="empty-icon-svg" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${paths}</svg>
      <div class="empty-title">${ctx.escapeHtml(opts.title)}</div>
      ${opts.sub ? `<div class="empty-sub">${ctx.escapeHtml(opts.sub)}</div>` : ''}
    </div>
  `;
}

/**
 * 实例列表（日志页 / 监控页 / 聊天页共用）。
 * @param {HTMLElement} el
 * @param {object} ctx
 * @param {{
 *   instances: Array,
 *   activeId?: string|null,
 *   onSelect?: (id: string, instance: object) => void,
 *   emptyText?: string,
 *   onlyRunning?: boolean,
 * }} opts
 */
export function renderInstanceList(el, ctx, opts) {
  const { instances = [], activeId = null, onSelect, onlyRunning = false } = opts;
  let list = Array.isArray(instances) ? instances : [];
  if (onlyRunning) list = list.filter((i) => i.status === 'running');
  const sorted = [...list].sort((a, b) => (STATUS_ORDER[a.status] ?? 9) - (STATUS_ORDER[b.status] ?? 9));

  renderList(el, ctx, {
    items: sorted.map((i) => ({
      key: String(i.id),
      label: i.name || ctx.basename(i.model_path) || i.id,
      badge: ctx.statusBadge(i.status),
      meta: `${t('dash.port')} ${i.port || '-'} · PID ${i.pid || '-'}`,
      raw: i,
    })),
    activeKey: activeId,
    onSelect: (key, item) => {
      if (typeof onSelect === 'function') onSelect(key, item && item.raw);
    },
    emptyText: opts.emptyText || t('panel.noInstances'),
  });
}

/**
 * 只在这两处签名变化时才真的重绘面板。
 * WS 会持续推送实例状态，无条件重绘会把用户的面板滚动位置顶回去。
 */
export function instanceSignature(instances, activeId) {
  const list = Array.isArray(instances) ? instances : [];
  const body = list
    .map((i) => `${i.id}:${i.status}:${i.port}:${i.pid}`)
    .sort()
    .join('|');
  return `${body}#${activeId || ''}`;
}
