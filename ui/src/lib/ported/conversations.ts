/**
 * Pure helpers for the conversation list: title normalization, pin-aware
 * ordering, and import/export validation.
 *
 * Ported from `frontend/js/chat_conversations.mjs`. Every rule here mirrors a
 * backend rule the UI must not contradict:
 *
 *   - `normalizeChatTitle` mirrors `chat_store._normalize_title` (strip, then
 *     truncate). A longer title would be silently truncated server-side, so the
 *     UI would show one thing and store another.
 *   - `sortConversations` mirrors the pinned-first ordering in
 *     `list_conversations`, so a client that re-sorts locally cannot put a pinned
 *     conversation back into the chronological middle.
 *   - `parseConversationImport` mirrors what `chat_store.import_conversation`
 *     accepts, so an unsupported file is rejected with a readable reason instead
 *     of failing halfway through a write.
 */

import type { Conversation, ChatAttachment } from '@/lib/api/types';

/** Mirrors `chat_store.MAX_TITLE_CHARS`. */
export const MAX_CHAT_TITLE_CHARS = 120;
/** Mirrors `chat_store.MAX_MESSAGES_PER_CONVERSATION` (used to reject imports early). */
export const MAX_IMPORTED_MESSAGES = 500;

export type TitleProblem = 'empty' | 'too-long' | '';

export interface NormalizedTitle {
  ok: boolean;
  title: string;
  reason: TitleProblem;
}

/**
 * Turn user input into a submittable title.
 *
 * An empty title is an **error**, not "restore the automatic title":
 * `PATCH /api/chats/{id}` with an empty string really does store an empty title,
 * the list then shows "new chat", and the user concludes the rename failed. The
 * only reliable way back to an auto-generated title is to create a new
 * conversation.
 *
 * `too-long` is a warning, not a rejection: both sides truncate to the same
 * length, which is the predictable behaviour.
 */
export function normalizeChatTitle(value: unknown): NormalizedTitle {
  const raw = typeof value === 'string' ? value : String(value ?? '');
  const title = raw.trim();
  if (!title) return { ok: false, title: '', reason: 'empty' };
  // Count by code point so an emoji or CJK surrogate pair counts as one char.
  const limited = Array.from(title).slice(0, MAX_CHAT_TITLE_CHARS).join('');
  return {
    ok: true,
    title: limited,
    reason: limited !== title ? 'too-long' : '',
  };
}

/**
 * Only the two fields ordering depends on. Deliberately not `Conversation`:
 * the function must accept any row-shaped object a caller has, and an index
 * signature would force every caller to widen its own type.
 */
export interface SortableConversation {
  pinned?: boolean;
  /** Unix epoch seconds from SQLite, but tolerate an ISO string too. */
  updated_at?: string | number | null;
}

/**
 * Pinned first, then most recently updated. Returns a new array.
 *
 * The backend already sorts `pinned DESC, updated_at DESC`; the client re-sorts
 * because create/rename/delete mutate the array locally and a pinned item would
 * otherwise drift back into chronological order. The sort must be **stable** —
 * two conversations with the same timestamp must not swap on re-render. `sort` is
 * stable in ES2019+, so a plain comparator is enough.
 */
export function sortConversations<T extends SortableConversation>(conversations: T[] | null | undefined): T[] {
  const list = Array.isArray(conversations) ? conversations.slice() : [];
  const pinned = (item: T) => (item && item.pinned ? 1 : 0);
  // The backend sends epoch seconds as a number. 1.0 assumed an ISO string and
  // called `Number()` on it, which yields NaN → 0 for *every* row, so the sort
  // silently degenerated to insertion order. Accept both shapes: a numeric value
  // is used directly, an ISO string is parsed.
  const updated = (item: T) => {
    const raw = item && item.updated_at;
    if (typeof raw === 'number') return Number.isFinite(raw) ? raw : 0;
    if (typeof raw === 'string') {
      const asNumber = Number(raw);
      if (Number.isFinite(asNumber) && raw.trim() !== '') return asNumber;
      const parsed = Date.parse(raw);
      return Number.isFinite(parsed) ? parsed / 1000 : 0;
    }
    return 0;
  };
  return list.sort((left, right) => {
    const pinDelta = pinned(right) - pinned(left);
    if (pinDelta !== 0) return pinDelta;
    return updated(right) - updated(left);
  });
}

export interface ImportMessage {
  role: 'system' | 'user' | 'assistant';
  content: string;
  reasoning: string;
  stats: Record<string, unknown>;
  attachments: Array<{ name: string; content: string }>;
}

export interface ImportedConversation {
  title: string;
  model: string;
  params: Record<string, unknown>;
  messages: ImportMessage[];
}

export type ImportProblem =
  | 'not-an-object'
  | 'unsupported-format'
  | 'unsupported-version'
  | 'missing-conversation'
  | 'missing-messages'
  | 'empty-conversation'
  | 'too-many-messages'
  | 'bad-message'
  | 'bad-role'
  | 'bad-content'
  | 'attachment-role';

export type ImportResult =
  | { ok: true; conversation: ImportedConversation }
  | { ok: false; reason: ImportProblem };

