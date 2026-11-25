/**
 * Pure SSE and streaming helpers for the native chat stream.
 *
 * Ported verbatim (behaviour-for-behaviour) from the 1.0 module
 * `frontend/js/chat_stream.mjs`, which is covered by tests written against real
 * llama.cpp behaviour. The logic is deliberately unchanged: these functions
 * encode measured facts about how llama-server streams.
 *
 * llama-server's OpenAI-compatible endpoint sends `data: {json}` events and a
 * final `data: [DONE]`. Chunks can split anywhere, so the parser keeps a buffer
 * and only emits complete events.
 */

import type { ChatToolCall } from '@/lib/api/types';

export type SseEvent =
  | { done: true }
  | { data: unknown }
  | { raw: string };

export interface SseParser {
  push: (text: string) => SseEvent[];
  flush: () => SseEvent[];
}

function parseBlock(block: string): SseEvent | null {
  const dataLines: string[] = [];
  for (const line of block.split('\n')) {
    if (line.startsWith(':')) continue; // comment / keep-alive
    if (!line.startsWith('data:')) continue;
    dataLines.push(line.slice(5).replace(/^ /, ''));
  }
  if (!dataLines.length) return null;
  const data = dataLines.join('\n');
  if (data === '[DONE]') return { done: true };
  try {
    return { data: JSON.parse(data) as unknown };
  } catch {
    return { raw: data };
  }
}

export function createSseParser(): SseParser {
  let buffer = '';

  const drain = (): SseEvent[] => {
    const events: SseEvent[] = [];
    let index = buffer.indexOf('\n\n');
    while (index !== -1) {
      const block = buffer.slice(0, index);
      buffer = buffer.slice(index + 2);
      const parsed = parseBlock(block);
      if (parsed) events.push(parsed);
      index = buffer.indexOf('\n\n');
    }
    return events;
  };

  return {
    push(text: string) {
      buffer += String(text ?? '').replace(/\r\n?/g, '\n');
      return drain();
    },
    flush() {
      const events: SseEvent[] = [];
      if (buffer.trim()) {
        const parsed = parseBlock(buffer);
        if (parsed) events.push(parsed);
      }
      buffer = '';
      return events;
    },
  };
}

export interface NormalizedChunk {
  content: string;
  reasoning: string;
  toolCalls: ChatToolCall[];
  finishReason: string | null;
  usage: Record<string, number> | null;
  timings: Record<string, number> | null;
  error: string | null;
}

interface RawDelta {
  content?: unknown;
  reasoning_content?: unknown;
  reasoning?: unknown;
  tool_calls?: unknown;
}

interface RawChoice {
  delta?: unknown;
  finish_reason?: unknown;
}

interface RawPayload {
  choices?: unknown;
  usage?: unknown;
  timings?: unknown;
  error?: unknown;
}

/**
 * Normalize one OpenAI-compatible chunk.
 *
 * llama.cpp may put reasoning text in `reasoning_content`; usage/timings can
 * arrive on the final chunk. Tool calls stream **incrementally** — the same
 * `index` appears in many chunks, each carrying a fragment of `arguments`
 * (measured against b11698: `{"` → `"pattern":"` → `*` → `gg` → `uf` …).
 * All fields are optional.
 */
export function extractChunk(payload: unknown): NormalizedChunk | null {
  if (!payload || typeof payload !== 'object') return null;
  const raw = payload as RawPayload;
  const choice = Array.isArray(raw.choices) ? (raw.choices[0] as RawChoice | undefined) : undefined;
  const delta =
    choice && typeof choice.delta === 'object' && choice.delta !== null
      ? (choice.delta as RawDelta)
      : {};
  const error = raw.error;
  return {
    content: typeof delta.content === 'string' ? delta.content : '',
    reasoning:
      typeof delta.reasoning_content === 'string'
        ? delta.reasoning_content
        : typeof delta.reasoning === 'string'
          ? delta.reasoning
          : '',
    // Raw deltas: `accumulateToolCalls` merges them by index.
    toolCalls: Array.isArray(delta.tool_calls) ? (delta.tool_calls as ChatToolCall[]) : [],
    finishReason: choice && choice.finish_reason ? String(choice.finish_reason) : null,
    usage:
      raw.usage && typeof raw.usage === 'object'
        ? (raw.usage as Record<string, number>)
        : null,
    timings:
      raw.timings && typeof raw.timings === 'object'
        ? (raw.timings as Record<string, number>)
        : null,
    error: error
      ? String(
          typeof error === 'object' && error !== null && 'message' in error
            ? (error as { message: unknown }).message
            : error,
        )
      : null,
  };
}

