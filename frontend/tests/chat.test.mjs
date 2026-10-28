import test from 'node:test';
import assert from 'node:assert/strict';

import { markdownPlainText, renderMarkdown } from '../js/markdown.mjs';
import {
  buildChatStats,
  contextUsage,
  createSseParser,
  extractChunk,
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
