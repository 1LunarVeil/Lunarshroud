import { expect, test } from '@playwright/test';
import { boot, collectEvidence, goTo } from './helpers';

/**
 * Accessibility gate.
 *
 * Runs axe in **both themes** against the pages that hold the most distinct
 * components. Two things this does that a naive gate misses:
 *
 * 1. It waits for finite animations to settle. 1.0's gate measured a colour
 *    mid-`page-fade-in` and reported 4.42:1 for a token that was actually 5.13:1 —
 *    the violation *count* varied between runs (0/2/4/5), which is the signature of
 *    a race rather than a defect.
 * 2. It names the selector, the HTML and the measured ratio for each violation, so
 *    a failure is diagnosable without re-running.
 */
const PAGES = ['chat', 'dashboard', 'models', 'knowledge', 'serving', 'observability'] as const;

type AxeViolation = {
  id: string;
  impact: string | null;
  description: string;
  nodes: Array<{ target: string[]; html: string; failureSummary?: string }>;
};

async function runAxe(page: import('@playwright/test').Page): Promise<AxeViolation[]> {
  // Serve axe from the app's **own origin** so the CSP (`script-src 'self'`) is
  // honoured rather than disabled. It is intercepted here instead of copied into
  // `public/`: shipping a 560 KB auditor inside the release binary to satisfy a
  // test would be a real cost for a test-only dependency.
  await page.route('**/axe.min.js', async (route) => {
    const { readFile } = await import('node:fs/promises');
    const { createRequire } = await import('node:module');
    const require = createRequire(import.meta.url);
    const source = await readFile(require.resolve('axe-core/axe.min.js'), 'utf8');
    await route.fulfill({
      status: 200,
      contentType: 'application/javascript',
      body: source,
    });
  });
  await page.addScriptTag({ url: '/axe.min.js' });
  return page.evaluate(async () => {
    const axe = (window as unknown as { axe: { run: (ctx: unknown, opts: unknown) => Promise<{ violations: AxeViolation[] }> } }).axe;
    const result = await axe.run(document, {
      runOnly: { type: 'tag', values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa'] },
      rules: {
        // The app is a desktop window, not a document; region/landmark rules assume
        // a page structure with a contentinfo etc. Landmarks that exist are checked.
        region: { enabled: false },
      },
    });
    return result.violations;
  });
}

function describeViolations(violations: AxeViolation[]): string {
  return violations
    .map((violation) => {
      const nodes = violation.nodes
        .slice(0, 4)
        .map((node) => `    ${node.target.join(' ')} :: ${node.html.slice(0, 160)}`)
        .join('\n');
      return `${violation.id} (${violation.impact}): ${violation.description}\n${nodes}`;
    })
    .join('\n\n');
}

/** Let finite animations finish; ignore infinite ones (a spinner never settles). */
async function settle(page: import('@playwright/test').Page): Promise<void> {
  await page.evaluate(async () => {
    const finite = document.getAnimations().filter((animation) => {
      const timing = animation.effect?.getComputedTiming();
      return timing?.iterations !== Infinity;
    });
    await Promise.all(
      finite.map((animation) => animation.finished.catch(() => undefined)),
    );
  });
}

for (const theme of ['light', 'dark'] as const) {
  test(`no WCAG A/AA violations in ${theme} theme @a11y`, async ({ page }) => {
    const evidence = collectEvidence(page);
    await boot(page, evidence);
    if (theme === 'dark') {
      await page.evaluate(() => document.documentElement.classList.add('theme-dark'));
    } else {
      await page.evaluate(() => document.documentElement.classList.remove('theme-dark'));
    }

    const failures: string[] = [];
    for (const pageId of PAGES) {
      await goTo(page, pageId);
      await page.waitForTimeout(250);
      await settle(page);
      const violations = await runAxe(page);
      const blocking = violations.filter(
        (violation) => violation.impact === 'critical' || violation.impact === 'serious',
      );
      if (blocking.length) {
        failures.push(`[${pageId}]\n${describeViolations(blocking)}`);
      }
    }

    expect(failures.join('\n\n'), `accessibility violations (${theme}):\n${failures.join('\n\n')}`).toBe('');
    expect(evidence.consoleErrors, evidence.consoleErrors.join('\n')).toEqual([]);
  });
}

test('keyboard reaches every sidebar destination @a11y', async ({ page }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);

  // Tab from the top of the document and record the aria-labels/titles reached.
  const reached: string[] = [];
  for (let step = 0; step < 40; step += 1) {
    await page.keyboard.press('Tab');
    const descriptor = await page.evaluate(() => {
      const active = document.activeElement as HTMLElement | null;
      if (!active) return '';
      return (
        active.dataset.testid ||
        active.getAttribute('aria-label') ||
        active.textContent?.trim().slice(0, 40) ||
        active.tagName
      );
    });
    if (descriptor) reached.push(descriptor);
  }

  for (const pageId of ['chat', 'dashboard', 'models', 'knowledge', 'serving']) {
    expect(
      reached.some((entry) => entry.includes(`nav-${pageId}`) || entry.includes(pageId)),
      `keyboard never reached nav-${pageId}; reached: ${reached.slice(0, 20).join(', ')}`,
    ).toBe(true);
  }

  // The focus ring must be a real outline, not a colour change on hover.
  const outline = await page.evaluate(() => {
    const active = document.activeElement as HTMLElement | null;
    if (!active) return null;
    const style = getComputedStyle(active);
    return { width: style.outlineWidth, style: style.outlineStyle };
  });
  expect(outline?.style).not.toBe('none');
});