interface ToolCallDelta {
  index?: unknown;
  id?: unknown;
  type?: unknown;
  function?: { name?: unknown; arguments?: unknown };
}

/**
 * Merge incremental `tool_calls` deltas into complete calls.
 *
 * Why this needs its own function: the server sends only **a fragment** of the
 * argument string per frame, keyed by `index`. Overwriting loses all but the last
 * character; pushing produces a dozen same-named broken calls. Measured on
 * b11698, `arguments` arrives character-by-character or word-by-word.
 *
 * `id` and `name` appear only in the first fragment, so the merge must be
 * "write when present", never "overwrite with the new value".
 */
export function accumulateToolCalls(
  existing: ChatToolCall[] | null | undefined,
  deltas: unknown,
): ChatToolCall[] {
  const list: ChatToolCall[] = Array.isArray(existing) ? existing : [];
  for (const delta of Array.isArray(deltas) ? (deltas as ToolCallDelta[]) : []) {
    if (!delta || typeof delta !== 'object') continue;
    const index = Number.isInteger(delta.index) ? (delta.index as number) : 0;
    while (list.length <= index) {
      list.push({ id: '', type: 'function', function: { name: '', arguments: '' } });
    }
    const target = list[index];
    if (!target) continue;
    if (!target.function) target.function = { name: '', arguments: '' };
    if (typeof delta.id === 'string' && delta.id) target.id = delta.id;
    if (typeof delta.type === 'string' && delta.type) {
      target.type = delta.type as ChatToolCall['type'];
    }
    const fn = delta.function;
    if (fn && typeof fn === 'object') {
      if (typeof fn.name === 'string' && fn.name) target.function.name = fn.name;
      if (typeof fn.arguments === 'string' && fn.arguments) {
        target.function.arguments += fn.arguments;
      }
    }
  }
  return list;
}

/**
 * Parse a completed argument string.
 *
 * Mid-stream parsing necessarily fails (the JSON is not written yet), so this
 * returns `null` instead of throwing: the UI has to show "preparing to call X"
 * while arguments are still incomplete, and callers only read the parsed value
 * after the call completes.
 */
export function parseToolArguments(text: string | null | undefined): Record<string, unknown> | null {
  const raw = String(text ?? '').trim();
  if (!raw) return {};
  try {
    const parsed: unknown = JSON.parse(raw);
    return parsed && typeof parsed === 'object'
      ? (parsed as Record<string, unknown>)
      : null;
  } catch {
    return null;
  }
}

export interface ContextUsage {
  used: number | null;
  total: number;
  ratio: number;
  nearLimit: boolean;
}

interface UsageMessage {
  role?: unknown;
  stats?: Record<string, unknown> | null;
}

/**
 * Approximate how much of the instance context the latest turn occupies.
 *
 * llama-server reports usage on the final SSE chunk. `total_tokens` covers the
 * prompt plus the generated reply, which is exactly what the next request must
 * fit again. Older or interrupted turns may only have component counters; keep
 * those as a fallback. `used` is `null` until a response has real stats, rather
 * than pretending a character count is a token count.
 */
export function contextUsage(
  messages: UsageMessage[] | null | undefined,
  contextSize: number | string | null | undefined,
): ContextUsage {
  const total = Number(contextSize);
  const limit = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  let used: number | null = null;
  if (Array.isArray(messages)) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (!message || message.role !== 'assistant') continue;
      const stats = message.stats && typeof message.stats === 'object' ? message.stats : {};
      const totalTokens = stats.total_tokens;
      if (typeof totalTokens === 'number' && Number.isFinite(totalTokens) && totalTokens > 0) {
        used = totalTokens;
        break;
      }
      const prompt =
        typeof stats.prompt_tokens === 'number' && Number.isFinite(stats.prompt_tokens)
          ? stats.prompt_tokens
          : typeof stats.prompt_n === 'number' && Number.isFinite(stats.prompt_n)
            ? stats.prompt_n
            : 0;
      const completion =
        typeof stats.completion_tokens === 'number' && Number.isFinite(stats.completion_tokens)
          ? stats.completion_tokens
          : typeof stats.predicted_n === 'number' && Number.isFinite(stats.predicted_n)
            ? stats.predicted_n
            : 0;
      if (prompt > 0 || completion > 0) {
        used = prompt + completion;
        break;
      }
    }
  }
  const ratio = limit && used !== null ? Math.min(1, used / limit) : 0;
  return { used, total: limit, ratio, nearLimit: ratio >= 0.85 };
}

