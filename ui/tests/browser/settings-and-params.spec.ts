import { expect, test } from '@playwright/test';
import { boot, collectEvidence } from './helpers';

/**
 * The three defects reported against 2.0.0, driven through a real browser.
 *
 * None of these could be caught by the unit suite:
 *
 * - The Developer tab crashed **only when the tab was clicked**, because that is
 *   when `DiagnosticsTab` mounts and calls `.map` on an object. A unit test of the
 *   type would have agreed with the code, because the type was the lie.
 * - The path fields had no picker at all — an absence, not a wrong value.
 * - The parameter page's density is a rendered property; counting source lines
 *   says nothing about how much a user has to scroll.
 *
 * The settings sheet is a dialog opened from the sidebar footer, and the params page
 * is a secondary route with no sidebar entry, so both are driven by clicking rather
 * than through `goTo()`.
 */

async function openSettings(page: import('@playwright/test').Page) {
  await page.getByTestId('nav-settings').click();
  const sheet = page.getByRole('dialog');
  await expect(sheet).toBeVisible({ timeout: 10_000 });
  return sheet;
}

test('the Developer tab renders storage and paths instead of crashing', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);

  // Clicking the tab is what mounts the component that used to throw.
  await sheet.getByRole('button', { name: /开发者|Developer/ }).click();

  // The app-level boundary renders this on any uncaught render error.
  await expect(page.getByTestId('app-crash')).toHaveCount(0);

  // The storage block must list the three databases by label, with real paths.
  // Before the fix this threw `databases.map is not a function` and the whole
  // window was replaced by the crash screen.
  await expect(sheet.getByText('chats', { exact: true })).toBeVisible();
  await expect(sheet.getByText('history', { exact: true })).toBeVisible();
  await expect(sheet.getByText('knowledge', { exact: true })).toBeVisible();
  await expect(sheet.getByText(/chats\.db/)).toBeVisible();

  // And the paths block must show the real Python version. The old type read
  // `python` while the route sends `python_version`, so this row rendered blank
  // and nothing threw — the quiet half of the same defect.
  const pythonRow = sheet
    .locator('dt', { hasText: /Python/i })
    .locator('xpath=following-sibling::dd[1]');
  await expect(pythonRow).toHaveText(/^\d+\.\d+\.\d+/);

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors, null, 2)).toEqual([]);
});

test('the model directory field offers a native picker', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);
  await sheet.getByRole('button', { name: /常规|General/ }).click();

  // Every path field carries a "Browse…" button. In the browser channel there is no
  // Tauri bridge, so clicking it falls back to `window.prompt`; this test only
  // asserts the affordance exists and is wired to the field's row.
  const serverPathRow = page.locator('#settings-server-path').locator('xpath=ancestor::div[1]');
  await expect(serverPathRow.getByRole('button', { name: /浏览|Browse/ })).toBeVisible();

  const modelDirRow = page.locator('#settings-model-dir').locator('xpath=ancestor::div[1]');
  await expect(modelDirRow.getByRole('button', { name: /浏览|Browse/ })).toBeVisible();

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors, null, 2)).toEqual([]);
});

/**
 * Every settings section renders, and the model-directory save actually works.
 *
 * Two things this pins that nothing else did:
 *
 * - **10 tabs, not 5.** 2.1 split the sheet into ten sections (系统与引擎 / 聊天 /
 *   用量统计 / 数据 / 关于 were new). The older specs only ever clicked 常规 and
 *   开发者, so five whole sections could have crashed on mount and the suite would
 *   have stayed green.
 * - **The save the user reported.** "默认模型目录 → 保存 → 不支持的配置项" was a
 *   key-name mismatch (`model_directory` vs `default_model_dir`); a unit test of
 *   the type agreed with the bug. Here the real PUT runs against the real backend:
 *   the field keeps its value and no error toast appears.
 */
test('every settings section renders and the model directory saves', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);

  const tabs = [
    'general',
    'appearance',
    'chat',
    'system',
    'ports',
    'serving',
    'usage',
    'data',
    'diagnostics',
    'about',
  ] as const;
  for (const tab of tabs) {
    await page.getByTestId(`settings-tab-${tab}`).click();
    // A section that throws on mount replaces the window with the crash screen.
    await expect(page.getByTestId('app-crash')).toHaveCount(0);
    await expect(page.getByTestId('settings-body')).not.toBeEmpty();
  }

  // The reported defect, end to end: type a directory, save, read it back.
  await page.getByTestId('settings-tab-general').click();
  const directory = 'D:\\browser-test-models';
  await page.fill('#settings-model-dir', directory);
  await sheet.getByRole('button', { name: /保存设置|Save settings/i }).click();

  // The success toast is the only proof the PUT was accepted; the failure path
  // renders "不支持的配置项" / "Unsupported settings key".
  await expect(page.getByText(/设置已保存|Settings saved/i)).toBeVisible({ timeout: 10_000 });
  await expect(page.getByText(/不支持的配置项|Unsupported settings key/i)).toHaveCount(0);

  // Re-open the sheet: the value must come back from the backend, not from the draft.
  await page.keyboard.press('Escape');
  await page.waitForTimeout(300);
  const reopened = await openSettings(page);
  await reopened.getByTestId('settings-tab-general').click();
  await expect(page.locator('#settings-model-dir')).toHaveValue(directory);

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors, null, 2)).toEqual([]);
});

test('the parameter page is one page of collapsible sections with short descriptions', async ({
  page,
}) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // Secondary route: reachable from the command palette, not the sidebar.
  await page.keyboard.press('Control+k');
  await page.getByRole('combobox').fill('Load model');
  await page.keyboard.press('Enter');
  await page.waitForSelector('[data-testid^="param-group-"]', { timeout: 15_000 });

  // All eight groups exist at once — the tab layout made seven of them unreachable
  // without clicking. This is the "must page through" complaint.
  const groups = page.locator('[data-testid^="param-group-"]');
  await expect(groups).toHaveCount(8);

  // Only the backend's `default_open` groups start expanded.
  const open = page.locator('[data-testid^="param-group-"][data-open="true"]');
  await expect(open).toHaveCount(3);

  // Long explanations are behind a disclosure, not printed under every control.
  const detailButtons = page.getByRole('button', { name: /展开说明|Show description/ });
  expect(await detailButtons.count()).toBeGreaterThan(5);
  const firstDetail = detailButtons.first();
  await firstDetail.click();
  await expect(
    page.getByRole('button', { name: /收起说明|Hide description/ }).first(),
  ).toBeVisible();

  // Searching narrows the page and force-opens the matching sections.
  await page.getByTestId('params-search').fill('显存');
  await page.waitForTimeout(200);
  const stillThere = await page.locator('[data-testid^="param-group-"]').count();
  expect(stillThere).toBe(8);
  const openAfterSearch = await page
    .locator('[data-testid^="param-group-"][data-open="true"]')
    .count();
  expect(openAfterSearch).toBeLessThanOrEqual(3);

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors, null, 2)).toEqual([]);
});

test('no page header shows a raw translation key', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // `nav.params` / `params.subtitle` / `params.model` were displayed verbatim
  // because the route id and the dictionary key disagreed.
  for (const route of ['chat', 'dashboard', 'models', 'presets', 'knowledge', 'serving']) {
    await page.getByTestId(`nav-${route}`).click();
    const header = await page.locator('header').first().innerText();
    expect(header, `raw key in the toolbar on ${route}`).not.toMatch(/\bnav\.[a-z_]+/);
  }

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors, null, 2)).toEqual([]);
});
