import { afterEach, describe, expect, it, vi } from 'vitest';
import { followRun, type AgentEvent } from '@/lib/agent/client';

/**
 * `followRun` transport behaviour.
 *
 * Two defects from the 2026-11-23 frontend audit (L6) are pinned here:
 *
 * 1. **Base URL.** `baseUrl` defaulted to `''`, so the request went to the page
 *    origin. In the packaged build that is `tauri.localhost`, not the backend, so
 *    the agent stream only worked in the same-origin source-run channel.
 * 2. **Reconnect.** `after` replay was unreachable: the function returned on the
 *    first transport error, so a dropped connection ended the stream even though the
 *    server supports resuming from a cursor.
 *
 * `fetch` is stubbed, so this is a unit test of the client's own control flow —
 * the real server behaviour is covered by `tests/e2e/agent-e2e.mjs`.
 */

function sseBlock(payload: unknown): string {
  return `data: ${JSON.stringify(payload)}\n\n`;
}

function streamOf(chunks: string[]): Response {
  const encoder = new TextEncoder();
  let index = 0;
  return {
    ok: true,
    status: 200,
    body: {
      getReader() {
        return {
          async read() {
            if (index >= chunks.length) return { done: true, value: undefined };
            const value = encoder.encode(chunks[index]);
            index += 1;
            return { done: false, value };
          },
        };
      },
    },
  } as unknown as Response;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

describe('followRun', () => {
  it('addresses the resolved backend origin, not the page origin', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url);
        return streamOf([sseBlock({ seq: 1, type: 'end', at: 0 })]);
      }),
    );

    const stop = followRun('run_abc', { onEvent: () => {} }, {
      baseUrl: 'http://127.0.0.1:19999',
      token: 'tok',
    });
    await vi.waitFor(() => expect(urls.length).toBeGreaterThan(0));
    stop();

    expect(urls[0]).toBe('http://127.0.0.1:19999/api/agent/runs/run_abc/events?after=0');
  });

  it('falls back to the resolved backend session when no base is given', async () => {
    // The packaged build's page origin is `tauri.localhost`; a relative URL there
    // addresses the WebView's own asset server, so the stream must resolve the
    // backend explicitly. Browser mode (jsdom here) resolves to the page origin,
    // which is the backend in the source-run channel — either way it must not be
    // the empty string, which produced a bare `/api/...` request.
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url);
        return streamOf([sseBlock({ seq: 1, type: 'end', at: 0 })]);
      }),
    );

    const stop = followRun('run_default', { onEvent: () => {} });
    await vi.waitFor(() => expect(urls.length).toBeGreaterThan(0));
    stop();

    expect(urls[0]).not.toMatch(/^\/api\//);
    expect(urls[0]).toMatch(/^https?:\/\/[^/]+\/api\/agent\/runs\/run_default\/events/);
  });

  it('sends the session token as a header, never in the URL', async () => {
    let called: { url: string; init?: RequestInit } | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init?: RequestInit) => {
        called = { url, init };
        return streamOf([sseBlock({ seq: 1, type: 'end', at: 0 })]);
      }),
    );

    const stop = followRun('run_abc', { onEvent: () => {} }, {
      baseUrl: 'http://127.0.0.1:19999',
      token: 'secret-token',
    });
    await vi.waitFor(() => expect(called).not.toBeNull());
    stop();

    const seen = called as unknown as { url: string; init: RequestInit };
    expect(seen.url).not.toContain('secret-token');
    expect((seen.init.headers as Record<string, string>)['X-LunarShroud-Token']).toBe(
      'secret-token',
    );
  });

  it('resumes from the last seen sequence number after a dropped connection', async () => {
    const urls: string[] = [];
    let attempt = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(url);
        attempt += 1;
        if (attempt === 1) {
          // First connection delivers two events, then dies mid-stream.
          return streamOf([
            sseBlock({ seq: 1, type: 'turn_started', at: 0, turn: 1 }),
            sseBlock({ seq: 2, type: 'assistant_message', at: 0, content: 'hi' }),
          ]);
        }
        return streamOf([sseBlock({ seq: 3, type: 'end', at: 0, status: 'done' })]);
      }),
    );

    const seen: number[] = [];
    const ended = new Promise<string>((resolve) => {
      const stop = followRun(
        'run_resume',
        {
          onEvent: (event: AgentEvent) => seen.push(event.seq),
          onEnd: (status) => resolve(status),
        },
        { baseUrl: 'http://127.0.0.1:19999' },
      );
      // The retry backoff is 300 ms; the promise below is resolved by onEnd.
      void stop;
    });

    expect(await ended).toBe('done');
    expect(seen).toEqual([1, 2]);
    // The second request resumed at 2 — no gap, no duplicate.
    expect(urls[1]).toContain('after=2');
  });

  it('does not retry a 4xx, which can never succeed', async () => {
    let calls = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        calls += 1;
        return { ok: false, status: 404, body: null } as unknown as Response;
      }),
    );

    const errors: string[] = [];
    const stop = followRun(
      'run_missing',
      { onEvent: () => {}, onTransportError: (message) => errors.push(message) },
      { baseUrl: 'http://127.0.0.1:19999' },
    );
    await vi.waitFor(() => expect(errors.length).toBe(1));
    stop();

    expect(calls).toBe(1);
    expect(errors[0]).toContain('404');
  });
});