export interface ChatStats {
  elapsed_ms: number;
  chars: number;
  reasoning_chars?: number;
  prompt_tokens?: number;
  completion_tokens?: number;
  total_tokens?: number;
  predicted_n?: number;
  predicted_per_second?: number;
  prompt_n?: number;
}

/**
 * Merge the fields we care about from a final chunk into a message's stats.
 *
 * Reasoning text is deliberately **not** stored here: `stats_json` has a 16 KiB
 * budget, so prose in stats would be silently truncated at write time. It is a
 * first-class `messages.reasoning` column instead.
 */
export function buildChatStats({
  usage = null,
  timings = null,
  elapsedMs = 0,
  content = '',
  reasoningChars = 0,
}: {
  usage?: Record<string, number> | null;
  timings?: Record<string, number> | null;
  elapsedMs?: number;
  content?: string;
  reasoningChars?: number;
} = {}): ChatStats {
  const stats: ChatStats = {
    elapsed_ms: Math.max(0, Math.round(elapsedMs)),
    chars: content.length,
  };
  if (reasoningChars) stats.reasoning_chars = reasoningChars;
  if (usage && typeof usage === 'object') {
    if (typeof usage.prompt_tokens === 'number' && Number.isFinite(usage.prompt_tokens)) {
      stats.prompt_tokens = usage.prompt_tokens;
    }
    if (typeof usage.completion_tokens === 'number' && Number.isFinite(usage.completion_tokens)) {
      stats.completion_tokens = usage.completion_tokens;
    }
    if (typeof usage.total_tokens === 'number' && Number.isFinite(usage.total_tokens)) {
      stats.total_tokens = usage.total_tokens;
    }
  }
  if (timings && typeof timings === 'object') {
    if (typeof timings.predicted_n === 'number' && Number.isFinite(timings.predicted_n)) {
      stats.predicted_n = timings.predicted_n;
    }
    if (
      typeof timings.predicted_per_second === 'number' &&
      Number.isFinite(timings.predicted_per_second)
    ) {
      stats.predicted_per_second = timings.predicted_per_second;
    }
    if (typeof timings.prompt_n === 'number' && Number.isFinite(timings.prompt_n)) {
      stats.prompt_n = timings.prompt_n;
    }
  }
  return stats;
}

export type ReasoningMode = 'auto' | 'expand' | 'collapse';

export interface ReasoningPanelState {
  mode?: ReasoningMode | string;
  streaming?: boolean;
  touched?: boolean;
  chosen?: boolean;
  hasReasoning?: boolean;
}

/**
 * Decide whether a message's reasoning panel is open.
 *
 * Precedence is the part that matters: **a manual toggle always beats the
 * automatic behaviour**. Without it, "expand while thinking, collapse when done"
 * would snap shut on the very message the user just opened to read — and because
 * the list re-renders on every chunk, that reads as a broken control rather than
 * as a policy.
 *
 * An unknown `mode` is treated as `auto`, so a corrupt setting degrades to the
 * default instead of hiding the panel forever.
 */
export function reasoningPanelOpen({
  mode = 'auto',
  streaming = false,
  touched = false,
  chosen = false,
  hasReasoning = false,
}: ReasoningPanelState = {}): boolean {
  if (!hasReasoning) return false;
  if (touched) return Boolean(chosen);
  if (mode === 'expand') return true;
  if (mode === 'collapse') return false;
  return Boolean(streaming);
}

export type ReasoningUpdateAction = 'create' | 'update' | 'none';

export interface ReasoningUpdatePlan {
  action: ReasoningUpdateAction;
  open: boolean;
}

/**
 * How a streaming update should touch the reasoning panel.
 *
 * The distinction exists because of a real regression: the streaming updater
 * only refreshed the content node and never touched the reasoning panel, so the
 * whole thinking phase showed a fixed "thinking…" shell with no progress —
 * which users read as a hang.
 *
 * The React port keeps the same three-way split but for a different reason: the
 * panel is now a component, so `create`/`update` decide whether the panel is
 * mounted at all on the first frame that has reasoning text.
 */
export function planReasoningUpdate({
  hasPanel = false,
  shouldOpen = false,
  textLength = 0,
  renderedLength = null,
}: {
  hasPanel?: boolean;
  shouldOpen?: boolean;
  textLength?: number;
  renderedLength?: number | string | null;
} = {}): ReasoningUpdatePlan {
  if (!textLength) return { action: 'none', open: false };
  if (!hasPanel) return { action: 'create', open: Boolean(shouldOpen) };
  if (renderedLength !== null && String(renderedLength) === String(textLength)) {
    return { action: 'none', open: Boolean(shouldOpen) };
  }
  return { action: 'update', open: Boolean(shouldOpen) };
}
