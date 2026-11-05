import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdir } from 'node:fs/promises';
import { BASE_URL, gotoApp, launchPage, runAxe, SCREENSHOT_DIR } from './helpers.mjs';

async function gotoPage(page, label) {
  await page.click(`.sidebar-item[data-page="${label}"]`);
  await page.waitForFunction((id) => document.body.dataset.activePage === id, label, { timeout: 5000 })
    .catch(async () => {
      // app.js does not expose the active page as a data attribute; fall back to
      // the sidebar's aria-current marker.
      await page.waitForSelector(`.sidebar-item[data-page="${label}"][aria-current="page"]`, { timeout: 5000 });
    });
}

test('core navigation, knowledge modal and chat controls stay error-free', async () => {
  await mkdir(SCREENSHOT_DIR, { recursive: true });
  const { browser, page, consoleErrors, failedRequests } = await launchPage();
  try {
    await gotoApp(page, consoleErrors, failedRequests);
    const sidebar = await page.locator('.sidebar-item').allTextContents();
    // 8 个导航入口：会话组（仪表盘 / Chat）、服务组（本地 API 服务）、
    // 资源组（模型工作区 / 知识库 / 可观测性）、工具组（加载模型 / 新手指南）。
    // 「设置」不在分组里 —— 它固定在侧栏底部动作区（与"收起导航"同一块），
    // 所以 `.sidebar-item` 的计数从 9 回到 8。它是普通 button 而不是页面分组项。
    assert.equal(sidebar.filter(Boolean).length, 8, `unexpected navigation: ${sidebar.join(',')}`);
    // 侧栏底部动作仍然可达（用户要能打开设置）。
    await page.waitForSelector('#btn-sidebar-settings');

    await gotoPage(page, 'workspace');
    await page.click('[data-workspace-tab="runtimes"]');
    await page.waitForSelector('#observability-tab-logs, [data-workspace-tab="presets"]');
    await page.click('[data-workspace-tab="models"]');
    // 模型库不再有"卡片 / 列表"切换：列表搬进侧栏后，正文只留详情，
    // 同一批模型在左栏与正文各列一遍是重复。这里改为断言侧栏列表出现。
    await page.waitForSelector('#model-list-body .panel-list-item, #model-list-body .empty-state');

    await gotoPage(page, 'knowledge');
    await page.click('#kb-new');
    await page.waitForSelector('#kb-new-name');
    await page.click('.modal-footer .btn-secondary');
    await page.waitForSelector('.modal-overlay', { state: 'detached' });

    await gotoPage(page, 'chat');
    await page.waitForSelector('#chat-instance-select');
    await page.waitForSelector('#chat-kb-select');

    await gotoPage(page, 'observability');
    await page.waitForSelector('[data-observability-tab="monitor"]');

    await gotoPage(page, 'dashboard');
    await page.screenshot({ path: `${SCREENSHOT_DIR}/dashboard.png`, fullPage: true });
    assert.deepEqual(consoleErrors, [], `console errors: ${consoleErrors.join('\n')}`);
  } finally {
    await browser.close();
  }
});

test('dashboard and knowledge pages meet the accessibility gate', async () => {
  const { browser, page } = await launchPage();
  const consoleErrors = [];
  const failedRequests = [];
  try {
    for (const route of ['dashboard', 'knowledge']) {
      await gotoApp(page, consoleErrors, failedRequests);
      await gotoPage(page, route);
      const results = await runAxe(page);
      const violations = results.violations.map(
        (item) => `${item.id}: ${item.nodes.length} node(s)`
      );
      assert.deepEqual(violations, [], `${route} accessibility violations: ${violations.join(', ')}`);
    }
  } finally {
    await browser.close();
  }
});

test('first render stays under the performance budget', async () => {
  const { browser, page } = await launchPage();
  try {
    // 性能预算只关心首屏绘制，不该被轮询请求的 quiescence 拖住；用
    // domcontentloaded 拿到导航时间线即可（networkidle 在慢 runner 上会拖到超时）。
    await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });
    const metrics = await page.evaluate(() => {
      const paint = performance.getEntriesByType('paint').find((entry) => entry.name === 'first-contentful-paint');
      const navigation = performance.getEntriesByType('navigation')[0];
      return {
        fcp: paint ? paint.startTime : 0,
        domContentLoaded: navigation ? navigation.domContentLoadedEventEnd : 0,
      };
    });
    assert.ok(metrics.fcp > 0 && metrics.fcp < 3000, `FCP ${metrics.fcp}ms`);
    assert.ok(metrics.domContentLoaded < 5000, `DOMContentLoaded ${metrics.domContentLoaded}ms`);
  } finally {
    await browser.close();
  }
});
