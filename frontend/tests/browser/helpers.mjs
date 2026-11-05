import { readFileSync } from 'node:fs';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';
import path from 'node:path';
import { chromium } from 'playwright-core';

const require = createRequire(import.meta.url);
const axeSource = readFileSync(require.resolve('axe-core/axe.min.js'), 'utf8');
export const BASE_URL = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17520';
export const SCREENSHOT_DIR = path.resolve(
  fileURLToPath(new URL('../../..', import.meta.url)),
  'tmp/browser'
);

export async function launchPage() {
  // 默认用 Playwright 自带的那份 Chromium（CI 里由 `npx playwright install
  // chromium` 装好）。不要默认 `channel: 'msedge'`：那要求 runner 预装 Edge，
  // 是一条外部假定——runner 镜像换版或 Edge 被策略限制时，故障现象是"页面没
  // 渲染出来"，而不是"找不到浏览器"，排查成本极高（2026-11-05 实际踩过）。
  //
  // 想验证真机 Edge 行为时设 LUNARSHROUD_BROWSER_CHANNEL=msedge。
  const channel = process.env.LUNARSHROUD_BROWSER_CHANNEL || '';
  const browser = await chromium.launch({
    ...(channel ? { channel } : {}),
    headless: process.env.LUNARSHROUD_HEADED !== '1',
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  // 资源加载失败也要留痕：index.html 引用的 JS/CSS 若 404 或 MIME 不对，
  // 浏览器只在 console 报一句，不抓就只剩"页面是空的"。
  const failedRequests = [];
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.method()} ${request.url()} -> ${request.failure()?.errorText}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) {
      failedRequests.push(`${response.request().method()} ${response.url()} -> HTTP ${response.status()}`);
    }
  });
  return { browser, context, page, consoleErrors, failedRequests };
}

/**
 * 打开应用并等到它真正就绪；失败时把**可诊断的证据**抛出去。
 *
 * 两个刻意的选择：
 *
 * 1. **不用 `waitUntil: 'networkidle'`**。页面启动后每 5 秒轮询实例、每 8 秒
 *    轮询硬件，慢 runner 上 `networkidle`（连续 500ms 无请求）可能迟迟不达成，
 *    而这与"界面能不能用"毫无关系——它会把一次纯粹的时序问题表现成"页面没
 *    渲染出来"。改为等 DOM 就绪（`domcontentloaded`）+ 应用自己的 ready 信号。
 *
 * 2. **失败时把证据打出来**。以前这里只写 `waitForSelector('.sidebar-item')`，
 *    CI 超时后日志里只剩 "Timeout 30000ms exceeded"，既看不到 console 报错、
 *    也看不到响应体，只能靠猜（2026-11-05 就是这样排查了整轮）。所以失败时
 *    附上：实际 URL、HTTP 状态、DOM 快照、全部 console 输出、失败请求。
 */
export async function gotoApp(page, consoleErrors, failedRequests = [], url = BASE_URL) {
  const response = await page.goto(url, { waitUntil: 'domcontentloaded' });
  try {
    // `boot()` 最后会 log 一行 `[LunarShroud] frontend ready`，且它会同步建好
    // 侧栏。等这个信号比等一个固定时长可靠：它要么到达，要么说明 boot 抛错了。
    await page.waitForSelector('.sidebar-item', { timeout: 20000 });
  } catch (error) {
    const status = response ? response.status() : 'no response';
    let body = '';
    let title = '';
    let probe = '';
    try {
      title = await page.title();
      const state = await page.evaluate(() => ({
        readyState: document.readyState,
        hasSidebar: Boolean(document.getElementById('sidebar')),
        navItems: document.querySelectorAll('.sidebar-item').length,
        scripts: Array.from(document.scripts).map((s) => s.src || '(inline)'),
      }));
      probe = JSON.stringify(state);
      body = (await page.evaluate(() => document.body?.innerText || document.documentElement?.outerHTML || ''))
        .replace(/\s+/g, ' ')
        .slice(0, 300);
    } catch (probeError) {
      body = `<could not read page: ${probeError.message}>`;
    }
    const details = [
      `url: ${page.url()}`,
      `http status: ${status}`,
      `title: ${title || '<empty>'}`,
      `dom: ${probe || '<unavailable>'}`,
      `body: ${body || '<empty>'}`,
      `console errors (${consoleErrors.length}): ${consoleErrors.join(' | ') || '<none>'}`,
      `failed requests (${failedRequests.length}): ${failedRequests.join(' | ') || '<none>'}`,
    ].join('\n  ');
    throw new Error(
      `app did not render a sidebar.\n  ${details}\n  original: ${error.message}`
    );
  }
  return response;
}

export async function runAxe(page) {
  // CSP is script-src 'self'; serve axe from the page origin through Playwright's
  // request interception instead of injecting an inline script.
  await page.route('**/__axe.min.js', (route) => route.fulfill({
    status: 200,
    contentType: 'application/javascript',
    body: axeSource,
  }));
  await page.addScriptTag({ url: '/__axe.min.js' });
  return page.evaluate(async () => await window.axe.run(document, {
    resultTypes: ['violations'],
  }));
}

/**
 * 把 axe 违例整理成可直接行动的文本。
 *
 * 门禁原本只打印 `color-contrast: 2 node(s)`。一句话之外什么都没有：不知道
 * 是哪个元素、哪个 token、实测比值多少，于是只能靠猜（2026-11-05 为此多花了
 * 两轮）。这里把 axe 自己给出的**前景色、背景色、实测比值**原样带出来。
 */
export function describeViolations(violations) {
  const lines = [];
  for (const violation of violations) {
    lines.push(`${violation.id}: ${violation.nodes.length} node(s)`);
    for (const node of violation.nodes) {
      lines.push(`  target: ${node.target.join(' ')}`);
      if (node.html) lines.push(`  html:   ${String(node.html).slice(0, 200)}`);
      if (node.failureSummary) {
        lines.push(`  why:    ${String(node.failureSummary).replace(/\n/g, ' | ')}`);
      }
    }
  }
  return lines.join('\n');
}
