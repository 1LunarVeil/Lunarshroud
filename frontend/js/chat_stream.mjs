// Pure SSE helpers for the native chat stream.
//
// llama-server's OpenAI-compatible endpoint sends `data: {json}` events and a
// final `data: [DONE]`. Chunks can split anywhere, so the parser keeps a
// buffer and only emits complete events.

/**
 * @returns {{push: (text: string) => object[], flush: () => object[]}}
 */
export function createSseParser() {
  let buffer = '';

  const parseBlock = (block) => {
    const dataLines = [];
    for (const line of block.split('\n')) {
      if (line.startsWith(':')) continue; // comment / keep-alive
      if (!line.startsWith('data:')) continue;
      dataLines.push(line.slice(5).replace(/^ /, ''));
    }
    if (!dataLines.length) return null;
    const data = dataLines.join('\n');
    if (data === '[DONE]') return { done: true };
    try {
      return { data: JSON.parse(data) };
    } catch {
      return { raw: data };
    }
  };

  const drain = () => {
    const events = [];
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
    push(text) {
      buffer += String(text ?? '').replace(/\r\n?/g, '\n');
      return drain();
    },
    flush() {
      const events = [];
      if (buffer.trim()) {
        const parsed = parseBlock(buffer);
        if (parsed) events.push(parsed);
      }
      buffer = '';
      return events;
    },
  };
}

/**
 * Normalize one OpenAI-compatible chunk.
 *
 * llama.cpp may put reasoning text in `reasoning_content`; usage/timings can
 * arrive on the final chunk. All fields are optional.
 */
export function extractChunk(payload) {
  if (!payload || typeof payload !== 'object') return null;
  const choice = Array.isArray(payload.choices) ? payload.choices[0] : null;
  const delta = choice && typeof choice.delta === 'object' ? choice.delta : {};
  const error = payload.error;
  return {
    content: typeof delta.content === 'string' ? delta.content : '',
    reasoning:
      typeof delta.reasoning_content === 'string'
        ? delta.reasoning_content
        : typeof delta.reasoning === 'string'
          ? delta.reasoning
          : '',
    finishReason: choice && choice.finish_reason ? String(choice.finish_reason) : null,
    usage: payload.usage && typeof payload.usage === 'object' ? payload.usage : null,
    timings: payload.timings && typeof payload.timings === 'object' ? payload.timings : null,
    error: error ? String(error.message || error) : null,
  };
}

/**
 * Approximate how much of the instance context the latest turn occupies.
 *
 * llama-server reports usage on the final SSE chunk. `total_tokens` covers the
 * prompt plus the generated reply, which is exactly what the next request must
 * fit again. Older or interrupted turns may only have component counters; keep
 * those as a fallback. Returns `used: null` until a response has real stats,
 * rather than pretending a character count is a token count.
 */
export function contextUsage(messages, contextSize) {
  const total = Number(contextSize);
  const limit = Number.isFinite(total) && total > 0 ? Math.floor(total) : 0;
  let used = null;
  if (Array.isArray(messages)) {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      const message = messages[index];
      if (!message || message.role !== 'assistant') continue;
      const stats = message.stats && typeof message.stats === 'object' ? message.stats : {};
      if (Number.isFinite(stats.total_tokens) && stats.total_tokens > 0) {
        used = stats.total_tokens;
        break;
      }
      const prompt = Number.isFinite(stats.prompt_tokens)
        ? stats.prompt_tokens
        : Number.isFinite(stats.prompt_n)
          ? stats.prompt_n
          : 0;
      const completion = Number.isFinite(stats.completion_tokens)
        ? stats.completion_tokens
        : Number.isFinite(stats.predicted_n)
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

/** Merge the fields we care about from a final chunk into a message's stats. */
export function buildChatStats({
  usage = null,
  timings = null,
  elapsedMs = 0,
  content = '',
  reasoningChars = 0,
} = {}) {
  const stats = {
    elapsed_ms: Math.max(0, Math.round(elapsedMs)),
    chars: content.length,
  };
  // 思考正文不再进 stats —— stats_json 只有 16 KiB 预算，把正文塞进去就得截断，
  // 而"截断"发生在写库那一刻，用户完全看不见。它现在是 messages.reasoning 一等列。
  if (reasoningChars) {
    stats.reasoning_chars = reasoningChars;
  }
  if (usage && typeof usage === 'object') {
    if (Number.isFinite(usage.prompt_tokens)) stats.prompt_tokens = usage.prompt_tokens;
    if (Number.isFinite(usage.completion_tokens)) stats.completion_tokens = usage.completion_tokens;
    if (Number.isFinite(usage.total_tokens)) stats.total_tokens = usage.total_tokens;
  }
  if (timings && typeof timings === 'object') {
    if (Number.isFinite(timings.predicted_n)) stats.predicted_n = timings.predicted_n;
    if (Number.isFinite(timings.predicted_per_second)) {
      stats.predicted_per_second = timings.predicted_per_second;
    }
    if (Number.isFinite(timings.prompt_n)) stats.prompt_n = timings.prompt_n;
  }
  return stats;
}

/**
 * Decide whether a message's reasoning panel is open.
 *
 * The rule that matters is precedence: **a manual toggle always beats the
 * automatic behaviour**. Without that, "expand while it thinks, collapse when
 * done" would snap shut on the exact message the user just opened to read --
 * and because the list re-renders on every chunk, that reads as the control
 * being broken rather than as a policy.
 *
 * @param {object} state
 * @param {'auto'|'expand'|'collapse'} [state.mode] preference; anything else
 *   is treated as `auto` so a missing or corrupt setting degrades to the
 *   default instead of hiding the panel forever.
 * @param {boolean} state.streaming  is this reply still generating
 * @param {boolean} state.touched    did the user click it at least once
 * @param {boolean} state.chosen     the open/closed state they left it in
 * @param {boolean} state.hasReasoning is there anything to show at all
 * @returns {boolean}
 */
export function reasoningPanelOpen({
  mode = 'auto',
  streaming = false,
  touched = false,
  chosen = false,
  hasReasoning = false,
} = {}) {
  if (!hasReasoning) return false;
  if (touched) return Boolean(chosen);
  if (mode === 'expand') return true;
  if (mode === 'collapse') return false;
  return Boolean(streaming);
}
