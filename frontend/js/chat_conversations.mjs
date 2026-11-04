// Pure helpers for the conversation list: title normalization, pin-aware
// ordering and import validation.
//
// These live outside chat.js because chat.js is DOM-coupled and cannot be
// imported by `node --test`. Every rule here mirrors a backend rule that the
// UI must not contradict:
//
//   * `normalizeChatTitle` mirrors `chat_store._normalize_title` (strip, then
//     truncate to MAX_TITLE_CHARS). Sending a longer title would be silently
//     truncated server-side, so the UI would show one thing and store another.
//   * `sortConversations` mirrors the pinned-first ordering the backend applies
//     in `list_conversations`, so a client that re-sorts the array locally
//     cannot put a pinned conversation back into the chronological middle.
//   * `parseConversationImport` mirrors what `chat_store.import_conversation`
//     accepts, so an unsupported file is rejected with a readable reason
//     instead of failing halfway through the write.

/** 与后端 ``chat_store.MAX_TITLE_CHARS`` 保持一致。 */
export const MAX_CHAT_TITLE_CHARS = 120;
/** 与后端 ``MAX_MESSAGES_PER_CONVERSATION`` 保持一致（导入时用于提前拒绝）。 */
export const MAX_IMPORTED_MESSAGES = 500;

/**
 * 把用户输入变成可提交的标题。
 *
 * 空标题是**错误**而不是"恢复自动标题"：`PATCH /api/chats/{id}` 传空串会
 * 真的把标题存成空，列表随后显示"新对话"，用户会以为改名没生效。想让标题
 * 回到自动生成，唯一可靠的做法是新建会话。
 *
 * @returns {{ok: boolean, title: string, reason: 'empty'|'too-long'|''}}
 */
export function normalizeChatTitle(value) {
  const raw = typeof value === 'string' ? value : String(value ?? '');
  const title = raw.trim();
  if (!title) return { ok: false, title: '', reason: 'empty' };
  // 只警告不拒绝截断：后端也会截，两边都截到同一个长度才是可预测的。
  const limited = Array.from(title).slice(0, MAX_CHAT_TITLE_CHARS).join('');
  return {
    ok: true,
    title: limited,
    reason: limited !== title ? 'too-long' : '',
  };
}

/**
 * Pinned conversations first, then most-recently-updated.
 *
 * 后端已经按 ``pinned DESC, updated_at DESC`` 排过序；前端再排一次是因为
 * 新建/重命名/删除都在本地改数组，不重排会让置顶项漂回时间顺序的中间。
 * 排序必须是**稳定**的：同一时间戳的两条会话不能因为重绘而互换位置。
 *
 * @param {Array<object>} conversations
 * @returns {Array<object>} 新数组（不改原数组）
 */
export function sortConversations(conversations) {
  const list = Array.isArray(conversations) ? conversations.slice() : [];
  const pinned = (item) => (item && item.pinned ? 1 : 0);
  const updated = (item) => {
    const value = Number(item && item.updated_at);
    return Number.isFinite(value) ? value : 0;
  };
  return list.sort((left, right) => {
    const pinDelta = pinned(right) - pinned(left);
    if (pinDelta !== 0) return pinDelta;
    return updated(right) - updated(left);
  });
}

/**
 * 校验一个"我们自己导出的"会话 JSON 能否导入。
 *
 * 只接受本产品导出的形状（``{format, version, conversation}``）：
 * 别人的备份格式字段名不同，猜错会让用户拿到一个空会话，或者更糟 ——
 * 把附件/参数当成消息写进去。
 *
 * 返回 ``{ok: true, conversation}`` 或 ``{ok: false, reason, detail}``。
 */
export function parseConversationImport(payload) {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, reason: 'not-an-object' };
  }
  if (payload.format !== 'lunarshroud-chat') {
    return { ok: false, reason: 'unsupported-format' };
  }
  const version = Number(payload.version);
  if (!Number.isInteger(version) || version < 1) {
    return { ok: false, reason: 'unsupported-version' };
  }
  const conversation = payload.conversation;
  if (!conversation || typeof conversation !== 'object' || Array.isArray(conversation)) {
    return { ok: false, reason: 'missing-conversation' };
  }
  const rawMessages = conversation.messages;
  if (!Array.isArray(rawMessages)) {
    return { ok: false, reason: 'missing-messages' };
  }
  if (!rawMessages.length) {
    return { ok: false, reason: 'empty-conversation' };
  }
  if (rawMessages.length > MAX_IMPORTED_MESSAGES) {
    return { ok: false, reason: 'too-many-messages' };
  }
  const messages = [];
  for (const message of rawMessages) {
    if (!message || typeof message !== 'object' || Array.isArray(message)) {
      return { ok: false, reason: 'bad-message' };
    }
    const role = message.role;
    if (role !== 'system' && role !== 'user' && role !== 'assistant') {
      return { ok: false, reason: 'bad-role' };
    }
    if (typeof message.content !== 'string') {
      return { ok: false, reason: 'bad-content' };
    }
    const reasoning =
      typeof message.reasoning === 'string' ? message.reasoning : '';
    const attachments = Array.isArray(message.attachments)
      ? message.attachments
          .filter(
            (item) =>
              item &&
              typeof item === 'object' &&
              typeof item.name === 'string' &&
              typeof item.content === 'string'
          )
          .map((item) => ({ name: item.name, content: item.content }))
      : [];
    if (attachments.length && role !== 'user') {
      return { ok: false, reason: 'attachment-role' };
    }
    const stats =
      message.stats && typeof message.stats === 'object' && !Array.isArray(message.stats)
        ? message.stats
        : {};
    messages.push({ role, content: message.content, reasoning, stats, attachments });
  }
  const title = typeof conversation.title === 'string' ? conversation.title : '';
  const model = typeof conversation.model === 'string' ? conversation.model : '';
  const params =
    conversation.params && typeof conversation.params === 'object'
      ? conversation.params
      : {};
  return { ok: true, conversation: { title, model, params, messages } };
}

/**
 * 组装导出用的 JSON（可再导入）。
 *
 * 与 Markdown 导出并列而不是替代它：Markdown 是给人看的存档，JSON 是给本
 * 程序读回来的备份。两者都要带上思考过程与引用 —— 少一样，恢复出来的会话
 * 就和导出时看到的不一样。
 */
export function buildConversationExport(conversation, messages, { exportedAt } = {}) {
  const source = conversation && typeof conversation === 'object' ? conversation : {};
  const rows = Array.isArray(messages) ? messages : [];
  return {
    format: 'lunarshroud-chat',
    version: 1,
    exported_at: Number.isFinite(Number(exportedAt))
      ? Number(exportedAt)
      : Date.now() / 1000,
    conversation: {
      title: String(source.title || ''),
      model: String(source.model || ''),
      params: source.params && typeof source.params === 'object' ? source.params : {},
      messages: rows
        .filter((message) => message && (message.role === 'user' || message.role === 'assistant' || message.role === 'system'))
        .map((message) => ({
          role: message.role,
          content: String(message.content || ''),
          reasoning: String(message.reasoning || ''),
          stats: message.stats && typeof message.stats === 'object' ? message.stats : {},
          attachments: (Array.isArray(message.attachments) ? message.attachments : [])
            .map((attachment) => ({
              name: String(attachment.name || ''),
              content: String(attachment.content || ''),
            }))
            .filter((attachment) => attachment.name && attachment.content),
        })),
    },
  };
}
