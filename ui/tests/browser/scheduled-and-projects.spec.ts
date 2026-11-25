import { expect, test } from '@playwright/test';
import { boot, collectEvidence } from './helpers';

/**
 * The 2.2 surfaces that only exist as a *rendered* behaviour: scheduled tasks and
 * project grouping.
 *
 * Both are things a source assertion cannot vouch for. A task list that renders an
 * empty state because the response envelope was wrong, and a "grouped" sidebar that
 * still renders one flat list, both look correct in the source and in the type.
 *
 * The state check watches **the app's own request** (`page.waitForResponse`) rather
 * than making a side request from the test: that proves the page really called the
 * route and really understood the answer, which is the whole point.
 */

async function openSettings(page: import('@playwright/test').Page) {
  await page.getByTestId('nav-settings').click();
  const sheet = page.getByRole('dialog');
  await expect(sheet).toBeVisible({ timeout: 10_000 });
  return sheet;
}

test('the scheduled-tasks tab creates a task and shows its schedule', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);

  await sheet.getByRole('button', { name: /定时任务|Scheduled tasks/ }).click();

  // The editor only exists after the button is pressed — no stub panel.
  await sheet.getByRole('button', { name: /新建任务|New task/ }).click();

  const name = `e2e-${Date.now()}`;
  await sheet.getByLabel(/任务名|Task name/).fill(name);
  await sheet.getByLabel(/提示词|Prompt/).fill('Summarise the day in one line.');
  // "Daily" is the default shape; the time field must be present and accepted.
  await sheet.getByLabel(/^时间$|^Time$/).fill('09:30');

  const created = page.waitForResponse(
    (response) => response.url().includes('/api/scheduled-tasks') && response.request().method() === 'POST',
  );
  await sheet.getByRole('button', { name: /保存|Save/ }).click();

  const response = await created;
  expect(response.status(), 'the create call failed').toBe(200);
  const stored = await response.json();

  // The server's own label is what the row shows; `schedule_label` is the backend's
  // answer, so a wrong unit or a refused schedule surfaces here.
  expect(stored.schedule).toEqual({ kind: 'daily', at: '09:30' });
  expect(typeof stored.next_run_at).toBe('number');
  expect(stored.schedule_label).toContain('09:30');

  await expect(sheet.getByText(name, { exact: false })).toBeVisible({ timeout: 10_000 });

  // Clean up through the UI's own delete button so the suite is idempotent against a
  // shared backend, and so the delete path is exercised too.
  page.once('dialog', (dialog) => void dialog.accept());
  const row = sheet.locator('li', { hasText: name }).first();
  await row.getByRole('button', { name: /删除|Delete/ }).click();
  await expect(sheet.getByText(name, { exact: false })).toHaveCount(0, { timeout: 10_000 });
});

test('choosing an accent preset repaints real elements, not just an attribute', async ({
  page,
}) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);
  await sheet.getByRole('button', { name: /外观|Appearance/ }).click();

  // Read the computed colour of an element that really uses the accent, before and
  // after. Asserting `data-accent` alone would pass even if no CSS block matched.
  const accentColor = () =>
    page.evaluate(() => {
      const node = document.querySelector('[data-testid="nav-chat"]');
      if (!node) return '';
      return getComputedStyle(node).getPropertyValue('--accent').trim();
    });

  const before = await accentColor();
  expect(before).not.toBe('');

  // Reset to a known value first. The shared dev backend keeps state across runs, so
  // a previous failed attempt could have left another preset applied; without this
  // the "changed" assertion can start from the value it is about to set.
  const blue = sheet.getByTestId('accent-blue');
  await blue.click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  await expect.poll(accentColor, { timeout: 10_000 }).toBe('#2c5fd6');
  const baseline = await accentColor();

  const swatch = sheet.getByTestId('accent-teal');
  await expect(swatch).toBeVisible({ timeout: 5000 });
  await swatch.click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  // Wait for the config to land; the effect reads from the config query.
  await expect
    .poll(accentColor, { timeout: 10_000 })
    .not.toBe(baseline);

  // Restore, so the shared dev backend is left as it was.
  const reset = sheet.getByTestId('accent-blue');
  await reset.click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  await expect.poll(accentColor, { timeout: 10_000 }).toBe(baseline);
});

test('a project groups the conversations assigned to it', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // Start from a conversation so there is something to group.
  await page.getByTestId('nav-chat').click();

  // Count first: the shared dev backend keeps conversations from earlier runs, so
  // "a row is visible" is true before this test does anything. That false green is
  // exactly how the missing list invalidation survived the first version of this
  // test — a `first()` locator matched an old row 54 ms after the click.
  const rows = () => page.locator('[data-testid^="conversation-chat_"]');
  const before = await rows().count();

  await page.getByRole('button', { name: /新建会话|New chat/ }).first().click();
  await expect(rows()).toHaveCount(before + 1, { timeout: 10_000 });

  // The newest row is the one at the top of the list.
  const row = rows().first();

  // Create a project through the list menu. The name goes into an inline field, not
  // a `window.prompt` — WebView2 in a Tauri window does not implement prompt(), so
  // the packaged app would have created nothing.
  const projectName = `group-${Date.now()}`;
  await page.getByTestId('conversation-menu').click();
  await expect(page.getByRole('menu')).toBeVisible({ timeout: 5000 });
  await page.getByRole('menuitem', { name: /新建项目|New project/ }).click();
  const nameField = page.getByRole('textbox', { name: /项目名称|Project name/ });
  await expect(nameField).toBeVisible({ timeout: 5000 });
  await nameField.fill(projectName);
  await nameField.press('Enter');

  // Assign the conversation to it: the row's project control opens a select, which
  // only exists while that row is being edited.
  const rowItem = page.locator('li', { has: row }).first();
  await rowItem.getByRole('button', { name: /归属项目|Project/ }).click();
  const select = rowItem.getByRole('combobox');
  await select.selectOption({ label: projectName });

  // The row must end up inside a section titled with the project's name — that is
  // what "grouped" means. An h3 alone proves nothing.
  const group = page.locator('section[data-testid^="conversation-group-"]', {
    has: page.getByRole('heading', { name: new RegExp(projectName) }),
  });
  await expect(group).toBeVisible({ timeout: 10_000 });
  await expect(group.locator('[data-testid^="conversation-chat_"]')).toHaveCount(1);
});
