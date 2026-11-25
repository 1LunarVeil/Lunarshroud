import { describe, expect, it } from 'vitest';
import {
  buildConversationExport,
  MAX_CHAT_TITLE_CHARS,
  normalizeChatTitle,
  parseConversationImport,
  sortConversations,
} from '@/lib/ported/conversations';

describe('normalizeChatTitle', () => {
  it('trims and accepts a normal title', () => {
    expect(normalizeChatTitle('  hello  ')).toEqual({ ok: true, title: 'hello', reason: '' });
  });

  it('treats an empty title as an error, not as "restore automatic"', () => {
    // PATCH with an empty string really does store an empty title, and the list
    // then shows "new chat" — the user concludes the rename failed.
    expect(normalizeChatTitle('   ')).toEqual({ ok: false, title: '', reason: 'empty' });
    expect(normalizeChatTitle(null).ok).toBe(false);
  });

  it('truncates over-long input and reports it as a warning, not a rejection', () => {
    const long = 'a'.repeat(MAX_CHAT_TITLE_CHARS + 40);
    const result = normalizeChatTitle(long);
    expect(result.ok).toBe(true);
    expect(result.title).toHaveLength(MAX_CHAT_TITLE_CHARS);
    expect(result.reason).toBe('too-long');
  });

  it('counts code points, so an emoji is one character', () => {
    const emoji = '🙂'.repeat(MAX_CHAT_TITLE_CHARS + 5);
    const result = normalizeChatTitle(emoji);
    expect(Array.from(result.title)).toHaveLength(MAX_CHAT_TITLE_CHARS);
  });
});

describe('sortConversations', () => {
  // The backend returns epoch seconds as a number (verified against the live API),
  // so the fixture uses that shape.
  const rows = [
    { id: 'old', pinned: false, updated_at: 1767225600 },
    { id: 'newest', pinned: false, updated_at: 1780272000 },
    { id: 'pinned-old', pinned: true, updated_at: 1735689600 },
  ];

  it('puts pinned conversations first, then most recent', () => {
    expect(sortConversations(rows).map((row) => row.id)).toEqual(['pinned-old', 'newest', 'old']);
  });

  it('returns a new array and leaves the input untouched', () => {
    const original = rows.map((row) => row.id);
    const sorted = sortConversations(rows);
    expect(sorted).not.toBe(rows);
    expect(rows.map((row) => row.id)).toEqual(original);
  });

  it('is stable for equal timestamps', () => {
    const tie = [
      { id: 'a', pinned: false, updated_at: 1767225600 },
      { id: 'b', pinned: false, updated_at: 1767225600 },
    ];
    expect(sortConversations(tie).map((row) => row.id)).toEqual(['a', 'b']);
  });

  it('sorts an ISO string the same way as an epoch number', () => {
    // Tolerating both shapes keeps the function usable against fixtures and old
    // cached payloads without reintroducing the NaN degeneration.
    const mixed = [
      { id: 'iso', pinned: false, updated_at: '2026-06-01T00:00:00Z' },
      { id: 'epoch', pinned: false, updated_at: 1767225600 },
    ];
    expect(sortConversations(mixed).map((row) => row.id)).toEqual(['iso', 'epoch']);
  });

  it('tolerates missing or unparseable timestamps', () => {
    const messy = [
      { id: 'x', pinned: false, updated_at: null },
      { id: 'y', pinned: false, updated_at: 'not-a-date' },
      { id: 'z', pinned: false },
    ];
    expect(sortConversations(messy)).toHaveLength(3);
  });
});

