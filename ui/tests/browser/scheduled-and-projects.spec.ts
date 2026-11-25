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

test('the body font preset changes the computed font family', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = await openSettings(page);
  await sheet.getByRole('button', { name: /外观|Appearance/ }).click();

  // The computed `font-family` of <body> is what actually renders — asserting the
  // `data-font` attribute would pass even if no CSS block matched (that is exactly
  // how `--z-dropdown` shipped: attribute/class present, declaration invalid).
  const bodyFont = () =>
    page.evaluate(() => getComputedStyle(document.body).fontFamily.toLowerCase());

  // Reset to a known state first: the shared dev backend keeps config across runs.
  await sheet.getByTestId('font-system').click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  await expect.poll(bodyFont, { timeout: 10_000 }).toContain('inter');
  const baseline = await bodyFont();

  await sheet.getByTestId('font-mono').click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  await expect.poll(bodyFont, { timeout: 10_000 }).toContain('mono');

  await sheet.getByTestId('font-system').click();
  await sheet.getByRole('button', { name: /保存|Save/ }).click();
  await expect.poll(bodyFont, { timeout: 10_000 }).toBe(baseline);
});

test('the whole-window wallpaper paints a real layer behind the app', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  /*
    This drives the **layer**, not the settings file.

    An earlier version wrote `chat_background_image` through the config API and left
    it cleared afterwards — which still changed global state for every later spec:
    `core-paths` compares the body colour in both themes, and a wallpaper makes that
    ground transparent, so the two themes resolved to the same value and an unrelated
    test failed. Injecting the same custom properties the effect writes proves the CSS
    contract without touching shared state.
  */
  const onePixel =
    'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==';

  await page.evaluate((image) => {
    const style = document.body.style;
    style.setProperty('--wallpaper-image', `url("${image}")`);
    style.setProperty('--wallpaper-opacity', '0.5');
    style.setProperty('--wallpaper-blur', '0px');
    style.setProperty('--wallpaper-background', 'transparent');
  }, onePixel);

  // The layer is a `::before` on body; `getComputedStyle` reports its resolved values.
  const layer = await page.evaluate(() => {
    const style = getComputedStyle(document.body, '::before');
    return {
      image: style.backgroundImage,
      position: style.position,
      pointerEvents: style.pointerEvents,
      bodyBackground: getComputedStyle(document.body).backgroundColor,
    };
  });

  expect(layer.position).toBe('fixed');
  expect(layer.pointerEvents).toBe('none');
  expect(layer.image).toContain('data:image/png');
  // The body ground must be transparent, or it paints over the layer: a fixed,
  // negatively-stacked child paints behind its parent's background. This is the
  // assertion that catches the real defect (verified by injecting
  // `--wallpaper-background: var(--canvas)`: the body stays opaque and the image is
  // completely hidden).
  expect(layer.bodyBackground).toBe('rgba(0, 0, 0, 0)');

  // The opacity transition is 180 ms; sample until it settles. Reading once measures
  // a mixed value (observed 0.131 for a target of 0.5) — the same mid-animation race
  // that made the a11y gate report a varying violation count.
  // `getComputedStyle().opacity` is a CSS string and `toBeCloseTo` needs a number.
  // Poll on a parsed numeric value produced **in the browser**, so no string ever
  // reaches the matcher (an earlier version polled the raw string and failed with
  // "received value must be a number" even though it printed as 0.5).
  const settled = () =>
    page.evaluate(
      () => Number.parseFloat(getComputedStyle(document.body, '::before').opacity) || 0,
    );
  await expect.poll(() => settled(), { timeout: 5000 }).toBeCloseTo(0.5, 2);
  // Clearing the properties must take the layer away rather than leave it half-on.
  await page.evaluate(() => {
    const style = document.body.style;
    for (const name of [
      '--wallpaper-image',
      '--wallpaper-opacity',
      '--wallpaper-blur',
      '--wallpaper-background',
    ]) {
      style.removeProperty(name);
    }
  });
  // Clearing the properties starts a 180 ms fade-out; poll until it settles rather
  // than sampling once (the same mid-transition race as the fade-in above).
  await expect
    .poll(
      () =>
        page.evaluate(
          () =>
            Number.parseFloat(getComputedStyle(document.body, '::before').opacity) || 0,
        ),
      { timeout: 5000 },
    )
    .toBeCloseTo(0, 2);
  const after = await page.evaluate(() => ({
    image: getComputedStyle(document.body, '::before').backgroundImage,
    bodyBackground: getComputedStyle(document.body).backgroundColor,
  }));
  expect(after.image === 'none' || after.image === '').toBe(true);
  expect(after.bodyBackground).not.toBe('rgba(0, 0, 0, 0)');
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
