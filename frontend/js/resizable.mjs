// 侧栏宽度可拖拽 + 持久化。
//
// 为什么必须做：两个宽度原本是写死的像素值，用户没法按自己的屏幕和习惯
// 调整 —— 而同一产品的实例卡片是响应式的，两套行为并存。
// 这里补上拖拽与记忆，且**把约束集中在这一处**，避免两处各写一遍。
//
// 1.0 第二轮的语义变化：
//   - `SIDEBAR_LIMITS` → 左侧单列导航栏（导航项 + 当前页列表）。
//     列表现在长在侧栏里，所以下限比旧版高：200px 以下列表项标题会被截得
//     只剩几个字，那种宽度不如让用户直接收起整列。
//   - `PANEL_LIMITS` → Chat 右侧参数面板（实例/知识库/系统提示/采样/工具）。
//     它承载的是表单控件，比旧版"仅列表"的面板更宽。
//
// 数值与 tokens.css 里的 --sidebar-width-min/max、--panel-width-min/max 一致；
// backend/config.py 的 CHAT_*_RANGE 也必须同步（后端是最终守门人）。

/** 宽度夹取范围。下限保证文字可读，上限防止侧栏吃掉正文。 */
export const SIDEBAR_LIMITS = { min: 200, max: 420, default: 264 };
export const PANEL_LIMITS = { min: 260, max: 420, default: 288 };

/**
 * 夹取一个宽度到合法区间。
 *
 * 两种坏输入要区别对待：
 * - **非数值**（`null`、`''`、`'abc'`、`NaN`、非数字对象）：配置读坏了，
 *   回落 default 而不是 min —— 压到最窄看起来像界面坏了。
 * - **数值但越界**（负数、超上限）：拖拽会真的产生这种值，应夹到边界。
 *   把负数当"坏配置"回落 default 会让用户向右拖到底时侧栏突然弹回默认宽度。
 */
export function clampWidth(value, limits = SIDEBAR_LIMITS) {
  const { min, max, default: fallback } = limits;
  // `Number(null)` 和 `Number('')` 都是 0 —— 如果不在这里拦住，它们会被当成
  // "越界的数值"夹到 min，而不是当作"没配置"回落默认值。
  if (value === null || value === undefined || value === '') return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.min(max, Math.max(min, Math.round(n)));
}

/**
 * 从配置对象里读两个侧栏宽度，非法值各自回落。
 *
 * `0` 是后端的"未设置"哨兵（见 config.DEFAULT_CONFIG），必须**在夹取之前**
 * 拦掉：夹取会把 0 当成"越界的数值"夹到下界，那不是"用默认值"。
 *
 * @param {object|null} config 后端 /api/system/config 的返回
 * @returns {{sidebar: number, panel: number}}
 */
export function readSidebarWidths(config) {
  const pick = (value, limits) => {
    // 未设置（0）或非正数 → 默认。0 在配置里表示"没设过"，不是"要 0 像素"。
    if (value === null || value === undefined || value === '') return limits.default;
    const n = Number(value);
    if (!Number.isFinite(n) || n <= 0) return limits.default;
    return clampWidth(n, limits);
  };
  return {
    sidebar: pick(config && config.chat_sidebar_width, SIDEBAR_LIMITS),
    panel: pick(config && config.chat_panel_width, PANEL_LIMITS),
  };
}

/**
 * 拖拽过程中的新宽度：`起点宽 + (当前指针 - 起点指针)`，再夹取。
 *
 * 抽成纯函数是为了能测边界（拖到 0、拖到屏幕外、反向拖），
 * 这些在浏览器里靠人手很难稳定复现。
 *
 * `invert` 用于**右侧面板**：右侧分隔条往左拖才是变宽，所以位移取反。
 * 左侧栏（导航）与右侧面板共用同一个函数，靠这个开关区分方向 ——
 * 分成两份实现的话，"夹取逻辑"会变成两处各写一遍。
 */
export function widthFromDrag({
  startWidth,
  startX,
  currentX,
  limits = SIDEBAR_LIMITS,
  invert = false,
}) {
  const delta = Number(currentX) - Number(startX);
  if (!Number.isFinite(delta)) return clampWidth(startWidth, limits);
  const signed = invert ? -delta : delta;
  return clampWidth(Number(startWidth) + signed, limits);
}

