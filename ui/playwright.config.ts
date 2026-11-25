import { defineConfig } from '@playwright/test';

/**
 * Browser acceptance config.
 *
 * The suite runs against a **real backend** serving the built UI
 * (`tools/dev_backend.py`, which isolates DATA_DIR). That is deliberate: the 1.0
 * release was green while being unusable because every gap lived past where the
 * test doubles stopped — `TestClient` never crosses the browser CORS layer, and a
 * fake never returns a 500.
 *
 * `LUNARSHROUD_TEST_URL` lets CI point at whatever port it started.
 */
export default defineConfig({
  testDir: './tests/browser',
  fullyParallel: false,
  // Serial on purpose: these specs share one backend and one data directory.
  workers: 1,
  retries: 0,
  reporter: [['list']],
  timeout: 60_000,
  expect: { timeout: 10_000 },
  use: {
    baseURL: process.env.LUNARSHROUD_TEST_URL || 'http://127.0.0.1:17521',
    viewport: { width: 1280, height: 860 },
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  projects: [{ name: 'chromium', use: { browserName: 'chromium' } }],
});
