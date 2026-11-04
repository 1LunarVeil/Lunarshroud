import test from 'node:test';
import assert from 'node:assert/strict';

import { countMcpServers, describeAgentState } from '../js/chat_agent.mjs';
import { markdownPlainText, renderMarkdown } from '../js/markdown.mjs';
import {
  accumulateToolCalls,
  buildChatStats,
  contextUsage,
  createSseParser,
  extractChunk,
  parseToolArguments,
  planReasoningUpdate,
  reasoningPanelOpen,
} from '../js/chat_stream.mjs';
import {
  messageCopyText,
  messageError,
  messageExportLines,
  messageUsableForContext,
  shouldPersistAssistantMessage,
} from '../js/chat_message_text.mjs';
import {
  attachmentBaseName,
  buildMessagePayloadContent,
  classifyAttachmentFile,
  messageHasImages,
  formatAttachmentSize,
  isSupportedAttachmentName,
  looksBinaryText,
} from '../js/chat_attachments.mjs';
import { highlightParts } from '../js/chat_search.mjs';

test('markdown escapes untrusted html and keeps emphasis', () => {
  const html = renderMarkdown('<script>alert(1)</script> **bold** *italic*', { copy: 'Copy' });
  assert.match(html, /&lt;script&gt;alert\(1\)&lt;\/script&gt;/);
  assert.match(html, /<strong>bold<\/strong>/);
  assert.match(html, /<em>italic<\/em>/);
  assert.doesNotMatch(html, /<script>/);
});

test('markdown renders fenced code with a copy control and escapes the body', () => {
  const html = renderMarkdown('```js\nconst x = "<b>";\n```', { copy: '复制' });
  assert.match(html, /class="language-js"/);
  assert.match(html, /data-copy-code/);
  assert.match(html, /复制/);
  assert.match(html, /&quot;&lt;b&gt;&quot;/);
  assert.doesNotMatch(html, /<b>/);
});

