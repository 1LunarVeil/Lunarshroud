/**
 * Visual capture and console-error probe for the 2.0 shell.
 *
 * Not a test suite: this is the inspection tool used while building, so a claim
 * about how the UI looks is backed by a screenshot instead of memory. The gated
 * acceptance suite lives in `tests/browser/*.spec.ts`.
 *
 * Usage:
 *   LUNARSHROUD_TEST_URL=http://127.0.0.1:17521 node tests/visual/capture.mjs
 */
import { mkdir, writeFile } from 'node:fs/promises';
import path from 'node:path';
import { chromium } from 'playwright-core';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17521';
const OUT = path.resolve(import.meta.dirname, '../../shots');

const consoleErrors = [];
const failedRequests = [];

const browser = await chromium.launch({ headless: true });
const context = await browser.newContext({ viewport: { width: 1280, height: 860 } });
const page = await context.newPage();

page.on('console', (message) => {
  if (message.type() === 'error') consoleErrors.push(message.text());
});
page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`));
page.on('requestfailed', (request) => {
  failedRequests.push(`${request.method()} ${request.url()} -> ${request.failure()?.errorText}`);
});
page.on('response', (response) => {
  if (response.status() >= 400) {
    failedRequests.push(`${response.request().method()} ${response.url()} -> ${response.status()}`);
  }
});

await mkdir(OUT, { recursive: true });

const report = { base: BASE, steps: [], consoleErrors, failedRequests };

async function step(name, action) {
  try {
    if (action) await action();
    await page.waitForTimeout(450);
    const file = path.join(OUT, `${name}.png`);
    await page.screenshot({ path: file, fullPage: false });
    report.steps.push({ name, file, url: page.url() });
    console.log(`captured ${name}`);
  } catch (error) {
    report.steps.push({ name, error: String(error) });
    console.error(`FAILED ${name}: ${error}`);
  }
}

const response = await page.goto(BASE, { waitUntil: 'domcontentloaded' });
console.log('status', response?.status());

// Boot signal: the sidebar exists only after React has mounted.
try {
  await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 15000 });
} catch {
  const html = await page.content();
  await writeFile(path.join(OUT, 'boot-failure.html'), html, 'utf8');
  console.error('boot did not complete; DOM dumped to shots/boot-failure.html');
}

await step('light-chat', async () => {
  await page.evaluate(() => document.documentElement.classList.remove('theme-dark'));
});

await step('dark-chat', async () => {
  await page.evaluate(() => document.documentElement.classList.add('theme-dark'));
});

await step('settings', async () => {
  await page.click('[data-testid="nav-settings"]');
});
await step('settings-dark', async () => {
  await page.evaluate(() => document.documentElement.classList.add('theme-dark'));
});
await page.keyboard.press('Escape');

await step('models-page', async () => {
  await page.evaluate(() => document.documentElement.classList.remove('theme-dark'));
  await page.click('[data-testid="nav-models"]');
});
await step('dashboard-page', async () => {
  await page.click('[data-testid="nav-dashboard"]');
});

await step('command-palette', async () => {
  await page.click('[data-testid="nav-chat"]');
  await page.keyboard.press('Control+k');
});
await page.keyboard.press('Escape');

// Geometry probe: the transcript column must never overflow its pane.
const geometry = await page.evaluate(() => {
  const thread = document.querySelector('[data-testid="chat-thread"]');
  const composer = document.querySelector('[data-testid="composer"]');
  const toolbar = document.querySelector('header');
  return {
    viewport: { width: window.innerWidth, height: window.innerHeight },
    thread: thread ? thread.getBoundingClientRect().toJSON() : null,
    composer: composer ? composer.getBoundingClientRect().toJSON() : null,
    toolbarHeight: toolbar ? toolbar.getBoundingClientRect().height : null,
    bodyOverflowX: document.body.scrollWidth > window.innerWidth,
    // Every colour-bearing element must resolve colour from the token layer, which
    // is why this counts computed styles that failed to resolve.
    emptyBackgrounds: [...document.querySelectorAll('div,header,footer,nav')].filter(
      (node) => getComputedStyle(node).backgroundColor === 'rgba(0, 0, 0, 0)' && node.clientHeight > 40,
    ).length,
  };
});
report.geometry = geometry;

// Narrow viewport: no horizontal overflow allowed.
await page.setViewportSize({ width: 820, height: 700 });
await step('narrow-chat');
report.narrowOverflow = await page.evaluate(
  () => document.body.scrollWidth > window.innerWidth,
);

await writeFile(path.join(OUT, 'report.json'), JSON.stringify(report, null, 2), 'utf8');
await browser.close();

console.log('\n--- summary ---');
console.log('console errors:', consoleErrors.length);
if (consoleErrors.length) console.log(consoleErrors.slice(0, 12).join('\n'));
console.log('failed requests:', failedRequests.length);
if (failedRequests.length) console.log(failedRequests.slice(0, 12).join('\n'));
console.log('body overflow at 820px:', report.narrowOverflow);
console.log('geometry:', JSON.stringify(geometry, null, 2));
