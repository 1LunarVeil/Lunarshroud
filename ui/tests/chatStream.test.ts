import { describe, expect, it } from 'vitest';
import {
  accumulateToolCalls,
  buildChatStats,
  contextUsage,
  createSseParser,
  extractChunk,
  parseToolArguments,
  planReasoningUpdate,
  reasoningPanelOpen,
} from '@/lib/ported/chatStream';

/**
 * These cases are ported from the 1.0 `chat.test.mjs` suite, which was written
 * against real llama.cpp output. Their value is that they encode **measured**
 * framing behaviour; if a refactor "simplifies" the parser, they fail.
 */

describe('createSseParser', () => {
  it('emits only complete events when a chunk splits mid-frame', () => {
    const parser = createSseParser();
    expect(parser.push('data: {"a":1}')).toEqual([]);
    expect(parser.push('\n\ndata: {"b":2}\n\n')).toEqual([{ data: { a: 1 } }, { data: { b: 2 } }]);
  });

  it('normalizes CRLF and lone CR to LF', () => {
    const parser = createSseParser();
    expect(parser.push('data: {"a":1}\r\n\r\n')).toEqual([{ data: { a: 1 } }]);
  });

  it('ignores keep-alive comments', () => {
    const parser = createSseParser();
    expect(parser.push(': keep-alive\n\n')).toEqual([]);
  });

  it('recognizes the [DONE] sentinel', () => {
    const parser = createSseParser();
    expect(parser.push('data: [DONE]\n\n')).toEqual([{ done: true }]);
  });

  it('joins multiple data lines in one event', () => {
    const parser = createSseParser();
    expect(parser.push('data: {"a":\ndata: 1}\n\n')).toEqual([{ data: { a: 1 } }]);
  });

  it('returns raw text when the payload is not JSON', () => {
    const parser = createSseParser();
    expect(parser.push('data: not-json\n\n')).toEqual([{ raw: 'not-json' }]);
  });

  it('flush drains a trailing event that never got its blank line', () => {
    const parser = createSseParser();
    parser.push('data: {"a":1}');
    expect(parser.flush()).toEqual([{ data: { a: 1 } }]);
    // A second flush must not repeat it.
    expect(parser.flush()).toEqual([]);
  });
});

describe('extractChunk', () => {
  it('reads content, reasoning_content and reasoning', () => {
    expect(
      extractChunk({ choices: [{ delta: { content: 'hi', reasoning_content: 'think' } }] }),
    ).toMatchObject({ content: 'hi', reasoning: 'think' });

    expect(extractChunk({ choices: [{ delta: { reasoning: 'r' } }] })).toMatchObject({
      reasoning: 'r',
    });
  });

  it('surfaces finish_reason, usage and timings', () => {
    const chunk = extractChunk({
      choices: [{ delta: {}, finish_reason: 'length' }],
      usage: { prompt_tokens: 3, completion_tokens: 4, total_tokens: 7 },
      timings: { predicted_n: 4, predicted_per_second: 12.5 },
    });
    expect(chunk).toMatchObject({
      finishReason: 'length',
      usage: { total_tokens: 7 },
      timings: { predicted_per_second: 12.5 },
    });
  });

  it('extracts an error message from either shape', () => {
    expect(extractChunk({ error: { message: 'boom' } })?.error).toBe('boom');
    expect(extractChunk({ error: 'plain' })?.error).toBe('plain');
  });

  it('returns null for non-objects', () => {
    expect(extractChunk(null)).toBeNull();
    expect(extractChunk('text')).toBeNull();
  });
});

describe('accumulateToolCalls', () => {
  it('merges argument fragments by index instead of overwriting', () => {
    // Measured on b11698: arguments arrive as `{"` then `\"pattern\":\"` ...
    const calls = accumulateToolCalls([], [
      { index: 0, id: 'call-1', type: 'function', function: { name: 'grep', arguments: '{"' } },
    ]);
    accumulateToolCalls(calls, [{ index: 0, function: { arguments: 'pattern":"x"}' } }]);
    expect(calls).toHaveLength(1);
    expect(calls[0]?.id).toBe('call-1');
    expect(calls[0]?.function.name).toBe('grep');
    expect(calls[0]?.function.arguments).toBe('{"pattern":"x"}');
  });

  it('keeps id and name when later fragments omit them', () => {
    const calls = accumulateToolCalls([], [
      { index: 0, id: 'a', function: { name: 'read_file', arguments: '' } },
      { index: 0, function: { arguments: '{}' } },
    ]);
    expect(calls[0]?.id).toBe('a');
    expect(calls[0]?.function.name).toBe('read_file');
  });

  it('allocates sparse indices without dropping earlier calls', () => {
    const calls = accumulateToolCalls([], [
      { index: 1, id: 'b', function: { name: 'grep', arguments: '{}' } },
    ]);
    expect(calls).toHaveLength(2);
    expect(calls[0]?.id).toBe('');
    expect(calls[1]?.id).toBe('b');
  });
});