describe('parseConversationImport', () => {
  const valid = {
    format: 'lunarshroud-chat',
    version: 1,
    conversation: {
      title: 'T',
      model: 'm.gguf',
      params: { temperature: 0.7 },
      messages: [{ role: 'user', content: 'hi' }],
    },
  };

  it('accepts this product\'s own export shape', () => {
    const result = parseConversationImport(valid);
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.conversation.messages[0]).toEqual({
        role: 'user',
        content: 'hi',
        reasoning: '',
        stats: {},
        attachments: [],
      });
    }
  });

  it('rejects a foreign payload rather than guessing its fields', () => {
    expect(parseConversationImport({ messages: [] })).toEqual({
      ok: false,
      reason: 'unsupported-format',
    });
    expect(parseConversationImport(null)).toEqual({ ok: false, reason: 'not-an-object' });
    expect(parseConversationImport([])).toEqual({ ok: false, reason: 'not-an-object' });
  });

  it('rejects a bad version, empty transcript and over-limit transcript', () => {
    expect(
      parseConversationImport({ ...valid, version: 0 }),
    ).toEqual({ ok: false, reason: 'unsupported-version' });
    expect(
      parseConversationImport({ ...valid, conversation: { messages: [] } }),
    ).toEqual({ ok: false, reason: 'empty-conversation' });
    expect(
      parseConversationImport({
        ...valid,
        conversation: { messages: Array.from({ length: 501 }, () => ({ role: 'user', content: 'x' })) },
      }),
    ).toEqual({ ok: false, reason: 'too-many-messages' });
  });

  it('rejects an unknown role and non-string content', () => {
    expect(
      parseConversationImport({
        ...valid,
        conversation: { messages: [{ role: 'tool', content: 'x' }] },
      }),
    ).toEqual({ ok: false, reason: 'bad-role' });
    expect(
      parseConversationImport({
        ...valid,
        conversation: { messages: [{ role: 'user', content: 5 }] },
      }),
    ).toEqual({ ok: false, reason: 'bad-content' });
  });

  it('rejects attachments on a non-user message', () => {
    expect(
      parseConversationImport({
        ...valid,
        conversation: {
          messages: [{ role: 'assistant', content: 'x', attachments: [{ name: 'a', content: 'b' }] }],
        },
      }),
    ).toEqual({ ok: false, reason: 'attachment-role' });
  });

  it('keeps reasoning and attachments on a user message', () => {
    const result = parseConversationImport({
      ...valid,
      conversation: {
        messages: [
          {
            role: 'user',
            content: 'q',
            reasoning: 'r',
            attachments: [{ name: 'a.txt', content: 'body' }],
          },
        ],
      },
    });
    expect(result.ok).toBe(true);
    if (result.ok) {
      expect(result.conversation.messages[0]?.reasoning).toBe('r');
      expect(result.conversation.messages[0]?.attachments).toEqual([
        { name: 'a.txt', content: 'body' },
      ]);
    }
  });
});

describe('buildConversationExport', () => {
  it('round-trips through the importer', () => {
    const exported = buildConversationExport(
      { title: 'T', model: 'm.gguf', params: { top_p: 0.9 } },
      [
        { role: 'user', content: 'q', attachments: [{ name: 'a.txt', content: 'body' }] },
        { role: 'assistant', content: 'a', reasoning: 'thinking', stats: { total_tokens: 12 } },
      ],
      { exportedAt: 1700000000 },
    );
    expect(exported.format).toBe('lunarshroud-chat');
    expect(exported.version).toBe(1);
    expect(exported.exported_at).toBe(1700000000);

    const parsed = parseConversationImport(exported);
    expect(parsed.ok).toBe(true);
    if (parsed.ok) {
      expect(parsed.conversation.title).toBe('T');
      expect(parsed.conversation.messages).toHaveLength(2);
      // Reasoning and stats must survive: losing them makes a restored
      // conversation differ from what the user exported.
      expect(parsed.conversation.messages[1]?.reasoning).toBe('thinking');
      expect(parsed.conversation.messages[1]?.stats).toEqual({ total_tokens: 12 });
      expect(parsed.conversation.messages[0]?.attachments).toEqual([
        { name: 'a.txt', content: 'body' },
      ]);
    }
  });

  it('drops tool rows, which the import contract does not accept', () => {
    const exported = buildConversationExport(null, [
      { role: 'user', content: 'q' },
      { role: 'tool', content: 'raw' },
    ]);
    expect(exported.conversation.messages.map((message) => message.role)).toEqual(['user']);
  });

  it('omits attachments without both a name and content', () => {
    const exported = buildConversationExport(null, [
      { role: 'user', content: 'q', attachments: [{ name: '', content: 'x' }, { name: 'a', content: '' }] },
    ]);
    expect(exported.conversation.messages[0]?.attachments).toEqual([]);
  });
});
