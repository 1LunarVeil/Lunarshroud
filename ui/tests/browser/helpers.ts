import { expect, type Page } from '@playwright/test';

/**
 * Shared boot helper.
 *
 * The failure mode this must not have: "page did not load" with no evidence. 1.0
 * spent six CI rounds chasing a blank page whose HTTP responses were all 200 —
 * `.mjs` was served as `text/plain` and the module chain was silently rejected.
 * So a failed boot dumps the URL, status, console errors, failed requests and a
 * DOM excerpt.
 */
export interface BootEvidence {
  consoleErrors: string[];
  failedRequests: string[];
}

export function collectEvidence(page: Page): BootEvidence {
  const consoleErrors: string[] = [];
  const failedRequests: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => consoleErrors.push(`pageerror: ${error.message}`));
  page.on('requestfailed', (request) => {
    failedRequests.push(`${request.method()} ${request.url()} -> ${request.failure()?.errorText}`);
  });
  page.on('response', (response) => {
    if (response.status() >= 400) {
      failedRequests.push(
        `${response.request().method()} ${response.url()} -> HTTP ${response.status()}`,
      );
    }
  });
  return { consoleErrors, failedRequests };
}

/** Wait for the React shell to have mounted. Signals on the sidebar, which is
 *  rendered by the app and not present in index.html. */
export async function boot(page: Page, evidence: BootEvidence): Promise<void> {
  const response = await page.goto('/', { waitUntil: 'domcontentloaded' });
  try {
    await page.waitForSelector('[data-testid="nav-chat"]', { timeout: 20_000 });
  } catch (error) {
    const html = await page.content();
    throw new Error(
      [
        `boot failed: status=${response?.status()}`,
        `url=${page.url()}`,
        `console=${JSON.stringify(evidence.consoleErrors.slice(0, 10), null, 2)}`,
        `failedRequests=${JSON.stringify(evidence.failedRequests.slice(0, 10), null, 2)}`,
        `dom=${html.slice(0, 1200)}`,
        String(error),
      ].join('\n'),
    );
  }
}

/** Navigate by sidebar test id. */
export async function goTo(page: Page, pageId: string): Promise<void> {
  await page.click(`[data-testid="nav-${pageId}"]`);
  await expect(page.locator(`[data-testid="nav-${pageId}"]`)).toHaveAttribute('aria-current', 'page');
}

/**
 * Assert no element overflows the viewport horizontally.
 *
 * Reads real geometry (`getBoundingClientRect`), not text: 1.0 asserted an element
 * *existed* and missed an inspector whose right edge sat 152 px outside its
 * container.
 */
export async function expectNoHorizontalOverflow(page: Page): Promise<void> {
  const overflow = await page.evaluate(() => {
    const viewport = window.innerWidth;
    const offenders: string[] = [];
    for (const node of document.querySelectorAll('[data-testid], header, footer, main, nav')) {
      const rect = node.getBoundingClientRect();
      if (rect.width === 0) continue;
      if (rect.right > viewport + 1 || rect.left < -1) {
        offenders.push(
          `${node.tagName.toLowerCase()}[${(node as HTMLElement).dataset.testid ?? ''}] left=${Math.round(
            rect.left,
          )} right=${Math.round(rect.right)} viewport=${viewport}`,
        );
      }
    }
    return {
      bodyOverflow: document.body.scrollWidth > viewport,
      offenders,
    };
  });
  expect(overflow.bodyOverflow, `body scrolls horizontally: ${overflow.offenders.join('; ')}`).toBe(
    false,
  );
}
