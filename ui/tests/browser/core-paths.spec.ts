import { expect, test } from '@playwright/test';
import { boot, collectEvidence, expectNoHorizontalOverflow, goTo } from './helpers';

/**
 * Core paths and accessibility.
 *
 * Replaces 1.0's `core-paths.test.mjs`. Two things changed beyond the selectors:
 * the navigation is driven by `data-testid` (a class name is a styling detail, a
 * test id is a contract), and every assertion reads real geometry or accessible
 * state rather than the presence of a text string.
 */

test('shell boots with no console errors and no failed requests @core', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // Every destination renders.
  for (const pageId of [
    'chat',
    'dashboard',
    'models',
    'presets',
    'knowledge',
    'serving',
    'observability',
  ]) {
    await goTo(page, pageId);
    await expect(page.locator('main')).toBeVisible();
  }

  expect(evidence.consoleErrors, `console errors:\n${evidence.consoleErrors.join('\n')}`).toEqual([]);
  expect(
    evidence.failedRequests,
    `failed requests:\n${evidence.failedRequests.join('\n')}`,
  ).toEqual([]);
});

test('theme and locale switches apply without a reload @core', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // Theme cycles system → light → dark and the dark token block must actually win.
  const rootClass = () => page.evaluate(() => document.documentElement.className);
  await page.click('[data-testid="theme-toggle"]');
  const afterFirst = await rootClass();
  await page.click('[data-testid="theme-toggle"]');
  const afterSecond = await rootClass();
  await page.click('[data-testid="theme-toggle"]');
  const afterThird = await rootClass();

  const seen = new Set([afterFirst, afterSecond, afterThird]);
  expect(seen.size, 'theme toggle did not change the applied theme').toBeGreaterThan(1);

  // Force dark and read a resolved colour: it must be the dark canvas, not the light.
  await page.evaluate(() => document.documentElement.classList.add('theme-dark'));
  const darkCanvas = await page.evaluate(() =>
    getComputedStyle(document.body).backgroundColor,
  );
  await page.evaluate(() => document.documentElement.classList.remove('theme-dark'));
  const lightCanvas = await page.evaluate(() =>
    getComputedStyle(document.body).backgroundColor,
  );
  expect(darkCanvas, 'dark and light canvas resolve to the same colour').not.toBe(lightCanvas);

  // Language switch: a nav label must change language in place (no reload).
  const before = await page.locator('[data-testid="nav-models"]').innerText();
  await page.click('[data-testid="locale-toggle"]');
  const after = await page.locator('[data-testid="nav-models"]').innerText();
  expect(after, 'locale toggle did not change any label').not.toBe(before);

  expect(evidence.consoleErrors, evidence.consoleErrors.join('\n')).toEqual([]);
});

test('command palette opens, filters and navigates @core', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  await page.keyboard.press('Control+k');
  const palette = page.getByRole('dialog');
  await expect(palette).toBeVisible();

  // Filter down to one destination.
  await page.keyboard.type('knowledge');
  const options = palette.getByRole('option');
  await expect(options.first()).toBeVisible();
  await page.keyboard.press('Enter');

  await expect(page.locator('[data-testid="nav-knowledge"]')).toHaveAttribute('aria-current', 'page');
  expect(evidence.consoleErrors, evidence.consoleErrors.join('\n')).toEqual([]);
});

test('no horizontal overflow at 1280 and 820 px @core', async ({ page }) => {
  const evidence = collectEvidence(page);
  await page.setViewportSize({ width: 1280, height: 860 });
  await boot(page, evidence);
  // The chat page is a lazy route, so the shell mounting does not imply the
  // transcript exists. Waiting here is what the previous assertion missed.
  await page.waitForSelector('[data-testid="chat-thread"]', { timeout: 20_000 });
  await expectNoHorizontalOverflow(page);

  await page.setViewportSize({ width: 820, height: 700 });
  await expectNoHorizontalOverflow(page);

  // The transcript column must stay inside its pane at the narrow width too.
  const geometry = await page.evaluate(() => {
    const thread = document.querySelector('[data-testid="chat-thread"]');
    const composer = document.querySelector('[data-testid="composer"]');
    return {
      threadRight: thread ? thread.getBoundingClientRect().right : null,
      composerRight: composer ? composer.getBoundingClientRect().right : null,
      viewport: window.innerWidth,
    };
  });
  expect(geometry.threadRight, 'chat-thread is not mounted').not.toBeNull();
  expect(geometry.threadRight!).toBeLessThanOrEqual(geometry.viewport + 1);
  expect(geometry.composerRight!).toBeLessThanOrEqual(geometry.viewport + 1);
});

test('sidebar collapse and restore are both reachable @core', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  await expect(page.locator('[data-testid="nav-chat"]')).toBeVisible();
  // Collapsing hides the nav; the restore control must remain visible, otherwise
  // the user cannot get the sidebar back (a 1.0 regression).
  const collapse = page.getByRole('button', { name: /collapse|收起/i }).first();
  if (await collapse.count()) {
    await collapse.click();
    await expect(page.locator('[data-testid="nav-chat"]')).toBeHidden();
    const restore = page.getByRole('button', { name: /expand|展开/i }).first();
    await expect(restore).toBeVisible();
    await restore.click();
    await expect(page.locator('[data-testid="nav-chat"]')).toBeVisible();
  }
  expect(evidence.consoleErrors, evidence.consoleErrors.join('\n')).toEqual([]);
});
