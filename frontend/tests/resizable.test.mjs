// 侧栏宽度：夹取、配置读取、拖拽换算。
//
// 这些是纯函数，所以能在没有 DOM 的环境里测。真正需要浏览器验证的部分
// （指针事件、视觉反馈）在浏览器验收里做。

import test from 'node:test';
import assert from 'node:assert/strict';

import {
  PANEL_LIMITS,
  readSidebarWidths,
  SIDEBAR_LIMITS,
  clampWidth,
  widthFromDrag,
} from '../js/resizable.mjs';

test('clampWidth keeps values inside the limits and rounds them', () => {
  assert.equal(clampWidth(220, SIDEBAR_LIMITS), 220);
  assert.equal(clampWidth(220.6, SIDEBAR_LIMITS), 221);
  assert.equal(clampWidth(999, SIDEBAR_LIMITS), SIDEBAR_LIMITS.max);
  assert.equal(clampWidth(1, SIDEBAR_LIMITS), SIDEBAR_LIMITS.min);
});

test('a non-numeric width falls back to the default, not to the minimum', () => {
  // 配置坏了应该回到默认宽度。夹到 min 会让侧栏突然缩到最窄 ——
  // 看起来像"界面坏了"，而不是"设置没读到"。
  for (const bad of [null, undefined, '', 'abc', NaN, {}]) {
    assert.equal(
      clampWidth(bad, SIDEBAR_LIMITS),
      SIDEBAR_LIMITS.default,
      `bad value ${String(bad)} must fall back to default`
    );
  }
});

test('a numeric but out-of-range width clamps to the boundary', () => {
  // 拖拽会真的产生负数/超上限的值。把它当"坏配置"回默认会让用户
  // 拖到边缘时侧栏突然弹回默认宽度 —— 那是明显的交互 bug。
  assert.equal(clampWidth(-5, SIDEBAR_LIMITS), SIDEBAR_LIMITS.min);
  assert.equal(clampWidth(0, SIDEBAR_LIMITS), SIDEBAR_LIMITS.min);
  assert.equal(clampWidth(9999, SIDEBAR_LIMITS), SIDEBAR_LIMITS.max);
});

test('readSidebarWidths reads both widths independently and clamps each', () => {
  assert.deepEqual(readSidebarWidths({ chat_sidebar_width: 300, chat_panel_width: 400 }), {
    sidebar: 300,
    panel: 400,
  });
  // 0 = 未设置 → 各自默认
  assert.deepEqual(readSidebarWidths({ chat_sidebar_width: 0, chat_panel_width: 0 }), {
    sidebar: SIDEBAR_LIMITS.default,
    panel: PANEL_LIMITS.default,
  });
  // 缺字段 / 整个配置拿不到
  assert.deepEqual(readSidebarWidths(null), {
    sidebar: SIDEBAR_LIMITS.default,
    panel: PANEL_LIMITS.default,
  });
  // 越界各自夹取，且用各自的区间（panel 的上限比 sidebar 大）
  assert.deepEqual(readSidebarWidths({ chat_sidebar_width: 9999, chat_panel_width: 9999 }), {
    sidebar: SIDEBAR_LIMITS.max,
    panel: PANEL_LIMITS.max,
  });
});

test('widthFromDrag adds the pointer delta and clamps it', () => {
  assert.equal(widthFromDrag({ startWidth: 264, startX: 100, currentX: 160 }), 324);
  assert.equal(widthFromDrag({ startWidth: 264, startX: 100, currentX: 40 }), 204);
  // 反向拖过头 → 夹到下限，不会变成负数
  assert.equal(widthFromDrag({ startWidth: 264, startX: 400, currentX: 0 }), SIDEBAR_LIMITS.min);
  // 正向拖出屏幕 → 夹到上限
  assert.equal(widthFromDrag({ startWidth: 264, startX: 0, currentX: 5000 }), SIDEBAR_LIMITS.max);
  // 非数值指针坐标不能让宽度变成 NaN
  assert.equal(widthFromDrag({ startWidth: 264, startX: NaN, currentX: 10 }), 264);
});

test('widthFromDrag with invert grows the panel when dragging left', () => {
  // 右侧面板的分隔条在面板左边：往左拖 → 面板变宽，往右拖 → 变窄。
  // 同一个函数靠 invert 区分方向，避免两处各写一遍夹取逻辑。
  assert.equal(
    widthFromDrag({ startWidth: 288, startX: 1000, currentX: 940, limits: PANEL_LIMITS, invert: true }),
    348,
    'dragging left must widen the right-hand panel'
  );
  assert.equal(
    widthFromDrag({ startWidth: 288, startX: 1000, currentX: 1060, limits: PANEL_LIMITS, invert: true }),
    228 < PANEL_LIMITS.min ? PANEL_LIMITS.min : 228,
    'dragging right must narrow it (and clamp at the minimum)'
  );
  // 两个方向都不能越界
  assert.equal(
    widthFromDrag({ startWidth: 288, startX: 1000, currentX: 0, limits: PANEL_LIMITS, invert: true }),
    PANEL_LIMITS.max
  );
  assert.equal(
    widthFromDrag({ startWidth: 288, startX: 1000, currentX: 5000, limits: PANEL_LIMITS, invert: true }),
    PANEL_LIMITS.min
  );
});

test('the panel range stays independent from the sidebar range', () => {
  // 两者的语义在 1.0 第二轮变了：sidebar 是"导航 + 当前页列表"，panel 是
  // Chat 右侧参数面板（实例/系统提示/采样/工具）。它们**仍必须是独立区间**
  // —— 共用一个会让一侧的合理宽度被另一侧卡掉。
  //
  // 这里刻意**不再**断言 panel 比 sidebar 宽：列表搬进侧栏后，侧栏往往比
  // 参数面板更宽。写死"panel 更宽"会把一个已经不成立的假设钉死。
  assert.notEqual(PANEL_LIMITS.default, SIDEBAR_LIMITS.default);
  assert.ok(PANEL_LIMITS.min < PANEL_LIMITS.max);
  assert.ok(SIDEBAR_LIMITS.min < SIDEBAR_LIMITS.max);
  for (const limits of [SIDEBAR_LIMITS, PANEL_LIMITS]) {
    assert.ok(limits.default >= limits.min && limits.default <= limits.max);
  }
});