describe('parseToolArguments', () => {
  it('returns an object for valid JSON', () => {
    expect(parseToolArguments('{"path":"a.txt"}')).toEqual({ path: 'a.txt' });
  });

  it('returns {} for empty input', () => {
    expect(parseToolArguments('')).toEqual({});
    expect(parseToolArguments(null)).toEqual({});
  });

  it('returns null for incomplete JSON instead of throwing', () => {
    // Mid-stream this necessarily fails; the caller must not crash.
    expect(parseToolArguments('{"path":')).toBeNull();
  });
});

describe('contextUsage', () => {
  it('prefers total_tokens from the latest assistant message', () => {
    const usage = contextUsage(
      [
        { role: 'user' },
        { role: 'assistant', stats: { total_tokens: 500 } },
        { role: 'user' },
        { role: 'assistant', stats: { total_tokens: 900 } },
      ],
      1000,
    );
    expect(usage).toEqual({ used: 900, total: 1000, ratio: 0.9, nearLimit: true });
  });

  it('falls back to prompt+completion counters', () => {
    const usage = contextUsage([{ role: 'assistant', stats: { prompt_n: 100, predicted_n: 50 } }], 1000);
    expect(usage.used).toBe(150);
  });

  it('reports used: null rather than inventing a character-based estimate', () => {
    expect(contextUsage([{ role: 'assistant', stats: {} }], 1000).used).toBeNull();
  });

  it('ignores a non-positive context size', () => {
    expect(contextUsage([{ role: 'assistant', stats: { total_tokens: 5 } }], 0).ratio).toBe(0);
  });
});

describe('buildChatStats', () => {
  it('keeps usage and timings, and never stores reasoning text', () => {
    const stats = buildChatStats({
      usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
      timings: { predicted_n: 20, predicted_per_second: 8, prompt_n: 10 },
      elapsedMs: 1234.6,
      content: 'hello',
      reasoningChars: 42,
    });
    expect(stats).toMatchObject({
      elapsed_ms: 1235,
      chars: 5,
      reasoning_chars: 42,
      prompt_tokens: 10,
      completion_tokens: 20,
      total_tokens: 30,
      predicted_n: 20,
      predicted_per_second: 8,
      prompt_n: 10,
    });
    // Only the *length* is recorded — no `reasoning` text key. `stats_json` has a
    // 16 KiB budget, so prose here would be silently truncated at write time.
    expect(stats).not.toHaveProperty('reasoning');
    expect(stats).not.toHaveProperty('reasoning_text');
  });
});

describe('reasoningPanelOpen', () => {
  const base = { hasReasoning: true };

  it('auto opens while streaming and closes when finished', () => {
    expect(reasoningPanelOpen({ ...base, streaming: true })).toBe(true);
    expect(reasoningPanelOpen({ ...base, streaming: false })).toBe(false);
  });

  it('a manual toggle beats the automatic policy', () => {
    // The rule that matters: without it, the panel snaps shut on the message the
    // user just opened, which reads as a broken control.
    expect(reasoningPanelOpen({ ...base, streaming: true, touched: true, chosen: false })).toBe(false);
    expect(reasoningPanelOpen({ ...base, streaming: false, touched: true, chosen: true })).toBe(true);
  });

  it('expand/collapse preferences apply until touched', () => {
    expect(reasoningPanelOpen({ ...base, mode: 'expand', streaming: false })).toBe(true);
    expect(reasoningPanelOpen({ ...base, mode: 'collapse', streaming: true })).toBe(false);
  });

  it('an unknown mode degrades to auto', () => {
    expect(reasoningPanelOpen({ ...base, mode: 'bogus', streaming: true })).toBe(true);
  });

  it('is closed when there is nothing to show', () => {
    expect(reasoningPanelOpen({ hasReasoning: false, streaming: true })).toBe(false);
  });
});

describe('planReasoningUpdate', () => {
  it('creates on the first frame that has text', () => {
    expect(planReasoningUpdate({ hasPanel: false, shouldOpen: true, textLength: 5 })).toEqual({
      action: 'create',
      open: true,
    });
  });

  it('skips when the length is unchanged', () => {
    expect(
      planReasoningUpdate({ hasPanel: true, shouldOpen: true, textLength: 5, renderedLength: '5' }),
    ).toEqual({ action: 'none', open: true });
  });

  it('updates when new text arrived', () => {
    expect(
      planReasoningUpdate({ hasPanel: true, shouldOpen: true, textLength: 9, renderedLength: 5 }),
    ).toEqual({ action: 'update', open: true });
  });

  it('does nothing without text', () => {
    expect(planReasoningUpdate({ textLength: 0 })).toEqual({ action: 'none', open: false });
  });
});
