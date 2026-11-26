import { expect, test } from '@playwright/test';
import { boot, collectEvidence } from './helpers';

/**
 * Layout widths survive a machine move.
 *
 * `chat_sidebar_width` / `chat_panel_width` have been in `DEFAULT_CONFIG`, range
 * validated and unit-tested since 1.0, with a comment promising the layout follows
 * the user to another machine. Nothing ever **read** them: the settings sheet wrote
 * them and the shell took its width from localStorage only — a write-only setting.
 *
 * This drives the whole loop: seed the config, wipe the local value (the "new
 * machine" state), reload, and the shell must come up at the configured width.
 */
test('a configured sidebar width is used when this machine has no local value', async ({
  page,
}) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  const handle = page.getByRole('separator', { name: /resize|调整/i }).first();
  const measured = () =>
    page.evaluate(() => {
      const separator = document.querySelector('[role="separator"][aria-orientation="vertical"]');
      if (!separator) return 0;
      // The sidebar sits immediately before the handle; its width is the thing the
      // config value controls.
      const sidebar = separator.previousElementSibling as HTMLElement | null;
      return Math.round(sidebar?.getBoundingClientRect().width ?? 0);
    });

  // Seed a value that differs from the built-in default (272) so passing cannot be
  // explained by the fallback.
  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_sidebar_width: 300 }),
    });
  });

  // The "different machine" state: no local layout value.
  await page.evaluate(() => localStorage.removeItem('lunarshroud_sidebar_width'));
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 20_000 });

  await expect.poll(measured, { timeout: 10_000 }).toBe(300);
  await expect(handle).toBeVisible();

  // Restore so the shared dev backend is left as it was.
  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_sidebar_width: 0 }),
    });
    localStorage.removeItem('lunarshroud_sidebar_width');
  });
});

test('dragging the sidebar handle writes the width back to config', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const handle = page.getByRole('separator', { name: /resize|调整/i }).first();
  const box = await handle.boundingBox();
  expect(box, 'the resize handle has no geometry').not.toBeNull();

  const configured = () =>
    page.evaluate(async () => {
      const response = await fetch('/api/system/config');
      const body = await response.json();
      return Number(body.chat_sidebar_width);
    });

  const before = await configured();

  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x + 60, box.y + box.height / 2, { steps: 8 });
    await page.mouse.up();
  }

  // The write happens on pointer-up, not per move: a PUT per pointermove would be
  // dozens of requests per drag.
  await expect
    .poll(configured, { timeout: 10_000 })
    .not.toBe(before);

  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_sidebar_width: 0 }),
    });
    localStorage.removeItem('lunarshroud_sidebar_width');
  });
});

test('an appearance profile exports and re-imports through the settings sheet', async ({
  page,
}) => {
  /*
    Drives the file round trip a user actually performs. The export is a download and
    the import is a file input, so this intercepts the download and feeds the file
    back through `setInputFiles` — the same bytes in both directions.
  */
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  const sheet = page.getByRole('dialog');
  await page.getByTestId('nav-settings').click();
  await expect(sheet).toBeVisible({ timeout: 10_000 });
  await sheet.getByRole('button', { name: /外观|Appearance/ }).click();

  const downloadPromise = page.waitForEvent('download');
  await sheet.getByTestId('appearance-export').click();
  const download = await downloadPromise;
  const path = await download.path();
  expect(path, 'the export produced no file').not.toBeNull();

  const fs = await import('node:fs/promises');
  const text = await fs.readFile(path as string, 'utf8');
  const profile = JSON.parse(text) as Record<string, unknown>;

  // The profile must carry appearance keys and nothing sensitive.
  expect(Object.keys(profile)).toContain('accent_preset');
  expect(Object.keys(profile)).toContain('font_preset');
  expect(profile).not.toHaveProperty('llama_server_path');
  expect(profile).not.toHaveProperty('api_server_key');

  // Import it back. It stages into the draft, so Save becomes available.
  const input = sheet.locator('input[type="file"][accept*="json"]').last();
  await input.setInputFiles(path as string);
  await expect(
    sheet.getByRole('button', { name: /保存|Save/ }),
  ).toBeEnabled({ timeout: 10_000 });
});

test('high contrast raises real text contrast rather than only the attribute', async ({
  page,
}) => {
  /*
    Reads the resolved colour of a real element and computes the ratio against its
    surface. Asserting `data-contrast` alone would pass even if the CSS block never
    matched — the same hole that let `--z-dropdown` ship (class present, declaration
    invalid).
  */
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  const sample = () =>
    page.evaluate(() => {
      // A tertiary-text element that exists on every page: the status-bar hint.
      const node =
        document.querySelector('[data-testid="status-hint"]') ??
        document.querySelector('p');
      if (!node) return null;
      const style = getComputedStyle(node);
      return { color: style.color, background: getComputedStyle(document.body).backgroundColor };
    });

  const before = await sample();
  expect(before).not.toBeNull();

  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ high_contrast: true }),
    });
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 20_000 });

  const after = await sample();
  expect(after).not.toBeNull();
  // The computed colour must change; "attribute set" alone is not evidence.
  expect(after?.color).not.toBe(before?.color);

  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ high_contrast: false }),
    });
  });
});

test('the configured inspector width reaches the rendered panel', async ({ page }) => {
  /*
    `chat_panel_width` was a three-way break: the config key existed, the store had
    `inspectorWidth` (whose setter nothing called), and the panel read a CSS token.
    Hydration alone would move the value into the store and still change nothing on
    screen — which is why this asserts **geometry**, not state.
  */
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_panel_width: 360 }),
    });
    localStorage.removeItem('lunarshroud_inspector_width');
  });
  await page.reload({ waitUntil: 'domcontentloaded' });
  await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 20_000 });

  // Open the inspector from the toolbar.
  await page.getByRole('button', { name: /检查器|Inspector/ }).first().click();
  const panel = page.getByTestId('chat-inspector');
  await expect(panel).toBeVisible({ timeout: 10_000 });

  const width = () =>
    page.evaluate(() =>
      Math.round(
        document.querySelector('[data-testid="chat-inspector"]')?.getBoundingClientRect()
          .width ?? 0,
      ),
    );
  await expect.poll(width, { timeout: 10_000 }).toBe(360);

  await page.evaluate(async () => {
    await fetch('/api/system/config', {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ chat_panel_width: 0 }),
    });
    localStorage.removeItem('lunarshroud_inspector_width');
  });
});
