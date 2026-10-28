// 单一 portal tooltip。
//
// 为什么不用 CSS ::after：参数编辑器的 .pe-pane 是滚动容器，伪元素会被
// overflow 裁剪 —— 实测最后一个 ? 提示圈的 90px tooltip 完全不可见。
// portal 固定在 body 上，按视口翻转位置，滚动容器裁不到它。

const SHOW_DELAY_MS = 220;

let tooltipEl = null;
let currentTarget = null;
let showTimer = null;
let previousDescribedBy = null;

function ensureTooltip() {
  if (tooltipEl && tooltipEl.isConnected) return tooltipEl;
  tooltipEl = document.getElementById('ui-tooltip');
  if (!tooltipEl) {
    tooltipEl = document.createElement('div');
    tooltipEl.id = 'ui-tooltip';
    tooltipEl.className = 'ui-tooltip';
    tooltipEl.setAttribute('role', 'tooltip');
    tooltipEl.hidden = true;
    document.body.appendChild(tooltipEl);
  }
  return tooltipEl;
}

function position(target) {
  const el = ensureTooltip();
  const rect = target.getBoundingClientRect();
  // 先显示再测量，才能拿到真实宽高。
  el.hidden = false;
  const box = el.getBoundingClientRect();
  const gap = 8;
  let top = rect.bottom + gap;
  if (top + box.height > window.innerHeight - 8) {
    top = rect.top - box.height - gap;
  }
  top = Math.max(8, Math.min(top, window.innerHeight - box.height - 8));
  // 右缘对齐目标右缘（与旧 CSS 一致），再夹到视口内。
  let left = rect.right - box.width;
  left = Math.max(8, Math.min(left, window.innerWidth - box.width - 8));
  el.style.top = `${Math.round(top)}px`;
  el.style.left = `${Math.round(left)}px`;
}

function show(target, immediate) {
  const text = target.getAttribute('data-tip');
  if (!text) return;
  if (currentTarget === target) return;
  hide();
  const el = ensureTooltip();
  el.textContent = text;
  el.hidden = false;
  currentTarget = target;
  previousDescribedBy = target.getAttribute('aria-describedby');
  target.setAttribute('aria-describedby', 'ui-tooltip');
  position(target);
  if (immediate) return;
  el.style.opacity = '0';
  requestAnimationFrame(() => {
    if (currentTarget === target) el.style.opacity = '1';
  });
}

function hide() {
  if (showTimer) {
    clearTimeout(showTimer);
    showTimer = null;
  }
  if (currentTarget) {
    if (previousDescribedBy === null) currentTarget.removeAttribute('aria-describedby');
    else currentTarget.setAttribute('aria-describedby', previousDescribedBy);
    currentTarget = null;
    previousDescribedBy = null;
  }
  if (tooltipEl) {
    tooltipEl.hidden = true;
    tooltipEl.style.opacity = '';
  }
}

function scheduleShow(target, immediate) {
  if (showTimer) clearTimeout(showTimer);
  if (immediate) {
    show(target, true);
    return;
  }
  showTimer = setTimeout(() => show(target, false), SHOW_DELAY_MS);
}

function targetFrom(node) {
  return node && node.closest ? node.closest('[data-tip]') : null;
}

/**
 * Install one delegated tooltip listener set on `root`.
 * Safe to call once at boot; it does not retain page state.
 */
export function initTooltips(root = document) {
  root.addEventListener('mouseover', (event) => {
    const target = targetFrom(event.target);
    if (!target || target === currentTarget) return;
    scheduleShow(target, false);
  });
  root.addEventListener('mouseout', (event) => {
    const target = targetFrom(event.target);
    if (!target) return;
    if (target.contains(event.relatedTarget)) return;
    hide();
  });
  root.addEventListener('focusin', (event) => {
    const target = targetFrom(event.target);
    if (target) scheduleShow(target, true);
  });
  root.addEventListener('focusout', (event) => {
    const target = targetFrom(event.target);
    if (target && !target.contains(event.relatedTarget)) hide();
  });
  root.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && currentTarget) {
      event.stopPropagation();
      hide();
    }
  });
  window.addEventListener('scroll', hide, true);
  window.addEventListener('resize', hide);
}
