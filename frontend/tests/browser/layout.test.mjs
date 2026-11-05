// Real-rendering layout regression: the chat inspector must never overflow the
// content column, at any window width or panel width.
//
// 2026-11-03 defect: at 900px with a 720px panel the inspector's right edge sat
// at 1052px while the container ended at 900px — the overflow was clipped, which
// looked exactly like "the panel is covering the conversation". Measured with
// real getBoundingClientRect, not inferred from CSS.
//
// Requires a running dev backend:
//   LUNARSHROUD_TEST_URL=http://127.0.0.1:17591 node --test frontend/tests/browser/layout.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright-core';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17520';
const WIDTHS = [1600, 1440, 1280, 1100, 900, 700];

const MEASURE = () => {
  const inspector = document.querySelector('.chat-inspector');
  const column = document.querySelector('.chat-column');
  const main = document.querySelector('.main-content');
  const rect = (el) => {
    if (!el) return null;
    const r = el.getBoundingClientRect();
    return { left: r.left, right: r.right, width: r.width };
  };
  return {
    viewport: window.innerWidth,
    inspector: rect(inspector),
    column: rect(column),
    main: rect(main),
    scrollWidth: document.documentElement.scrollWidth,
    clientWidth: document.documentElement.clientWidth,
  };
};

async function openChat(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.sidebar-item');
  // 窄窗口下侧栏是抽屉，导航项在视口外；直接派发 click 不要求可见。
  await page.evaluate(() => {
    document.querySelector('.sidebar-item[data-page="chat"]')?.click();
  });
  await page.waitForTimeout(800);
}

test('the chat inspector never overflows the content column', async () => {
  const browser = await chromium.launch({
    channel: process.env.LUNARSHROUD_BROWSER_CHANNEL || 'msedge',
    headless: true,
  });
  try {
    for (const width of WIDTHS) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await context.newPage();
      await openChat(page);

      // 默认宽度
      let m = await page.evaluate(MEASURE);
      assert.ok(m.inspector, `${width}px: inspector missing`);
      assert.ok(
        m.inspector.right <= m.main.right + 1,
        `${width}px: inspector right ${m.inspector.right} exceeds main right ${m.main.right}`
      );
      assert.ok(
        m.column.right <= m.inspector.left + 1,
        `${width}px: column right ${m.column.right} overlaps inspector left ${m.inspector.left}`
      );

      // 把面板拖到远超窗口宽度的 720px：必须收缩，不能溢出。
      await page.evaluate(() => {
        const inspector = document.querySelector('.chat-inspector');
        if (inspector) {
          inspector.style.width = '720px';
          inspector.style.flexBasis = '720px';
        }
      });
      await page.waitForTimeout(200);
      m = await page.evaluate(MEASURE);
      assert.ok(
        m.inspector.right <= m.main.right + 1,
        `${width}px @720: inspector right ${m.inspector.right} exceeds main right ${m.main.right} ` +
          `(the panel must shrink, not overflow)`
      );
      assert.ok(
        m.column.width >= 300,
        `${width}px @720: conversation column crushed to ${m.column.width}px (min 320 expected)`
      );
      assert.equal(
        m.scrollWidth,
        m.clientWidth,
        `${width}px @720: horizontal scrollbar appeared (${m.scrollWidth} > ${m.clientWidth})`
      );

      await context.close();
    }
  } finally {
    await browser.close();
  }
});
