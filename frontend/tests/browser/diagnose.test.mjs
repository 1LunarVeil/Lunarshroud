// Diagnostic probe for the browser gate.
//
// When `core-paths` fails on CI with only "Timeout 30000ms exceeded", the log
// says nothing about *why* the app did not render: no console error, no HTTP
// status, no failed request. That happened on 2026-11-05 and cost a full
// debugging round. This file exists so the same failure explains itself.
//
// It is not part of the gate; `npm run test:browser` runs core-paths only. Run
// it deliberately when the gate fails:
//
//   node --test frontend/tests/browser/diagnose.test.mjs
//
// It prints the app's own ready signal, every console message, every failed
// request, and whether `#sidebar` exists — then fails if the app is not up.
import assert from 'node:assert/strict';
import test from 'node:test';
import { BASE_URL, launchPage } from './helpers.mjs';

test('the app boots and reports itself ready', async () => {
  const { browser, page, consoleErrors, failedRequests } = await launchPage();
  const consoleLines = [];
  page.on('console', (message) => {
    consoleLines.push(`[${message.type()}] ${message.text()}`);
  });
  try {
    // 与 helpers.gotoApp 一致：不用 networkidle —— 页面每 5 秒轮询实例，
    // 慢 runner 上"连续 500ms 无请求"可能永远不成立，而它与能否渲染无关。
    const response = await page.goto(BASE_URL, { waitUntil: 'domcontentloaded' });

    // Wait for the signal boot() logs last. If this times out, boot() threw or
    // never ran; the dump below says which.
    let ready = true;
    try {
      await page.waitForFunction(
        () => Boolean(document.querySelector('.sidebar-item')),
        undefined,
        { timeout: 15000 }
      );
    } catch {
      ready = false;
    }

    const state = await page.evaluate(() => ({
      url: location.href,
      title: document.title,
      sidebarExists: Boolean(document.getElementById('sidebar')),
      sidebarChildren: document.getElementById('sidebar')?.children.length ?? -1,
      navItems: document.querySelectorAll('.sidebar-item').length,
      pageCount: document.querySelectorAll('[data-page]').length,
      bodyClass: document.body.className,
      scripts: Array.from(document.scripts).map((s) => s.src || '(inline)'),
      htmlHead: document.head.innerHTML.slice(0, 200),
    }));

    const report = [
      `status: ${response ? response.status() : 'none'}`,
      `state: ${JSON.stringify(state, null, 2)}`,
      `console (${consoleLines.length}):\n    ${consoleLines.join('\n    ') || '<none>'}`,
      `console errors (${consoleErrors.length}): ${consoleErrors.join(' | ') || '<none>'}`,
      `failed requests (${failedRequests.length}): ${failedRequests.join(' | ') || '<none>'}`,
    ].join('\n  ');

    assert.ok(ready, `app never rendered a sidebar.\n  ${report}`);
    // A page error during boot leaves the DOM half-built; treat it as failure
    // even when the sidebar happened to appear.
    assert.deepEqual(consoleErrors, [], `console errors during boot:\n  ${report}`);
  } finally {
    await browser.close();
  }
});
