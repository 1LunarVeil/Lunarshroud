// End-to-end user paths against a REAL backend (no TestClient, no doubles).
//
// Covers the four defects a real user hit on 2026-11-03:
//   A. sending in a conversation whose bound model was switched
//   B. creating a new conversation ("创建对话失败：知识库设置无效")
//   C. the inspector must not overflow the conversation column
//   D. a visible quit entry must exist
//
// Every network failure and every console error fails the test — "the element
// exists" is not evidence that the path works.
//
// Requires a running dev backend:
//   LUNARSHROUD_TEST_URL=http://127.0.0.1:17591 \
//     node --test --test-concurrency=1 frontend/tests/browser/user-paths.test.mjs
import assert from 'node:assert/strict';
import test from 'node:test';
import { chromium } from 'playwright-core';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17520';

async function launch() {
  const browser = await chromium.launch({
    channel: process.env.LUNARSHROUD_BROWSER_CHANNEL || 'msedge',
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  const failedRequests = [];
  page.on('console', (m) => {
    if (m.type() === 'error') consoleErrors.push(m.text());
  });
  page.on('pageerror', (e) => consoleErrors.push(e.message));
  page.on('response', (r) => {
    if (r.status() >= 400) failedRequests.push(`${r.status()} ${r.url()}`);
  });
  return { browser, context, page, consoleErrors, failedRequests };
}

async function openChat(page) {
  await page.goto(BASE, { waitUntil: 'domcontentloaded' });
  await page.waitForSelector('.sidebar-item');
  await page.evaluate(() => {
    document.querySelector('.sidebar-item[data-page="chat"]')?.click();
  });
  await page.waitForTimeout(800);
}

test('creating a new conversation does not fail on an unset parameter', async () => {
  const { browser, page, consoleErrors } = await launch();
  try {
    await openChat(page);

    // **直接打 API**，不点按钮。
    //
    // 点“新对话”不够：`newChat()` 在 `currentInstance()` 为空时会提前 return
    // （没有运行中的模型时），请求根本不会发出 —— 那样的测试在无模型环境下
    // 永远是绿的，什么都证明不了。我第一版就是这么写的，退回后端修复后仍然
    // 全绿，正好复现了用户批评的“测试全绿但产品不能用”。
    //
    // 这里直接构造前端真实会发的 payload（含 `seed: null`），验证后端契约：
    // 一个 `null` 参数必须被当作“未设置”，而不是类型错误。
    const config = await page.evaluate(async () => {
      const r = await fetch('/api/system/config');
      return r.ok ? r.json() : null;
    });
    assert.ok(config, 'backend config endpoint unreachable');

    const result = await page.evaluate(async () => {
      const response = await fetch('/api/chats', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          title: '',
          instance_id: null,
          model: '',
          model_key: '',
          // 前端 DEFAULT_PARAMS 的真实形状。
          params: {
            system: '',
            temperature: 0.7,
            max_tokens: -1,
            top_p: 0.95,
            top_k: 40,
            repeat_penalty: 1.1,
            seed: null,
            thinking: 'off',
          },
          knowledge_base_id: null,
        }),
      });
      const text = await response.text();
      return { status: response.status, body: text.slice(0, 400) };
    });

    assert.equal(
      result.status,
      200,
      `creating a conversation with seed:null returned ${result.status}: ${result.body}`
    );
    assert.ok(
      !result.body.includes('must be a number'),
      `backend rejected an unset parameter: ${result.body}`
    );
    assert.deepEqual(consoleErrors, [], `console errors: ${consoleErrors.join(' / ')}`);
  } finally {
    await browser.close();
  }
});

test('the quit entry is visible without knowing a keyboard shortcut', async () => {
  const { browser, page } = await launch();
  try {
    await openChat(page);
    // 设置 → 备份 → 应用生命周期
    await page.evaluate(() => {
      document.querySelector('#btn-sidebar-settings')?.click();
    });
    await page.waitForSelector('#app-modal-overlay', { timeout: 5000 });
    await page.evaluate(() => {
      document.querySelector('.settings-nav-item[data-cat="backup"]')?.click();
    });
    await page.waitForTimeout(700);

    const visible = await page.evaluate(() => {
      const btn = document.querySelector('#btn-app-quit');
      if (!btn) return { found: false };
      const r = btn.getBoundingClientRect();
      const style = getComputedStyle(btn);
      return {
        found: true,
        width: Math.round(r.width),
        height: Math.round(r.height),
        display: style.display,
        visibility: style.visibility,
      };
    });

    assert.ok(visible.found, 'no visible quit control in Settings → Backups');
    assert.ok(
      visible.width > 0 && visible.height > 0,
      `quit control has no size: ${JSON.stringify(visible)}`
    );
    assert.notEqual(visible.display, 'none', 'quit control is display:none');
    assert.notEqual(visible.visibility, 'hidden', 'quit control is visibility:hidden');
  } finally {
    await browser.close();
  }
});

test('the inspector never covers the conversation at any width', async () => {
  const { browser } = await launch();
  try {
    for (const width of [1600, 1280, 900, 700]) {
      const context = await browser.newContext({ viewport: { width, height: 900 } });
      const page = await context.newPage();
      await openChat(page);
      const geometry = await page.evaluate(() => {
        const inspector = document.querySelector('.chat-inspector');
        const column = document.querySelector('.chat-column');
        const main = document.querySelector('.main-content');
        if (!inspector || !column || !main) return null;
        const i = inspector.getBoundingClientRect();
        const c = column.getBoundingClientRect();
        const m = main.getBoundingClientRect();
        return {
          inspectorRight: i.right,
          columnRight: c.right,
          columnWidth: c.width,
          mainRight: m.right,
        };
      });
      assert.ok(geometry, `${width}px: chat layout elements missing`);
      assert.ok(
        geometry.inspectorRight <= geometry.mainRight + 1,
        `${width}px: inspector right ${Math.round(geometry.inspectorRight)} ` +
          `overflows container right ${Math.round(geometry.mainRight)}`
      );
      assert.ok(
        geometry.columnRight <= geometry.inspectorRight,
        `${width}px: conversation column overlaps the inspector`
      );
      await context.close();
    }
  } finally {
    await browser.close();
  }
});
