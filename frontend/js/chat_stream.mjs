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
 * arrive on the final chunk. Tool calls stream **incrementally** — the same
 * `index` appears in many chunks, each carrying a fragment of `arguments`
 * (measured against b11698: `{"` → `\"pattern\":\"` → `*` → `gg` → `uf` …).
 * All fields are optional.
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
    // 原样透出，由 accumulateToolCalls 负责按 index 合并增量
    toolCalls: Array.isArray(delta.tool_calls) ? delta.tool_calls : [],
    finishReason: choice && choice.finish_reason ? String(choice.finish_reason) : null,
    usage: payload.usage && typeof payload.usage === 'object' ? payload.usage : null,
    timings: payload.timings && typeof payload.timings === 'object' ? payload.timings : null,
    error: error ? String(error.message || error) : null,
  };
}

/**
 * 把增量的 `tool_calls` 累加成完整调用列表。
 *
 * 为什么需要专门写：服务端每次只发**一段**参数字符串，靠 `index` 标识属于
 * 哪一个调用。直接覆盖会让参数只剩最后一个字符；直接 push 会得到十几个
 * 同名残缺调用。实测 b11698 的 `arguments` 就是这么逐字符/逐词到的。
 *
 * `id` 与 `name` 只在第一段出现，后续段没有 —— 所以不能"以新值覆盖旧值"，
 * 只能"有值才写"。
 *
 * @param {Array} existing 已累加的结果（会被修改）
 * @param {Array} deltas   本帧的 delta.tool_calls
 * @returns {Array} 同一个数组引用，便于调用方保持
 */
export function accumulateToolCalls(existing, deltas) {
  const list = Array.isArray(existing) ? existing : [];
  for (const delta of Array.isArray(deltas) ? deltas : []) {
    if (!delta || typeof delta !== 'object') continue;
    const index = Number.isInteger(delta.index) ? delta.index : 0;
    while (list.length <= index) {
      list.push({ id: '', type: 'function', function: { name: '', arguments: '' } });
    }
    const target = list[index];
    if (!target.function) target.function = { name: '', arguments: '' };
    if (typeof delta.id === 'string' && delta.id) target.id = delta.id;
    if (typeof delta.type === 'string' && delta.type) target.type = delta.type;
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
 * 把累加好的参数串解析成对象。
 *
 * 流式中途解析必然失败（JSON 还没写全），所以这里**返回 null 而不是抛**：
 * UI 要在流式期间就把"正在准备调用 X"显示出来，那时参数一定是不完整的。
 * 调用方只在完成后再取解析结果。
 */
export function parseToolArguments(text) {
  const raw = String(text || '').trim();
  if (!raw) return {};
  try {
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' ? parsed : null;
  } catch {
    return null;
  }
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

/**
 * 流式渲染时该怎么更新思考面板。
 *
 * 这存在的理由是一个真实的回归：`updateStreamingBubble()` 只刷新正文节点，
 * 从不碰思考面板，于是模型整个思考期间界面上只有一个空壳（一句固定的
 * "思考过程…"），用户看不到进展、还以为卡死。面板内容必须在每个增量上刷新。
 *
 * 返回值刻意区分两种动作，因为它们的 DOM 语义不同：
 * - `create`：面板节点还不存在（首帧时 reasoning 为空，模板没渲染它）。
 *   必须**用 `open` 属性**建，不能用 `element.open = true` —— 后者会异步触发
 *   `toggle`，而 toggle 委托监听器会把它记成"用户手动操作过"，从而冻结开合
 *   状态，之后"思考时展开、结束后收起"永久失效。
 * - `update`：只改正文。已存在的面板不要碰 `open`，同理。
 * - `none`：没有思考正文，或长度没变（廉价短路，避免每帧重跑 markdown）。
 *
 * @param {object} state
 * @param {boolean} state.hasPanel   思考面板节点当前是否存在
 * @param {boolean} state.shouldOpen reasoningPanelOpen(...) 的结果
 * @param {number} state.textLength  当前思考正文长度
 * @param {string|null} state.renderedLength 上次渲染时记下的长度
 * @returns {{action: 'create'|'update'|'none', open: boolean}}
 */
export function planReasoningUpdate({
  hasPanel = false,
  shouldOpen = false,
  textLength = 0,
  renderedLength = null,
} = {}) {
  if (!textLength) return { action: 'none', open: false };
  if (!hasPanel) return { action: 'create', open: Boolean(shouldOpen) };
  if (renderedLength !== null && renderedLength === String(textLength)) {
    return { action: 'none', open: Boolean(shouldOpen) };
  }
  return { action: 'update', open: Boolean(shouldOpen) };
}
