import test from 'node:test';
import assert from 'node:assert/strict';

import {
  MAX_CHAT_TITLE_CHARS,
  MAX_IMPORTED_MESSAGES,
  buildConversationExport,
  normalizeChatTitle,
  parseConversationImport,
  sortConversations,
} from '../js/chat_conversations.mjs';

test('normalizeChatTitle trims, rejects empty and truncates to the backend limit', () => {
  assert.deepEqual(normalizeChatTitle('  hello  '), {
    ok: true,
    title: 'hello',
    reason: '',
  });
  assert.equal(normalizeChatTitle('').ok, false);
  assert.equal(normalizeChatTitle('   ').reason, 'empty');
  assert.equal(normalizeChatTitle(null).reason, 'empty');
  assert.equal(normalizeChatTitle(undefined).reason, 'empty');

  // 截断到与后端一致的字符数，并给出 reason 以便 UI 提示（后端也会截，
  // 两边截到同一长度才可预测）
  const long = 'a'.repeat(MAX_CHAT_TITLE_CHARS + 10);
  const verdict = normalizeChatTitle(long);
  assert.equal(verdict.ok, true);
  assert.equal(Array.from(verdict.title).length, MAX_CHAT_TITLE_CHARS);
  assert.equal(verdict.reason, 'too-long');

  // 长度按码位而不是 UTF-16 单元算：emoji 不能被劈成半个代理对
  const emoji = '🦙'.repeat(MAX_CHAT_TITLE_CHARS + 5);
  const emojiVerdict = normalizeChatTitle(emoji);
  assert.equal(Array.from(emojiVerdict.title).length, MAX_CHAT_TITLE_CHARS);
  assert.equal(emojiVerdict.title.includes('\uFFFD'), false);
});

test('sortConversations puts pinned first, then newest, and stays stable', () => {
  const input = [
    { id: 'a', pinned: false, updated_at: 300 },
    { id: 'b', pinned: true, updated_at: 100 },
    { id: 'c', pinned: false, updated_at: 200 },
    { id: 'd', pinned: true, updated_at: 200 },
  ];
  assert.deepEqual(
    sortConversations(input).map((item) => item.id),
    ['d', 'b', 'a', 'c']
  );
  // 不改原数组
  assert.deepEqual(input.map((item) => item.id), ['a', 'b', 'c', 'd']);

  // 相同时间戳必须保持输入顺序（稳定排序），否则每次重绘列表都会抖动
  const tied = [
    { id: 'x', pinned: false, updated_at: 100 },
    { id: 'y', pinned: false, updated_at: 100 },
    { id: 'z', pinned: false, updated_at: 100 },
  ];
  assert.deepEqual(
    sortConversations(tied).map((item) => item.id),
    ['x', 'y', 'z']
  );

  // 缺字段/非数字不得抛错
  assert.doesNotThrow(() => sortConversations([{ id: 'n' }, null, { id: 'm', updated_at: 'x' }]));
  assert.deepEqual(sortConversations(null), []);
});

test('parseConversationImport accepts our own export shape', () => {
  const payload = {
    format: 'lunarshroud-chat',
    version: 1,
    conversation: {
      title: 'Trip',
      model: 'm.gguf',
      params: { temperature: 0.4 },
      messages: [
        { role: 'user', content: 'hi', attachments: [{ name: 'a.txt', content: 'body' }] },
        { role: 'assistant', content: 'hello', reasoning: 'thinking', stats: { predicted_n: 3 } },
      ],
    },
  };
  const verdict = parseConversationImport(payload);
  assert.equal(verdict.ok, true);
  assert.equal(verdict.conversation.title, 'Trip');
  assert.equal(verdict.conversation.model, 'm.gguf');
  assert.deepEqual(verdict.conversation.params, { temperature: 0.4 });
  assert.equal(verdict.conversation.messages.length, 2);
  assert.deepEqual(verdict.conversation.messages[0].attachments, [
    { name: 'a.txt', content: 'body' },
  ]);
  assert.equal(verdict.conversation.messages[1].reasoning, 'thinking');
  assert.deepEqual(verdict.conversation.messages[1].stats, { predicted_n: 3 });
});