interface RawImportPayload {
  format?: unknown;
  version?: unknown;
  conversation?: unknown;
}

interface RawImportConversation {
  title?: unknown;
  model?: unknown;
  params?: unknown;
  messages?: unknown;
}

interface RawImportMessage {
  role?: unknown;
  content?: unknown;
  reasoning?: unknown;
  stats?: unknown;
  attachments?: unknown;
}

/**
 * Validate a JSON payload exported by this product.
 *
 * Only this product's own shape (`{format, version, conversation}`) is accepted:
 * another app's backup uses different field names, and a wrong guess gives the
 * user either an empty conversation or — worse — a write in which attachments and
 * params are stored as messages.
 */
export function parseConversationImport(payload: unknown): ImportResult {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
    return { ok: false, reason: 'not-an-object' };
  }
  const root = payload as RawImportPayload;
  if (root.format !== 'lunarshroud-chat') return { ok: false, reason: 'unsupported-format' };

  const version = Number(root.version);
  if (!Number.isInteger(version) || version < 1) {
    return { ok: false, reason: 'unsupported-version' };
  }

  const conversation = root.conversation;
  if (!conversation || typeof conversation !== 'object' || Array.isArray(conversation)) {
    return { ok: false, reason: 'missing-conversation' };
  }
  const source = conversation as RawImportConversation;

  const rawMessages = source.messages;
  if (!Array.isArray(rawMessages)) return { ok: false, reason: 'missing-messages' };
  if (!rawMessages.length) return { ok: false, reason: 'empty-conversation' };
  if (rawMessages.length > MAX_IMPORTED_MESSAGES) return { ok: false, reason: 'too-many-messages' };

  const messages: ImportMessage[] = [];
  for (const raw of rawMessages) {
    if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
      return { ok: false, reason: 'bad-message' };
    }
    const message = raw as RawImportMessage;
    const role = message.role;
    if (role !== 'system' && role !== 'user' && role !== 'assistant') {
      return { ok: false, reason: 'bad-role' };
    }
    if (typeof message.content !== 'string') return { ok: false, reason: 'bad-content' };

    const reasoning = typeof message.reasoning === 'string' ? message.reasoning : '';
    const attachments = Array.isArray(message.attachments)
      ? message.attachments
          .filter(
            (item): item is { name: string; content: string } =>
              Boolean(item) &&
              typeof item === 'object' &&
              typeof (item as ChatAttachment).name === 'string' &&
              typeof (item as ChatAttachment).content === 'string',
          )
          .map((item) => ({ name: item.name, content: item.content }))
      : [];
    if (attachments.length && role !== 'user') {
      return { ok: false, reason: 'attachment-role' };
    }

    const stats =
      message.stats && typeof message.stats === 'object' && !Array.isArray(message.stats)
        ? (message.stats as Record<string, unknown>)
        : {};
    messages.push({ role, content: message.content, reasoning, stats, attachments });
  }

  return {
    ok: true,
    conversation: {
      title: typeof source.title === 'string' ? source.title : '',
      model: typeof source.model === 'string' ? source.model : '',
      params:
        source.params && typeof source.params === 'object'
          ? (source.params as Record<string, unknown>)
          : {},
      messages,
    },
  };
}

export interface ExportMessage {
  role?: unknown;
  content?: unknown;
  reasoning?: unknown;
  stats?: unknown;
  attachments?: unknown;
}

/**
 * Assemble the JSON export (re-importable).
 *
 * Complementary to the Markdown export rather than a replacement: Markdown is an
 * archive for a human, JSON is a backup this program can read back. Both must
 * carry the reasoning text and citations — drop either and the restored
 * conversation differs from what the user saw when exporting.
 */
export function buildConversationExport(
  conversation: Partial<Pick<Conversation, 'title' | 'model' | 'params'>> | null | undefined,
  messages: ExportMessage[] | null | undefined,
  { exportedAt }: { exportedAt?: number } = {},
) {
  const source = conversation && typeof conversation === 'object' ? conversation : {};
  const rows = Array.isArray(messages) ? messages : [];
  return {
    format: 'lunarshroud-chat' as const,
    version: 1 as const,
    exported_at: Number.isFinite(Number(exportedAt)) ? Number(exportedAt) : Date.now() / 1000,
    conversation: {
      title: String(source.title || ''),
      model: String(source.model || ''),
      params:
        source.params && typeof source.params === 'object' ? source.params : {},
      messages: rows
        .filter(
          (message) =>
            message &&
            (message.role === 'user' || message.role === 'assistant' || message.role === 'system'),
        )
        .map((message) => ({
          role: message.role as string,
          content: String(message.content || ''),
          reasoning: String(message.reasoning || ''),
          stats:
            message.stats && typeof message.stats === 'object'
              ? (message.stats as Record<string, unknown>)
              : {},
          attachments: (Array.isArray(message.attachments) ? message.attachments : [])
            .map((attachment: Partial<ChatAttachment>) => ({
              name: String(attachment?.name || ''),
              content: String(attachment?.content || ''),
            }))
            .filter((attachment) => attachment.name && attachment.content),
        })),
    },
  };
}
