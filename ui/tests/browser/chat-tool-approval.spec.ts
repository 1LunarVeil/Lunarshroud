import { expect, test } from '@playwright/test';
import { boot, collectEvidence, goTo } from './helpers';

/**
 * Chat tool calls must be **approved**, never executed on arrival.
 *
 * The 2026-11-23 frontend audit (H5) found that `send()` ran every `tool_calls`
 * entry inline right after the reply, so the approval bar was decoration: a model
 * could read any file in the authorized folder without the user ever seeing the
 * call. The same audit found no UI at all for authorizing a folder, which made the
 * whole read-only tool path unreachable instead.
 *
 * These tests drive the real page against the real backend. The assistant message
 * is written through the API (a model would be needed to produce one), then the
 * page is asked to render it — which is exactly the state a user reaches by
 * reloading a conversation that contains tool calls.
 *
 * Requests go through Playwright's `request` fixture with **relative** paths: the
 * configured `baseURL` is the backend under test, so no host is assembled here.
 */

test('chat shows an approval bar and does not run the tool on render', async ({ page, request }) => {
  const evidence = collectEvidence(page);

  const created = await request.post('/api/chats', {
    data: { title: '', instance_id: null, model: null },
  });
  expect(created.status()).toBe(200);
  const conversation = (await created.json()) as { id: string };

  // Authorize a folder for this conversation. Without one the tool block correctly
  // refuses the call with "authorize a folder" instead of offering an approval —
  // so a test that skipped this step would be asserting the wrong state.
  const authorized = await request.patch(`/api/chats/${conversation.id}`, {
    data: { tool_root: process.env.LUNARSHROUD_TEST_TOOL_ROOT || process.cwd() },
  });
  expect(authorized.status()).toBe(200);

  // A real turn: the user asks, the model replies with a tool request. The user
  // message is written first because that is what gives the conversation its title
  // (`chat_store` derives it from the first user content) — and because a
  // tool-result pairing only makes sense after a user turn.
  const asked = await request.post(`/api/chats/${conversation.id}/messages`, {
    data: { role: 'user', content: 'Please read notes.txt for me.' },
  });
  expect(asked.status()).toBe(200);

  // A tool request as it would arrive from the model, persisted with the reply.
  const appended = await request.post(`/api/chats/${conversation.id}/messages`, {
    data: {
      role: 'assistant',
      content: 'Let me look at that file.',
      stats: {
        tool_calls: [
          {
            id: 'call_pending_1',
            type: 'function',
            function: { name: 'read_file', arguments: '{"path":"notes.txt"}' },
          },
        ],
      },
    },
  });
  expect(appended.status()).toBe(200);

  await boot(page, evidence);
  await goTo(page, 'chat');

  // Open the conversation from the sidebar. The row shows the auto-title derived
  // from the user message.
  await page.getByRole('button', { name: /Please read notes\.txt for me\./ }).first().click();
  await page.getByText('Let me look at that file.').first().waitFor({ timeout: 15_000 });

  // The call is visible with its resolved target, and it is still pending.
  const toolCalls = page.locator('[data-testid="tool-calls"]');
  await expect(toolCalls).toBeVisible();
  await expect(toolCalls).toContainText('read_file');

  // No result block exists: the tool has not run.
  await expect(page.locator('[data-testid="tool-result"]')).toHaveCount(0);

  // Both decisions are offered. Without them the bar was previously dead markup.
  await expect(toolCalls.getByRole('button', { name: /允许执行|Allow/ })).toBeVisible();
  await expect(toolCalls.getByRole('button', { name: /拒绝|Deny/ })).toBeVisible();

  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors)).toEqual([]);
});

test('the tool-folder bar is reachable when an instance exists', async ({ page, request }) => {
  const evidence = collectEvidence(page);
  await boot(page, evidence);
  await goTo(page, 'chat');

  const bar = page.locator('[data-testid="tool-folder-bar"]');
  const instances = await request.get('/api/instances');
  const running = ((await instances.json()) as Array<{ status: string }>).filter(
    (instance) => instance.status === 'running',
  );
  if (running.length === 0) {
    // No instance in this environment: the composer is disabled and the bar is
    // correctly absent. Asserting its presence here would be asserting a bug.
    await expect(bar).toHaveCount(0);
    await expect(page.locator('[data-testid="chat-empty-load-model"]')).toBeVisible();
    expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors)).toEqual([]);
    return;
  }
  await expect(bar).toBeVisible();
  await expect(bar.getByRole('button', { name: /工具目录|Tool folder/ })).toBeVisible();
  expect(evidence.consoleErrors, JSON.stringify(evidence.consoleErrors)).toEqual([]);
});