test('parseConversationImport rejects each malformed shape with a reason', () => {
  const base = (overrides) => ({
    format: 'lunarshroud-chat',
    version: 1,
    conversation: {
      title: 'x',
      messages: [{ role: 'user', content: 'ok' }],
      ...(overrides || {}),
    },
  });

  const cases = [
    [null, 'not-an-object'],
    ['nope', 'not-an-object'],
    [[], 'not-an-object'],
    [{ format: 'other', version: 1, conversation: base().conversation }, 'unsupported-format'],
    [{ format: 'lunarshroud-chat', version: 0, conversation: base().conversation }, 'unsupported-version'],
    [{ format: 'lunarshroud-chat', version: 'x', conversation: base().conversation }, 'unsupported-version'],
    [{ format: 'lunarshroud-chat', version: 1 }, 'missing-conversation'],
    [{ format: 'lunarshroud-chat', version: 1, conversation: 5 }, 'missing-conversation'],
    [
      { format: 'lunarshroud-chat', version: 1, conversation: { messages: 'nope' } },
      'missing-messages',
    ],
    [
      { format: 'lunarshroud-chat', version: 1, conversation: { messages: [] } },
      'empty-conversation',
    ],
    [
      {
        format: 'lunarshroud-chat',
        version: 1,
        conversation: {
          messages: Array.from({ length: MAX_IMPORTED_MESSAGES + 1 }, () => ({
            role: 'user',
            content: 'x',
          })),
        },
      },
      'too-many-messages',
    ],
    [
      { format: 'lunarshroud-chat', version: 1, conversation: { messages: [null] } },
      'bad-message',
    ],
    [
      {
        format: 'lunarshroud-chat',
        version: 1,
        conversation: { messages: [{ role: 'tool', content: 'x' }] },
      },
      'bad-role',
    ],
    [
      {
        format: 'lunarshroud-chat',
        version: 1,
        conversation: { messages: [{ role: 'user', content: 5 }] },
      },
      'bad-content',
    ],
    [
      {
        format: 'lunarshroud-chat',
        version: 1,
        conversation: {
          messages: [
            { role: 'assistant', content: 'x', attachments: [{ name: 'a', content: 'b' }] },
          ],
        },
      },
      'attachment-role',
    ],
  ];
  for (const [payload, reason] of cases) {
    const verdict = parseConversationImport(payload);
    assert.equal(verdict.ok, false, `expected rejection for ${JSON.stringify(payload)}`);
    assert.equal(verdict.reason, reason);
  }

  // 合法的那一份仍然通过（证明上面的拒绝不是因为 base 本身坏了）
  assert.equal(parseConversationImport(base()).ok, true);
});

test('parseConversationImport drops junk per-message fields instead of failing', () => {
  const verdict = parseConversationImport({
    format: 'lunarshroud-chat',
    version: 1,
    conversation: {
      messages: [
        {
          role: 'user',
          content: 'x',
          // 非字符串的思考过程 / 非对象的 stats 回落成空值，而不是拒绝整个文件：
          // 它们是可选装饰，缺了不影响对话本身。
          reasoning: 42,
          stats: 'nope',
          attachments: [
            { name: 'ok.txt', content: 'body' },
            { name: 5, content: 'bad' },
            { content: 'no name' },
            null,
          ],
        },
      ],
    },
  });
  assert.equal(verdict.ok, true);
  const message = verdict.conversation.messages[0];
  assert.equal(message.reasoning, '');
  assert.deepEqual(message.stats, {});
  assert.deepEqual(message.attachments, [{ name: 'ok.txt', content: 'body' }]);
});

test('buildConversationExport produces a payload parseConversationImport accepts', () => {
  const conversation = {
    title: 'Round trip',
    model: 'm.gguf',
    params: { temperature: 0.3 },
  };
  const messages = [
    {
      role: 'user',
      content: 'question',
      attachments: [{ name: 'n.txt', content: 'body', size: 4 }],
    },
    {
      role: 'assistant',
      content: 'answer',
      reasoning: 'why',
      stats: { sources: [{ number: 1, title: 'doc' }] },
    },
    { role: 'system', content: 'be brief' },
    // 非法角色被过滤掉：导出文件必须始终能被自己的导入器读回来
    { role: 'tool', content: 'ignored' },
  ];
  const payload = buildConversationExport(conversation, messages, { exportedAt: 1234 });
  assert.equal(payload.format, 'lunarshroud-chat');
  assert.equal(payload.version, 1);
  assert.equal(payload.exported_at, 1234);

  const verdict = parseConversationImport(payload);
  assert.equal(verdict.ok, true);
  assert.equal(verdict.conversation.messages.length, 3);
  assert.equal(verdict.conversation.messages[0].attachments[0].name, 'n.txt');
  assert.equal(verdict.conversation.messages[1].reasoning, 'why');
  assert.deepEqual(verdict.conversation.messages[1].stats.sources, [{ number: 1, title: 'doc' }]);
  assert.deepEqual(verdict.conversation.params, { temperature: 0.3 });
});