/**
 * 给一个元素接上拖拽手柄。
 *
 * 用 pointer 事件而不是 mouse：pointer 在触屏/触控板下同样有效，且天然
 * 支持 pointer capture（拖出元素外仍持续接收移动，松手不会"粘住"）。
 *
 * @param {object} options
 * @param {HTMLElement} options.handle   分隔条元素
 * @param {HTMLElement} options.target   被调整的侧栏
 * @param {number} options.startWidth    当前宽度
 * @param {object} options.limits        SIDEBAR_LIMITS / PANEL_LIMITS
 * @param {(width:number)=>void} options.onResize  拖拽中回调（更新 DOM）
 * @param {(width:number)=>void} [options.onCommit] 松手回调（写配置）
 * @param {string} [options.cssVar]      要设置的 CSS 变量名；不给则改 width
 * @returns {() => void} 解绑函数
 */
export function attachResizer({
  handle,
  target,
  startWidth,
  limits,
  onResize,
  onCommit,
  cssVar,
  invert = false,
}) {
  if (!handle || !target) return () => {};
  let dragging = false;
  let originX = 0;
  let originWidth = 0;
  let latest = startWidth;

  const apply = (width) => {
    latest = width;
    if (cssVar) {
      target.style.setProperty(cssVar, `${width}px`);
    } else {
      target.style.width = `${width}px`;
      // flex 子项的实际宽度由 flex-basis 决定，光设 width 不够。
      // 实测：`.chat-inspector` 的 `flex-basis` 停在旧值时，拖拽会让 inline
      // width 变化、aria-valuenow 也变、PUT 也发出去了，但**渲染宽度纹丝不动**
      // —— 因为 flex 布局根本没看 width。两者必须一起写。
      target.style.flexBasis = `${width}px`;
    }
    // 可聚焦的 role="separator" 是 ARIA window splitter：读屏软件靠
    // aria-valuenow 播报"当前宽度"。只在 index.html 里写一个静态初始值是不够的
    // —— 用户拖完/按方向键后，播报会一直停在旧值上，等于给盲用户错误信息。
    handle.setAttribute('aria-valuenow', String(width));
    if (onResize) onResize(width);
  };

  const onMove = (event) => {
    if (!dragging) return;
    // 指针可能拖到窗口外；夹取在 widthFromDrag 里，这里不额外判断。
    const width = widthFromDrag({
      startWidth: originWidth,
      startX: originX,
      currentX: event.clientX,
      limits,
      invert,
    });
    apply(width);
    event.preventDefault();
  };

  const end = () => {
    if (!dragging) return;
    dragging = false;
    handle.classList.remove('is-dragging');
    document.body.classList.remove('is-resizing-sidebar');
    window.removeEventListener('pointermove', onMove);
    window.removeEventListener('pointerup', end);
    window.removeEventListener('pointercancel', end);
    if (onCommit) onCommit(latest);
  };

  const onDown = (event) => {
    if (event.button !== undefined && event.button !== 0) return; // 只响应主键
    dragging = true;
    originX = event.clientX;
    originWidth = currentWidth(target, limits, startWidth);
    handle.classList.add('is-dragging');
    document.body.classList.add('is-resizing-sidebar');
    // 拖拽时不要选中文字，否则整页会被刷蓝
    event.preventDefault();
    window.addEventListener('pointermove', onMove);
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  };

  handle.addEventListener('pointerdown', onDown);
  // 键盘可达性：左右方向键调整 16px。纯鼠标的拖拽对键盘用户完全不可用。
  handle.addEventListener('keydown', (event) => {
    const step = event.shiftKey ? 64 : 16;
    let next = null;
    if (event.key === 'ArrowLeft') next = widthFromDrag({
      startWidth: currentWidth(target, limits, latest), startX: 0, currentX: -step, limits, invert,
    });
    else if (event.key === 'ArrowRight') next = widthFromDrag({
      startWidth: currentWidth(target, limits, latest), startX: 0, currentX: step, limits, invert,
    });
    else if (event.key === 'Home') next = limits.default;
    if (next === null) return;
    event.preventDefault();
    apply(next);
    if (onCommit) onCommit(next);
  });

  return () => {
    end();
    handle.removeEventListener('pointerdown', onDown);
  };
}

/** 读元素当前实际宽度；拿不到就用兜底值。 */
function currentWidth(target, limits, fallback) {
  const measured = target.getBoundingClientRect().width;
  if (Number.isFinite(measured) && measured > 0) return clampWidth(measured, limits);
  return clampWidth(fallback, limits);
}
