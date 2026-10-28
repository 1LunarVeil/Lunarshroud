// Modal stack, focus trap, inert background and confirm dialogs.
// Extracted from app.js so the shell only wires pages/state.

import { t } from './i18n.mjs';
import { escapeHtml } from './html.mjs';

// 浮层栈：默认行为仍是“替换当前浮层”（兼容既有调用方）；需要叠加时传
// { stack: true }，例如启动前检查叠在加载表单上。栈顶持有 #app-modal-overlay，
// 之前的浮层变 inert + aria-hidden；关闭栈顶后焦点回到打开它的元素。
const overlayStack = [];
const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"])',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[tabindex]:not([tabindex="-1"])',
].join(',');

function visibleFocusables(rootEl) {
  return [...rootEl.querySelectorAll(FOCUSABLE_SELECTOR)].filter((el) => {
    if (el.closest('[inert]')) return false;
    const rect = el.getBoundingClientRect();
    return rect.width > 0 && rect.height > 0 && getComputedStyle(el).visibility !== 'hidden';
  });
}

function topOverlayEntry() {
  return overlayStack.length ? overlayStack[overlayStack.length - 1] : null;
}

function syncOverlayState() {
  const top = topOverlayEntry();
  for (const entry of overlayStack) {
    const isTop = entry === top;
    entry.overlay.inert = !isTop;
    entry.overlay.setAttribute('aria-hidden', isTop ? 'false' : 'true');
    if (isTop) entry.overlay.id = 'app-modal-overlay';
    else entry.overlay.removeAttribute('id');
    const dialog = entry.overlay.querySelector('.modal');
    if (dialog) {
      if (isTop) dialog.setAttribute('aria-modal', 'true');
      else dialog.removeAttribute('aria-modal');
    }
  }
  // 背景不可聚焦/不可读屏；toast 容器在 app-layout 之外，仍可被播报。
  const layout = document.querySelector('.app-layout');
  if (layout) layout.inert = overlayStack.length > 0;
  const backdrop = document.getElementById('sidebar-backdrop');
  if (backdrop) backdrop.inert = overlayStack.length > 0;
}

function closeEntry(entry) {
  const index = overlayStack.indexOf(entry);
  if (index === -1) return false;
  overlayStack.splice(index, 1);
  entry.overlay.remove();
  if (entry.closeHook) {
    try {
      entry.closeHook();
    } catch (e) {
      console.error('[modal onClose]', e);
    }
  }
  syncOverlayState();
  const target = entry.restoreFocus;
  if (target && target.isConnected && typeof target.focus === 'function') {
    target.focus({ preventScroll: true });
  }
  // 浮层销毁后，模块作用域上的防抖计时器必须有机会自清 ——
  // 否则关窗后仍会触发一次请求（param_editor 的防抖计时器条目）。
  document.dispatchEvent(new CustomEvent('modal-closed'));
  return true;
}

export function closeAllModals() {
  while (overlayStack.length) closeEntry(overlayStack[overlayStack.length - 1]);
}

