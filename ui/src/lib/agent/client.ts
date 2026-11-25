/**
 * Agent workbench client.
 *
 * The run is a server-side state machine: this module starts it, follows its event
 * stream, and answers approvals. It deliberately holds no loop of its own — a client
 * that reimplemented the turn/tool cycle would diverge from the server's, and the
 * approval gate only exists on the server.
 */

import { apiGet, apiPost, ensureBackendSession } from '@/lib/api/client';

/** Event types the workbench renders. Mirrors `core/agent_state.py::EVENT_TYPES`. */
export type AgentEventType =
  | 'turn_started'
  | 'assistant_message'
  | 'tool_call'
  | 'approval_requested'
  | 'tool_started'
  | 'tool_result'
  | 'tool_denied'
  | 'error'
  | 'done'
  | 'cancelled';

export interface AgentToolPreview {
  tool: string;
  ok: boolean;
  risk: 'normal' | 'write' | 'command' | 'high' | string;
  summary: string;
  reason?: string;
  path?: string;
  argv?: string[];
  cwd?: string | null;
  bytes?: number;
  /** First ~400 characters of a `write_file` body, elided with a marker when longer. */
  content?: string;
  content_chars?: number;
  replace_all?: boolean;
  preview?: { old: string; new: string };
}

export interface AgentEvent {
  seq: number;
  at: number;
  type: AgentEventType | 'end';
  [key: string]: unknown;
}

export interface AgentRunSnapshot {
  id: string;
  conversation_id: string;
  status: 'running' | 'done' | 'failed' | 'cancelled';
  error: string | null;
  turns: number;
  tool_calls: number;
  started_at: number;
  events: AgentEvent[];
}

export interface AgentToolInfo {
  name: string;
  description: string;
  parameters: Record<string, unknown>;
  risk: 'normal' | 'write' | 'command' | string;
  requires_approval: boolean;
}

export type ApprovalDecision = 'allow_once' | 'allow_session' | 'deny';

export interface StartRunInput {
  conversationId: string;
  instanceId: string;
  root: string;
  messages: Array<{ role: 'user' | 'assistant' | 'system'; content: string }>;
  params?: Record<string, unknown>;
  systemPrompt?: string;
}

export const agentApi = {
  tools: () => apiGet<{ tools: AgentToolInfo[] }>('/api/agent/tools'),

  start: (input: StartRunInput) =>
    apiPost<{ run_id: string; status: string }>('/api/agent/runs', {
      conversation_id: input.conversationId,
      instance_id: input.instanceId,
      root: input.root,
      messages: input.messages,
      params: input.params ?? {},
      system_prompt: input.systemPrompt ?? '',
    }),

  snapshot: (runId: string) => apiGet<AgentRunSnapshot>(`/api/agent/runs/${encodeURIComponent(runId)}`),

  approve: (runId: string, approvalId: string, decision: ApprovalDecision) =>
    apiPost<{ resolved: boolean; decision: string }>(
      `/api/agent/runs/${encodeURIComponent(runId)}/approve`,
      { approval_id: approvalId, decision },
    ),

  cancel: (runId: string) =>
    apiPost<{ cancelled: boolean }>(`/api/agent/runs/${encodeURIComponent(runId)}/cancel`, {}),
};

/**
 * Follow a run's event stream.
 *
 * Cursor-based replay: `after` is the last sequence number seen, so a dropped
 * connection resumes with no gap and no duplicate. The server ends the stream with
 * `{type:'end'}`.
 *
 * **Base URL.** The default is the resolved backend session, not `''`. In the
 * packaged build the page origin is `tauri.localhost` while the backend listens on
 * loopback, so a relative URL silently addressed the wrong server — the stream only
 * worked in the same-origin source-run channel (audit L6). The session is the same
 * one `apiGet`/`apiPost` use, including the token, so this path gets identical
 * authentication and no hardcoded port.
 *
 * **Reconnect.** A transport failure used to return the error and stop, leaving
 * `after` replay unreachable — the docstring promised resume and the code had none.
 * It now retries a bounded number of times with backoff, resuming from `cursor`.
 */
export function followRun(
  runId: string,
  handlers: {
    onEvent: (event: AgentEvent) => void;
    onEnd?: (status: string) => void;
    onTransportError?: (message: string) => void;
  },
  options: { after?: number; signal?: AbortSignal; baseUrl?: string; token?: string } = {},
): () => void {
  const controller = new AbortController();
  const signal = options.signal ?? controller.signal;
  let cursor = options.after ?? 0;
  let finished = false;

  const RETRY_DELAYS_MS = [300, 900, 2000] as const;

  const readStream = async (base: string, token: string): Promise<'ended' | 'retry' | 'stop'> => {
    const path = `/api/agent/runs/${encodeURIComponent(runId)}/events?after=${cursor}`;
    const response = await fetch(`${base}${path}`, {
      headers: {
        Accept: 'text/event-stream',
        ...(token ? { 'X-LunarShroud-Token': token } : {}),
      },
      signal,
    });
    if (!response.ok) {
      handlers.onTransportError?.(`HTTP ${response.status}`);
      // 4xx means this run will never stream (wrong id, expired); retrying loops.
      return response.status >= 500 ? 'retry' : 'stop';
    }
    const reader = response.body?.getReader();
    if (!reader) {
      handlers.onTransportError?.('response has no body');
      return 'stop';
    }
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { done, value } = await reader.read();
      // A stream that ends **without** an `end` event is a dropped connection, not
      // a finished run: the server always terminates with `{type:'end'}`. Treating
      // EOF as success is how a reconnect never happens even though the code looks
      // like it has one (caught by `tests/agentClient.test.ts`).
      if (done) return finished ? 'stop' : 'retry';
      buffer += decoder.decode(value, { stream: true }).replace(/\r\n?/g, '\n');
      let index = buffer.indexOf('\n\n');
      while (index !== -1) {
        const block = buffer.slice(0, index);
        buffer = buffer.slice(index + 2);
        for (const line of block.split('\n')) {
          if (!line.startsWith('data:')) continue;
          const payload = line.slice(5).trim();
          if (!payload) continue;
          try {
            const event = JSON.parse(payload) as AgentEvent;
            if (event.type === 'end') {
              finished = true;
              handlers.onEnd?.(String((event as unknown as { status?: string }).status ?? 'done'));
              return 'stop';
            }
            // Advance before dispatch: if the handler throws, replaying this event
            // would be a duplicate on the next connection.
            cursor = event.seq;
            handlers.onEvent(event);
          } catch {
            // A malformed frame must not kill the stream; the next event recovers.
          }
        }
        index = buffer.indexOf('\n\n');
      }
    }
  };

  const run = async () => {
    let base = options.baseUrl;
    let token = options.token;
    if (base === undefined) {
      const session = await ensureBackendSession();
      base = session.url;
      token = token ?? session.token;
    }
    for (let attempt = 0; ; attempt += 1) {
      const outcome = await readStream(base, token ?? '');
      if (outcome !== 'retry' || finished || signal.aborted) return;
      const delay = RETRY_DELAYS_MS[attempt];
      if (delay === undefined) return;
      await new Promise((resolve) => setTimeout(resolve, delay));
      if (signal.aborted) return;
    }
  };

  void run().catch((error: unknown) => {
    if (signal.aborted) return;
    handlers.onTransportError?.(error instanceof Error ? error.message : 'stream failed');
  });

  return () => controller.abort();
}