test('markdown leaves inline code literal and only links http(s)', () => {
  const html = renderMarkdown('`**not bold**` [ok](https://example.com) [bad](javascript:alert(1))', {
    copy: 'Copy',
  });
  assert.match(html, /<code>\*\*not bold\*\*<\/code>/);
  assert.match(html, /href="https:\/\/example\.com"/);
  assert.doesNotMatch(html, /href="javascript:/);
});

test('markdown renders headings, lists, quotes and rules', () => {
  const html = renderMarkdown('# Title\n\n- one\n- two\n\n> quote\n\n---\n\n1. first\n2. second', {
    copy: 'Copy',
  });
  assert.match(html, /<h1 class="md-h">Title<\/h1>/);
  assert.match(html, /<ul><li>one<\/li><li>two<\/li><\/ul>/);
  assert.match(html, /<blockquote>quote<\/blockquote>/);
  assert.match(html, /<hr>/);
  assert.match(html, /<ol><li>first<\/li><li>second<\/li><\/ol>/);
});

test('markdownPlainText strips common markup for titles', () => {
  assert.equal(markdownPlainText('## Hello **world**\n\n`code`'), 'Hello world code');
});

test('sse parser handles split chunks, comments and [DONE]', () => {
  const parser = createSseParser();
  assert.deepEqual(parser.push(': keep-alive\n\n'), []);
  assert.deepEqual(parser.push('data: {"choices":'), []);
  const events = parser.push('[{"delta":{"content":"hi"}}]}\n\ndata: [DONE]\n\n');
  assert.equal(events.length, 2);
  assert.equal(events[0].data.choices[0].delta.content, 'hi');
  assert.deepEqual(events[1], { done: true });
  assert.deepEqual(parser.flush(), []);
});

test('sse parser flushes an unterminated final event', () => {
  const parser = createSseParser();
  assert.deepEqual(parser.push('data: {"usage":{"total_tokens":3}}'), []);
  const events = parser.flush();
  assert.equal(events[0].data.usage.total_tokens, 3);
});

test('contextUsage uses the latest assistant usage and falls back to components', () => {
  assert.deepEqual(contextUsage([], 8192), {
    used: null,
    total: 8192,
    ratio: 0,
    nearLimit: false,
  });

  const messages = [
    { role: 'user', content: 'hi' },
    { role: 'assistant', stats: { prompt_tokens: 100, completion_tokens: 20 } },
    { role: 'user', content: 'again' },
    { role: 'assistant', stats: { total_tokens: 7000 } },
  ];
  const usage = contextUsage(messages, 8192);
  assert.equal(usage.used, 7000);
  assert.equal(usage.total, 8192);
  assert.ok(usage.ratio > 0.85 && usage.ratio < 1);
  assert.equal(usage.nearLimit, true);

  const fallback = contextUsage(messages.slice(0, 2), 1000);
  assert.equal(fallback.used, 120);
  assert.equal(fallback.ratio, 0.12);
  assert.equal(
    contextUsage([{ role: 'assistant', stats: { prompt_n: 10, predicted_n: 5 } }], 100).used,
    15
  );
  assert.equal(contextUsage([], 0).total, 0);
});

test('attachment helpers enforce the text allow-list and format sizes', () => {
  assert.equal(attachmentBaseName('C:\\logs\\app.log'), 'app.log');
  assert.equal(attachmentBaseName('folder/notes.MD'), 'notes.MD');
  assert.equal(isSupportedAttachmentName('notes.MD'), true);
  assert.equal(isSupportedAttachmentName('script.py'), true);
  assert.equal(isSupportedAttachmentName('photo.png'), false);
  assert.equal(isSupportedAttachmentName('noextension'), false);
  assert.equal(looksBinaryText('a\x00b'), true);
  assert.equal(looksBinaryText('plain text'), false);
  assert.equal(formatAttachmentSize(0), '0 B');
  assert.equal(formatAttachmentSize(1536), '1.5 KB');
  assert.equal(formatAttachmentSize(2 * 1024 * 1024), '2.0 MB');
});

test('buildMessagePayloadContent keeps chat text and wraps attachments', () => {
  assert.equal(buildMessagePayloadContent({ content: 'hello' }), 'hello');
  const payload = buildMessagePayloadContent({
    content: 'review this',
    attachments: [{ name: 'a.py', content: 'print(1)' }],
  });
  assert.match(payload, /^review this\n\n--- Attached file: a\.py ---/);
  assert.match(payload, /print\(1\)/);
  assert.match(payload, /--- End of a\.py ---$/);
});

test('images become an OpenAI multimodal content array, not text', () => {
  // 把 base64 塞进纯文本只会让模型读到一长串乱码 —— 多模态必须走
  // content: [{type:'text'}, {type:'image_url'}]。
  const dataUrl = 'data:image/png;base64,iVBORw0KGgo=';
  const payload = buildMessagePayloadContent({
    content: 'what is this?',
    attachments: [
      { name: 'shot.png', content: dataUrl, kind: 'image' },
      { name: 'a.py', content: 'print(1)', kind: 'text' },
    ],
  });
  assert.ok(Array.isArray(payload), 'must be an array when images are present');
  assert.equal(payload[0].type, 'text');
  assert.match(payload[0].text, /what is this\?/);
  assert.match(payload[0].text, /Attached file: a\.py/);
  const imageParts = payload.filter((p) => p.type === 'image_url');
  assert.equal(imageParts.length, 1);
  assert.equal(imageParts[0].image_url.url, dataUrl);
});

test('a text-only message still returns a plain string', () => {
  // 旧调用点按字符串处理 content；多模态改动不能把它们全变成数组。
  const payload = buildMessagePayloadContent({
    content: 'hi',
    attachments: [{ name: 'a.txt', content: 'x', kind: 'text' }],
  });
  assert.equal(typeof payload, 'string');
});

test('classifyAttachmentFile gates images on the instance vision capability', () => {
  // 图片准入取决**实例**能力，不取决文件本身。
  const noVision = classifyAttachmentFile({
    name: 'shot.png',
    size: 1024,
    supportsVision: false,
  });
  assert.equal(noVision.ok, false);
  assert.equal(noVision.reason, 'no-vision');
  assert.equal(noVision.kind, 'image');

  const withVision = classifyAttachmentFile({
    name: 'shot.png',
    size: 1024,
    supportsVision: true,
  });
  assert.equal(withVision.ok, true);
  assert.equal(withVision.kind, 'image');

  // 超限的图片即使有视觉能力也要拒
  const tooBig = classifyAttachmentFile({
    name: 'huge.png',
    size: 5 * 1024 * 1024,
    supportsVision: true,
  });
  assert.equal(tooBig.ok, false);
  assert.equal(tooBig.reason, 'too-large');

  // 文本不受视觉能力影响
  const text = classifyAttachmentFile({
    name: 'a.py',
    size: 100,
    supportsVision: false,
  });
  assert.equal(text.ok, true);
  assert.equal(text.kind, 'text');

  const unsupported = classifyAttachmentFile({
    name: 'a.exe',
    size: 100,
    supportsVision: true,
  });
  assert.equal(unsupported.ok, false);
  assert.equal(unsupported.reason, 'unsupported');
});

test('messageHasImages detects multimodal turns', () => {
  assert.equal(messageHasImages({ attachments: [] }), false);
  assert.equal(
    messageHasImages({ attachments: [{ name: 'a.txt', content: 'x', kind: 'text' }] }),
    false
  );
  assert.equal(
    messageHasImages({ attachments: [{ name: 'a.png', content: 'data:x', kind: 'image' }] }),
    true
  );
  assert.equal(messageHasImages(null), false);
});

// ========== 工具调用（增量合并） ==========

test('tool_calls fragments are concatenated by index, not overwritten', () => {
  // 实测 b11698 的 SSE 形状：同一个 index 的 arguments 分很多段到达。
  // 覆盖会让参数只剩最后一个字符；push 会得到十几个残缺调用。
  let calls = [];
  calls = accumulateToolCalls(calls, [
    { index: 0, id: 'call_1', type: 'function', function: { name: 'file_glob_search', arguments: '{' } },
  ]);
  calls = accumulateToolCalls(calls, [{ index: 0, function: { arguments: '"pattern":"' } }]);
  calls = accumulateToolCalls(calls, [{ index: 0, function: { arguments: '*.' } }]);
  calls = accumulateToolCalls(calls, [{ index: 0, function: { arguments: 'gguf"}' } }]);

  assert.equal(calls.length, 1);
  assert.equal(calls[0].id, 'call_1');
  assert.equal(calls[0].function.name, 'file_glob_search');
  assert.equal(calls[0].function.arguments, '{"pattern":"*.gguf"}');
  assert.deepEqual(parseToolArguments(calls[0].function.arguments), { pattern: '*.gguf' });
});

test('two parallel tool calls stay separate', () => {
  // 并行调用靠 index 区分；把 index 1 的片段并进 index 0 会让两个调用混成一个。
  let calls = [];
  calls = accumulateToolCalls(calls, [
    { index: 0, id: 'a', function: { name: 'read_file', arguments: '{"p":' } },
    { index: 1, id: 'b', function: { name: 'grep_search', arguments: '{"q":' } },
  ]);
  calls = accumulateToolCalls(calls, [
    { index: 0, function: { arguments: '"x"}' } },
    { index: 1, function: { arguments: '"y"}' } },
  ]);
  assert.equal(calls.length, 2);
  assert.equal(calls[0].function.name, 'read_file');
  assert.equal(calls[0].function.arguments, '{"p":"x"}');
  assert.equal(calls[1].function.name, 'grep_search');
  assert.equal(calls[1].function.arguments, '{"q":"y"}');
});

test('a missing name in later fragments does not erase the first one', () => {
  // id/name 只在第一段出现。用"新值覆盖旧值"的写法会把名字抹成空串。
  let calls = accumulateToolCalls([], [
    { index: 0, id: 'x', function: { name: 'read_file', arguments: '' } },
  ]);
  calls = accumulateToolCalls(calls, [{ index: 0, function: { arguments: '{}' } }]);
  assert.equal(calls[0].function.name, 'read_file');
  assert.equal(calls[0].id, 'x');
});

test('a broken stream fragment leaves partial arguments visible', () => {
  // 流式中途参数一定不完整；解析必须返回 null 而不是抛，否则一帧就炸掉整轮生成。
  assert.equal(parseToolArguments('{"pattern":"*.gg'), null);
  assert.deepEqual(parseToolArguments(''), {});
  assert.equal(parseToolArguments('not json'), null);
  assert.deepEqual(parseToolArguments('{"a":1}'), { a: 1 });
});

test('highlightParts splits case-insensitively and preserves text', () => {
  assert.deepEqual(highlightParts('Find the Needle here', 'needle'), [
    { text: 'Find the ', match: false },
    { text: 'Needle', match: true },
    { text: ' here', match: false },
  ]);
  assert.deepEqual(highlightParts('abc', 'zzz'), [{ text: 'abc', match: false }]);
  assert.deepEqual(highlightParts('aa', 'a'), [
    { text: 'a', match: true },
    { text: 'a', match: true },
  ]);
  assert.deepEqual(highlightParts('x', ''), [{ text: 'x', match: false }]);
});

test('extractChunk normalizes content, reasoning and stats', () => {
  const chunk = extractChunk({
    choices: [{ delta: { content: 'hi', reasoning_content: 'think' }, finish_reason: 'stop' }],
    usage: { prompt_tokens: 2, completion_tokens: 3, total_tokens: 5 },
    timings: { predicted_n: 3, predicted_per_second: 12.5 },
  });
  assert.equal(chunk.content, 'hi');
  assert.equal(chunk.reasoning, 'think');
  assert.equal(chunk.finishReason, 'stop');
  assert.equal(chunk.usage.total_tokens, 5);
  assert.equal(chunk.timings.predicted_per_second, 12.5);

  const stats = buildChatStats({
    usage: chunk.usage,
    timings: chunk.timings,
    elapsedMs: 1234,
    content: 'hello',
    reasoningChars: 3,
  });
  assert.deepEqual(stats, {
    elapsed_ms: 1234,
    chars: 5,
    reasoning_chars: 3,
    prompt_tokens: 2,
    completion_tokens: 3,
    total_tokens: 5,
    predicted_n: 3,
    predicted_per_second: 12.5,
  });
  // 思考正文绝不能进 stats：stats_json 只有 16 KiB 预算，塞进去就必须在写库前
  // 截断，而截断是静默的。它现在是 messages.reasoning 一等列。
  assert.equal(stats.reasoning, undefined, 'reasoning text must not live in stats');
});

test('reasoning panel auto-expands while streaming and collapses after', () => {
  assert.equal(reasoningPanelOpen({ mode: 'auto', streaming: true, hasReasoning: true }), true);
  assert.equal(reasoningPanelOpen({ mode: 'auto', streaming: false, hasReasoning: true }), false);
});

test('the streaming bubble must create the reasoning panel once text arrives', () => {
  // 回归：`updateStreamingBubble()` 只刷新正文节点，从不碰思考面板。首帧时
  // reasoning 为空，模板根本不会输出 <details>，于是整个思考期间界面上只有
  // 一个空壳（一句固定的"思考过程…"），用户看不到进展、以为卡死。
  // 实测服务端是一个 token 一个 token 推 reasoning_content 的，数据一直都在，
  // 缺的是这一帧的渲染决策。
  const plan = planReasoningUpdate({ hasPanel: false, shouldOpen: true, textLength: 9 });
  assert.equal(plan.action, 'create');
  assert.equal(plan.open, true, '思考进行中必须创建并展开');

  // 已存在就别重建：重建会让 <details> 丢掉当前的滚动位置。
  const update = planReasoningUpdate({
    hasPanel: true,
    shouldOpen: true,
    textLength: 12,
    renderedLength: '9',
  });
  assert.equal(update.action, 'update');

  // 长度没变 = 没有新增量，跳过渲染（长思考时每帧重跑 markdown 很贵）。
  const same = planReasoningUpdate({
    hasPanel: true,
    shouldOpen: true,
    textLength: 12,
    renderedLength: '12',
  });
  assert.equal(same.action, 'none');

  // 没有思考正文时什么都不做，更不该创建空面板。
  const empty = planReasoningUpdate({ hasPanel: false, shouldOpen: true, textLength: 0 });
  assert.equal(empty.action, 'none');
});

test('a panel created under the collapse preference stays closed', () => {
  // 建节点时用的是 `open` **属性**而不是 `.open` 赋值：后者会异步触发
  // toggle，被委托监听器记成"用户手动操作过"，从而冻结开合状态，
  // 之后"思考时展开、结束后收起"就永久失效。plan.open 就是那个属性值。
  const plan = planReasoningUpdate({
    hasPanel: false,
    shouldOpen: reasoningPanelOpen({ mode: 'collapse', streaming: true, hasReasoning: true }),
    textLength: 5,
  });
  assert.equal(plan.action, 'create');
  assert.equal(plan.open, false, 'collapse 偏好下创建的面板必须不展开');
});

test('a manual toggle beats the automatic behaviour', () => {
  // 用户在看历史消息时手动展开：流式结束不得把它关掉。
  assert.equal(
    reasoningPanelOpen({
      mode: 'auto',
      streaming: false,
      touched: true,
      chosen: true,
      hasReasoning: true,
    }),
    true
  );
  // 用户在"始终展开"下手动收起某一条：偏好不得再把它撑开。
  assert.equal(
    reasoningPanelOpen({ mode: 'expand', touched: true, chosen: false, hasReasoning: true }),
    false
  );
  // 用户在"始终折叠"下手动点开：折叠偏好不得把它按回去。
  assert.equal(
    reasoningPanelOpen({ mode: 'collapse', touched: true, chosen: true, hasReasoning: true }),
    true
  );
});

test('reasoning panel mode and unknown values degrade to auto', () => {
  assert.equal(reasoningPanelOpen({ mode: 'expand', hasReasoning: true }), true);
  assert.equal(reasoningPanelOpen({ mode: 'collapse', hasReasoning: true }), false);
  // 配置缺失/被写坏时按 auto，而不是永久隐藏。
  assert.equal(reasoningPanelOpen({ mode: '???', streaming: true, hasReasoning: true }), true);
  assert.equal(reasoningPanelOpen({ streaming: true, hasReasoning: true }), true);
  // 没有思考内容时一律不展开（也不该渲染面板）。
  assert.equal(reasoningPanelOpen({ mode: 'expand', hasReasoning: false }), false);
});

test('copy keeps the reasoning only when asked, and never silently', () => {
  const message = { content: 'the answer', reasoning: 'step 1\nstep 2' };
  assert.equal(messageCopyText(message), 'the answer');
  assert.equal(
    messageCopyText(message, { includeReasoning: true }),
    'Reasoning\n\nstep 1\nstep 2\n\nthe answer'
  );
  // 没有思考过程时两个动作结果一致，不会凭空造一个小节标题。
  assert.equal(messageCopyText({ content: 'x', reasoning: '' }, { includeReasoning: true }), 'x');
});

test('markdown export carries the reasoning section', () => {
  const lines = messageExportLines(
    { content: 'the answer', reasoning: 'long chain of thought', attachments: [] },
    {
      speaker: 'Assistant',
      reasoningHeading: 'Reasoning',
      attachmentHeading: 'Attachment',
      partialMarker: 'partial',
    }
  );
  const text = lines.join('\n');
  assert.match(text, /## Assistant/);
  assert.match(text, /### Reasoning/);
  assert.match(text, /long chain of thought/);
  assert.match(text, /the answer/);
  // 思考在正文之前——模型就是这个顺序产出的。
  assert.ok(text.indexOf('long chain of thought') < text.indexOf('the answer'));
});

test('markdown export marks a thinking-only reply instead of writing a blank', () => {
  const lines = messageExportLines(
    { content: '', reasoning: 'was thinking...', attachments: [] },
    {
      speaker: 'Assistant',
      reasoningHeading: 'Reasoning',
      attachmentHeading: 'Attachment',
      partialMarker: 'stopped early',
    }
  );
  const text = lines.join('\n');
  assert.match(text, /was thinking\.\.\./);
  assert.match(text, /_stopped early_/);
});


test('messageError restores persisted stream failures from stats', () => {
  assert.equal(messageError({ error: 'network lost' }), 'network lost');
  assert.equal(messageError({ stats: { error: 'stream failed' } }), 'stream failed');
  assert.equal(messageError({ stats: {} }), '');
  assert.equal(messageUsableForContext({ stats: { error: 'stream failed' } }), false);
  assert.equal(messageUsableForContext({ content: 'ok' }), true);
});

test('markdown export keeps a visible failure marker for interrupted replies', () => {
  const lines = messageExportLines(
    { content: 'half an answer', stats: { error: 'connection lost' } },
    {
      speaker: 'Assistant',
      reasoningHeading: 'Reasoning',
      attachmentHeading: 'Attachment',
      partialMarker: 'partial',
      errorHeading: 'Generation failed',
    }
  );
  const text = lines.join('\n');
  assert.match(text, /Generation failed/);
  assert.match(text, /connection lost/);
  assert.match(text, /half an answer/);
});


test('an empty failed assistant turn is persisted, an empty stopped turn is not', () => {
  assert.equal(
    shouldPersistAssistantMessage({ content: '', reasoning: '', failure: 'upstream lost' }),
    true
  );
  assert.equal(
    shouldPersistAssistantMessage({ content: '', reasoning: '', failure: null, stopped: true }),
    false
  );
  assert.equal(
    shouldPersistAssistantMessage({ content: 'partial', reasoning: '', failure: 'x' }),
    true
  );
});

test('markdown export includes numbered knowledge sources', () => {
  const lines = messageExportLines(
    {
      content: '依据第一条，处理个人信息应当合法。',
      stats: {
        sources: [
          {
            number: 1,
            title: '个人信息保护法.txt',
            page: 3,
            snippet: '第一条 为了保护个人信息权益。',
          },
        ],
      },
    },
    {
      speaker: 'Assistant',
      reasoningHeading: 'Reasoning',
      attachmentHeading: 'Attachment',
      sourcesHeading: 'Sources',
      partialMarker: 'partial',
    }
  );
  const text = lines.join('\n');
  assert.match(text, /### Sources/);
  assert.match(text, /\[1\] 个人信息保护法\.txt \(p\. 3\)/);
  assert.match(text, /第一条 为了保护个人信息权益/);
});

// ========== Agent / MCP 状态（从已脱敏的 argv 归纳） ==========

test('a plain instance shows no agent badge', () => {
  // 没开 agent/MCP 的实例不该出现徽标 —— 徽标的存在本身就是信息。
  const state = describeAgentState(['llama-server.exe', '-m', 'm.gguf', '-c', '8192']);
  assert.equal(state.enabled, false);
  assert.equal(state.hasMcp, false);
});

test('agent mode, tools and MCP servers are each detected from argv', () => {
  const argv = [
    'llama-server.exe',
    '-m', 'm.gguf',
    '--agent',
    '--tools', 'read_file,grep_search',
    '--ui-mcp-proxy',
    '--mcp-servers-json', '{"mcpServers":{"fs":{"command":"npx"},"git":{"command":"npx"}}}',
  ];
  const state = describeAgentState(argv, {
    badge: 'Agent',
    agentMode: 'Agent mode',
    tools: (list) => `Tools: ${list}`,
    mcpProxy: 'MCP proxy',
    mcpServers: (count) => `${count} MCP`,
  });
  assert.equal(state.enabled, true);
  assert.equal(state.hasMcp, true);
  assert.equal(state.label, 'Agent');
  assert.match(state.title, /Agent mode/);
  assert.match(state.title, /Tools: read_file,grep_search/);
  assert.match(state.title, /MCP proxy/);
  assert.match(state.title, /2 MCP/);
});

test('equals-form flags are understood too', () => {
  // `--agent=true` / `--mcp-servers-json={...}` 是自由文本参数里常见的写法。
  const state = describeAgentState([
    'llama-server.exe',
    '--agent=true',
    '--mcp-servers-json={"only":{"command":"x"}}',
  ]);
  assert.equal(state.enabled, true);
  assert.equal(state.hasMcp, true);
  assert.match(state.title, /1 MCP server/);
});

test('a config file reference counts as MCP without inventing a count', () => {
  // `--mcp-servers-config` 只给路径，服务器数量未知 —— 不能编个数字出来。
  const state = describeAgentState([
    'llama-server.exe',
    '--mcp-servers-config', 'D:/cfg/mcp.json',
  ]);
  assert.equal(state.enabled, true);
  assert.equal(state.hasMcp, true);
  assert.match(state.title, /config file/);
  assert.doesNotMatch(state.title, /\d+ MCP server/);
});

test('server counting tolerates the two documented layouts and bad JSON', () => {
  assert.equal(countMcpServers('{"mcpServers":{"a":{},"b":{}}}'), 2);
  assert.equal(countMcpServers('{"servers":{"a":{}}}'), 1);
  // 裸 `{name:{...}}` 无法与"单个服务器定义本身"区分，所以不计（宁可少报）。
  // 这条刻意钉住那个决定：与其猜，不如只认两种有明确外层键的写法。
  assert.equal(countMcpServers('{"bare":{"command":"x"}}'), 1);
  // 手填坏了：给 0 个，不臆测，也不抛
  assert.equal(countMcpServers('{not json'), 0);
  assert.equal(countMcpServers(''), 0);
  assert.equal(countMcpServers(null), 0);
});

test('redacted credentials never leak into the badge title', () => {
  // 端到端防线：后端已把 MCP JSON 里的凭据换成 [REDACTED]（见
  // test_extra_args.py::test_mcp_server_credentials_are_redacted），
  // 这里确认徽标不会把占位符以外的内容带出来。
  const argv = [
    'llama-server.exe',
    '--mcp-servers-json',
    '{"mcpServers":{"fs":{"command":"npx","env":{"API_KEY":"[REDACTED]"}}}}',
  ];
  const state = describeAgentState(argv, { badge: 'Agent', mcpServers: (c) => `${c} MCP` });
  assert.match(state.title, /1 MCP/);
  assert.doesNotMatch(state.title, /API_KEY/);
});