export function showModal(title, contentHtml, actions = [], options = {}) {
  if (!options.stack) closeAllModals();
  const overlay = document.createElement('div');
  overlay.className = 'modal-overlay';
  const titleId = `modal-title-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`;

  const actionsHtml = actions
    .map(
      (a, i) =>
        `<button class="btn ${a.class || 'btn-secondary'}" data-idx="${i}">${escapeHtml(
          a.label
        )}</button>`
    )
    .join('');

  overlay.innerHTML = `
    <div class="modal ${modalSizeClass(actions)}" role="dialog" aria-modal="true" aria-labelledby="${titleId}">
      <div class="modal-header">
        <div class="modal-title" id="${titleId}"></div>
        <button class="modal-close" data-close aria-label="${escapeHtml(
          t('app.close')
        )}" title="${escapeHtml(t('app.close'))}">&times;</button>
      </div>
      <div class="modal-body"></div>
      <div class="modal-footer">${actionsHtml}</div>
    </div>
  `;
  overlay.querySelector('.modal-title').textContent = title;
  const body = overlay.querySelector('.modal-body');
  if (typeof contentHtml === 'string') {
    body.innerHTML = contentHtml;
  } else if (contentHtml instanceof Node) {
    body.appendChild(contentHtml);
  }

  const entry = {
    overlay,
    closeHook: typeof options.onClose === 'function' ? options.onClose : null,
    restoreFocus: document.activeElement instanceof HTMLElement ? document.activeElement : null,
  };

  // 关闭事件
  overlay.querySelector('[data-close]').addEventListener('click', () => closeEntry(entry));
  overlay.addEventListener('click', (e) => {
    if (e.target === overlay) closeEntry(entry);
  });

  // 动作按钮
  overlay.querySelectorAll('[data-idx]').forEach((btn) => {
    btn.addEventListener('click', async () => {
      const idx = parseInt(btn.dataset.idx, 10);
      const action = actions[idx];
      if (!action) return;
      // 同一动作在异步完成前不得重复触发；按钮的 disabled 仍由动作本身控制。
      if (entry.actionPending) return;
      let result;
      if (typeof action.onClick === 'function') {
        entry.actionPending = true;
        try {
          // 第三个参数是被点击的按钮：异步动作可以据此禁用自身，防止双击重复提交。
          result = await settleModalAction(action, overlay, body, btn);
        } catch (error) {
          // 动作自己的错误处理优先；这里至少不能让异常变成未处理的 rejection。
          console.error('[modal action]', error);
          return;
        } finally {
          entry.actionPending = false;
        }
      }
      if (!shouldCloseAfterAction(result, action.keepOpen)) return;
      closeEntry(entry);
    });
  });

  // 焦点陷阱：Tab/Shift+Tab 只在当前浮层内循环。
  overlay.addEventListener('keydown', (event) => {
    if (event.key !== 'Tab') return;
    const focusables = visibleFocusables(overlay);
    if (!focusables.length) {
      event.preventDefault();
      return;
    }
    const first = focusables[0];
    const last = focusables[focusables.length - 1];
    const active = document.activeElement;
    if (event.shiftKey) {
      if (active === first || !overlay.contains(active)) {
        event.preventDefault();
        last.focus({ preventScroll: true });
      }
    } else if (active === last || !overlay.contains(active)) {
      event.preventDefault();
      first.focus({ preventScroll: true });
    }
  });

  overlayStack.push(entry);
  document.body.appendChild(overlay);
  syncOverlayState();

  if (options.initialFocus !== false) {
    let target = null;
    if (typeof options.initialFocus === 'string') target = overlay.querySelector(options.initialFocus);
    else if (options.initialFocus instanceof HTMLElement) target = options.initialFocus;
    if (!target) {
      target =
        overlay.querySelector('.modal-body input, .modal-body select, .modal-body textarea') ||
        overlay.querySelector('.modal-footer .btn-primary') ||
        overlay.querySelector('.modal-footer button') ||
        visibleFocusables(overlay)[0] ||
        overlay.querySelector('.modal');
    }
    if (target) {
      if (
        !target.hasAttribute('tabindex') &&
        !/^(INPUT|SELECT|TEXTAREA|BUTTON|A)$/.test(target.tagName)
      ) {
        target.setAttribute('tabindex', '-1');
      }
      try {
        target.focus({ preventScroll: true });
      } catch {
        /* 某些只读环境不允许 focus，不影响浮层可用性 */
      }
    }
  }
  return overlay;
}

function modalSizeClass(actions) {
  if (actions.some((a) => a.size === 'lg')) return 'modal-lg';
  if (actions.some((a) => a.size === 'sm')) return 'modal-sm';
  return '';
}

export function closeModal() {
  const entry = topOverlayEntry();
  if (!entry) {
    document.dispatchEvent(new CustomEvent('modal-closed'));
    return;
  }
  closeEntry(entry);
}

/**
 * Run one action and wait for it when it is asynchronous.
 *
 * `showModal` used to treat a returned Promise as success and close immediately,
 * so an async save/validation that later returned false had already destroyed
 * the dialog (and the user's edits). Callers can keep using sync actions.
 */
export async function settleModalAction(action, ...args) {
  const result = action.onClick(...args);
  return result && typeof result.then === 'function' ? await result : result;
}

/** Whether an action result should auto-close the dialog. */
export function shouldCloseAfterAction(result, keepOpen = false) {
  return result !== false && !keepOpen;
}

export function showConfirm(message, title) {
  return new Promise((resolve) => {
    let settled = false;
    const done = (value) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    showModal(
      title || t('app.confirmTitle'),
      `<div class="diag-intro">${escapeHtml(message)}</div>`,
      [
        {
          label: t('app.cancel'),
          class: 'btn-secondary',
          onClick: () => done(false),
        },
        {
          label: t('app.ok'),
          class: 'btn-primary',
          onClick: () => done(true),
        },
      ],
      { onClose: () => done(false) }
    );
  });
}
