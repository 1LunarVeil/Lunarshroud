/** Capture every page in both themes for visual review. */
import { mkdir } from 'node:fs/promises';
import { chromium } from 'playwright-core';

const BASE = process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17521';
const OUT = 'shots/pages';
await mkdir(OUT, { recursive: true });

const browser = await chromium.launch();
const errors = [];
const page = await (await browser.newContext({ viewport: { width: 1440, height: 900 } })).newPage();
page.on('console', (m) => {
  if (m.type() === 'error') errors.push(m.text());
});
page.on('pageerror', (e) => errors.push(`pageerror: ${e.message}`));

await page.goto(BASE, { waitUntil: 'domcontentloaded' });
await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 20000 });

const pages = ['chat', 'agent', 'dashboard', 'models', 'params', 'presets', 'knowledge', 'serving', 'observability', 'help'];

const stats = [];
for (const theme of ['light', 'dark']) {
  await page.evaluate((value) => {
    document.documentElement.classList.toggle('theme-dark', value === 'dark');
  }, theme);
  for (const name of pages) {
    const navExists = await page.locator(`[data-testid="nav-${name}"]`).count();
    if (!navExists) {
      // params and help are secondary destinations, not in the sidebar groups.
      await page.evaluate((target) => {
        const store = window.__lunarNav;
        if (store) store(target);
      }, name);
      // Fall back to the command palette, which reaches every route.
      await page.keyboard.press('Control+k');
      await page.waitForTimeout(300);
      // The palette filters on the localized label, and the UI language follows the
      // browser locale. Use each destination's English label explicitly.
      const labels = { params: 'Load model', help: 'Getting started' };
      await page.fill('input[role="combobox"]', labels[name] ?? name);
      await page.waitForTimeout(300);
      await page.keyboard.press('Enter');
      await page.waitForTimeout(600);
      // Only the command palette renders this exact overlay; the settings sheet is a
      // Radix dialog with a different container.
      const stillOpen = await page.evaluate(
        () => document.querySelectorAll('div[role="presentation"]').length,
      );
      if (stillOpen) {
        console.log(`WARN: palette still open after navigating to ${name}; pressing Escape`);
        await page.keyboard.press('Escape');
        await page.waitForTimeout(300);
      }
    } else {
      await page.click(`[data-testid="nav-${name}"]`);
    }
    await page.waitForTimeout(700);
    const metrics = await page.evaluate(() => ({
      overflowX: document.body.scrollWidth > window.innerWidth,
      scrollWidth: document.body.scrollWidth,
      viewport: window.innerWidth,
      heading: document.querySelector('main h1, main h2')?.textContent?.trim().slice(0, 60) ?? null,
      controls: document.querySelectorAll('main button, main input, main select').length,
    }));
    stats.push({ theme, page: name, ...metrics });
    await page.screenshot({ path: `${OUT}/${theme}-${name}.png` });
  }
}

console.log(JSON.stringify(stats, null, 1));
console.log('\nconsole errors:', errors.length);
if (errors.length) console.log(errors.slice(0, 8).join('\n'));
await browser.close();
