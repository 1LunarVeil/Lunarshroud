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
  const browser = await chromium.launch({
    channel: process.env.LUNARSHROUD_BROWSER_CHANNEL || 'msedge',
    headless: process.env.LUNARSHROUD_HEADED !== '1',
  });
  const context = await browser.newContext({ viewport: { width: 1280, height: 900 } });
  const page = await context.newPage();
  const consoleErrors = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => consoleErrors.push(error.message));
  return { browser, context, page, consoleErrors };
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
