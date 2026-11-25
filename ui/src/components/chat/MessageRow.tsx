import { Check, Copy, Pencil, RefreshCw, Split, Trash2 } from 'lucide-react';
import { memo, useState } from 'react';
import type { ChatAttachment, ChatMessage, ChatToolCall } from '@/lib/api/types';
import { classifyAttachmentContent, formatAttachmentSize } from '@/lib/ported/attachments';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { Markdown } from '@/components/chat/Markdown';
import { Button } from '@/components/ui/Button';
import { MessageBoundary } from '@/components/chat/MessageBoundary';
import { ReasoningPanel } from '@/components/chat/ReasoningPanel';
import { Citations, ToolCallBlock, ToolResultBlock } from '@/components/chat/ToolBlocks';

/**
 * One row in the transcript.
 *
 * Three roles get three visually distinct treatments. That separation is the point
 * — 1.0 rendered a user message as a bright bubble and the assistant's answer as
 * bare text, so the two halves of one exchange did not look like the same product,
 * and tool output looked like the assistant talking.
 *
 * | role | treatment |
 * |---|---|
 * | user | right-aligned tinted bubble, max 80% width |
 * | assistant | full-width prose on the well, no bubble |
 * | tool | data block, collapsed, monospaced |
 * | system | muted aside |
 *
 * Actions are revealed on hover **and** on keyboard focus within the row. Hiding
 * them behind hover only would make them unreachable by keyboard.
 */
export interface MessageRowProps {
  message: ChatMessage;
  streaming?: boolean;
  reasoningMode?: 'auto' | 'expand' | 'collapse' | string;
  toolRoot?: string | null;
  toolResults?: Record<string, { ok: boolean; content: string }>;
  toolNamesByCallId?: Record<string, string>;
  /**
   * Display identity (config `chat_user_name` / `chat_assistant_name` and the two
   * avatar values). Passed in rather than read here so one fetch serves every row
   * and a settings change repaints immediately.
   */
  identity?: ChatIdentity;
  /** Show the stats row (config `chat_show_stats`). Defaults to on. */
  showStats?: boolean;
  /** Show when the message was created (config `chat_show_timestamps`). */
  showTimestamp?: boolean;
  onCopy?: (message: ChatMessage, includeReasoning: boolean) => void;
  onEdit?: (message: ChatMessage) => void;
  onBranch?: (message: ChatMessage) => void;
  onRegenerate?: (message: ChatMessage) => void;
  onDelete?: (message: ChatMessage) => void;
  onApproveTool?: (call: ChatToolCall) => void;
  onDenyTool?: (call: ChatToolCall) => void;
  canRegenerate?: boolean;
  /** Render this row as an editor (user messages only). */
  editing?: boolean;
  onEditCancel?: () => void;
  /** Called with the new text; the parent persists it and regenerates. */
  onEditSubmit?: (content: string) => void;
}

/** Display identity shared by every row. Empty fields fall back to the defaults. */
export interface ChatIdentity {
  userName: string;
  assistantName: string;
  userAvatar: string;
  assistantAvatar: string;
}

/**
 * The author chip above a message.
 *
 * Rendered for user and assistant only: tool output and system notices are not
 * "someone speaking", and giving them a face would suggest otherwise.
 *
 * The avatar is deliberately `<img>` for a data URL and a letter otherwise —
 * `classifyAvatar` in the backend already refuses remote URLs, so there is no
 * path here where a missing image turns into a network request.
 */
function AuthorChip({ role, identity }: { role: 'user' | 'assistant'; identity?: ChatIdentity }) {
  if (!identity) return null;
  const isUser = role === 'user';
  const name = (isUser ? identity.userName : identity.assistantName).trim();
  const avatar = (isUser ? identity.userAvatar : identity.assistantAvatar).trim();
  const fallback = translate(isUser ? 'chat.you' : 'chat.assistant');
  const label = name || fallback;

  return (
    <div className="mb-1 flex items-center gap-1.5" data-testid={`author-${role}`}>
      {avatar.startsWith('data:image/') ? (
        <img
          src={avatar}
          alt=""
          aria-hidden="true"
          draggable={false}
          className="size-[18px] shrink-0 rounded-full object-cover"
        />
      ) : (
        <span
          aria-hidden="true"
          className={cn(
            'grid size-[18px] shrink-0 place-items-center rounded-full text-[length:var(--text-2xs)] font-medium',
            isUser
              ? 'bg-[var(--accent-subtle)] text-[var(--accent-text)]'
              : 'bg-[var(--surface-sunken)] text-[var(--text-secondary)]',
          )}
        >
          {(avatar || label).slice(0, 1)}
        </span>
      )}
      <span className="truncate text-[length:var(--text-2xs)] font-medium text-[var(--text-secondary)]">
        {label}
      </span>
    </div>
  );
}

function AttachmentChips({ attachments }: { attachments: ChatAttachment[] }) {  if (!attachments.length) return null;
  return (
    <div className="mb-1.5 flex flex-wrap gap-1.5">
      {attachments.map((attachment, index) => {
        const kind = classifyAttachmentContent(attachment.content);
        return (
          <span
            key={`${attachment.name}-${index}`}
            title={attachment.name}
            className={cn(
              'inline-flex max-w-[16rem] items-center gap-1.5 rounded-[var(--radius-sm)] border px-1.5 py-1',
              'text-[length:var(--text-2xs)]',
              kind === 'image'
                ? 'border-[var(--accent)]/30 bg-[var(--accent-subtle)] text-[var(--accent-text)]'
                : 'border-[var(--border)] bg-[var(--surface)] text-[length:var(--text-sm)] text-[var(--text-secondary)]',
            )}
          >
            <span className="truncate">{attachment.name || 'attachment'}</span>
            {kind === 'text' ? (
              <span className="shrink-0 tnum text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
                {formatAttachmentSize(attachment.size)}
              </span>
            ) : null}
          </span>
        );
      })}
    </div>
  );
}

function MessageImages({ attachments }: { attachments: ChatAttachment[] }) {
  const images = attachments.filter((attachment) => classifyAttachmentContent(attachment.content) === 'image');
  if (!images.length) return null;
  return (
    <div className="mb-1.5 flex flex-wrap gap-1.5">
      {images.map((image, index) => (
        <img
          key={`${image.name}-${index}`}
          src={image.content}
          alt={image.name || 'Attached image'}
          loading="lazy"
          className="max-h-56 max-w-[min(22rem,100%)] rounded-[var(--radius-md)] border border-[var(--border)] object-contain"
        />
      ))}
    </div>
  );
}

function IconAction({
  label,
  onClick,
  children,
  danger = false,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      title={label}
      aria-label={label}
      className={cn(
        'grid size-6 place-items-center rounded-[var(--radius-xs)] transition-colors',
        'text-[length:var(--text-xs)] text-[var(--text-tertiary)]',
        danger
          ? 'hover:bg-[var(--danger-subtle)] hover:text-[var(--danger)]'
          : 'hover:bg-[var(--hover)] hover:text-[var(--text)]',
        'focus-visible:outline-2 focus-visible:outline-offset-1 focus-visible:outline-[var(--border-focus)]',
      )}
    >
      {children}
    </button>
  );
}

export const MessageRow = memo(function MessageRow({
  message,
  streaming = false,
  reasoningMode = 'auto',
  toolRoot = null,
  toolResults,
  toolNamesByCallId,
  onCopy,
  onEdit,
  onBranch,
  onRegenerate,
  onDelete,
  onApproveTool,
  onDenyTool,
  canRegenerate = false,
  identity,
  showStats = true,
  showTimestamp = false,
  editing = false,
  onEditCancel,
  onEditSubmit,
}: MessageRowProps) {
  const [copied, setCopied] = useState(false);
  const [editDraft, setEditDraft] = useState(message.content);
  const isUser = message.role === 'user';
  const isTool = message.role === 'tool';
  const isSystem = message.role === 'system';

  const toolCalls = Array.isArray(message.stats?.tool_calls) ? (message.stats.tool_calls as ChatToolCall[]) : [];
  const sources = Array.isArray(message.stats?.sources)
    ? (message.stats.sources as Array<{ index: number; title: string; page?: number | null; heading?: string | null; snippet: string; ocr?: boolean }>)
    : [];
  const durationMs =
    typeof message.stats?.elapsed_ms === 'number' ? (message.stats.elapsed_ms as number) : null;

  const copy = async (includeReasoning: boolean) => {
    if (onCopy) {
      onCopy(message, includeReasoning);
    } else if (typeof navigator !== 'undefined') {
      await navigator.clipboard.writeText(message.content);
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1400);
  };

  const actions = (
    <div
      className={cn(
        'mt-1 flex items-center gap-0.5 opacity-0 transition-opacity duration-100',
        'group-hover:opacity-100 group-focus-within:opacity-100',
        isUser && 'justify-end',
      )}
    >
      <IconAction label={translate('chat.copy')} onClick={() => void copy(false)}>
        {copied ? <Check size={13} aria-hidden="true" /> : <Copy size={13} aria-hidden="true" />}
      </IconAction>
      {message.reasoning ? (
        <IconAction label={translate('chat.copyWithThinking')} onClick={() => void copy(true)}>
          <Copy size={13} aria-hidden="true" />
        </IconAction>
      ) : null}
      {isUser && onEdit ? (
        <IconAction label={translate('chat.edit')} onClick={() => onEdit(message)}>
          <Pencil size={13} aria-hidden="true" />
        </IconAction>
      ) : null}
      {onBranch ? (
        <IconAction label={translate('chat.branch')} onClick={() => onBranch(message)}>
          <Split size={13} aria-hidden="true" />
        </IconAction>
      ) : null}
      {!isUser && canRegenerate && onRegenerate ? (
        <IconAction label={translate('chat.regenerate')} onClick={() => onRegenerate(message)}>
          <RefreshCw size={13} aria-hidden="true" />
        </IconAction>
      ) : null}
      {onDelete ? (
        <IconAction label={translate('common.delete')} onClick={() => onDelete(message)} danger>
          <Trash2 size={13} aria-hidden="true" />
        </IconAction>
      ) : null}
    </div>
  );

  if (isTool) {
    return (
      <article data-testid="message-tool" data-role="tool" className="group px-[var(--thread-pad)] py-1">
        <div className="mx-auto w-full max-w-[var(--thread-max)]">
          <ToolResultBlock
            name={toolNamesByCallId?.[message.tool_call_id ?? ''] ?? null}
            content={message.content}
            toolCallId={message.tool_call_id}
          />
        </div>
      </article>
    );
  }

  if (isSystem) {
    return (
      <article data-testid="message-system" data-role="system" className="group px-[var(--thread-pad)] py-1.5">
        <div className="mx-auto w-full max-w-[var(--thread-max)]">
          <div className="rounded-[var(--radius-md)] border border-[var(--border)] bg-[var(--surface-sunken)] px-3 py-2 text-[length:var(--text-xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
            <MessageBoundary fallbackText={message.content}>
              <Markdown text={message.content} />
            </MessageBoundary>
          </div>
        </div>
      </article>
    );
  }

  if (isUser) {
    if (editing && onEditSubmit) {
      return (
        <article data-testid="message-user-editing" data-role="user" className="group px-[var(--thread-pad)] py-1.5">
          <div className="mx-auto flex w-full max-w-[var(--thread-max)] flex-col items-end gap-1.5">
            <textarea
              autoFocus
              aria-label={translate('chat.edit')}
              value={editDraft}
              onChange={(event) => setEditDraft(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === 'Escape') {
                  event.preventDefault();
                  setEditDraft(message.content);
                  onEditCancel?.();
                }
                // Enter saves, Shift+Enter is a newline — the same contract as the
                // composer, so editing does not require relearning the keyboard.
                if (event.key === 'Enter' && !event.shiftKey) {
                  event.preventDefault();
                  if (editDraft.trim()) onEditSubmit(editDraft);
                }
              }}
              rows={Math.min(14, Math.max(3, editDraft.split('\n').length))}
              className={cn(
                'w-full resize-y rounded-[var(--radius-lg)] border border-[var(--border-focus)] bg-[var(--surface)]',
                'px-3 py-2 text-[length:var(--text-md)] leading-[var(--leading-cjk)]',
              )}
            />
            <div className="flex items-center gap-2">
              <span className="text-[length:var(--text-2xs)] text-[var(--text-tertiary)]">
                {translate('chat.editHelp')}
              </span>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => {
                  setEditDraft(message.content);
                  onEditCancel?.();
                }}
              >
                {translate('common.cancel')}
              </Button>
              <Button
                variant="primary"
                size="sm"
                disabled={!editDraft.trim() || editDraft === message.content}
                onClick={() => onEditSubmit(editDraft)}
              >
                {translate('chat.editSave')}
              </Button>
            </div>
          </div>
        </article>
      );
    }

    return (
      <article
        data-testid="message-user"
        data-role="user"
        className="group px-[var(--thread-pad)] py-1.5"
      >
        <div className="mx-auto flex w-full max-w-[var(--thread-max)] flex-col items-end">
          <AuthorChip role="user" identity={identity} />
          <div
            className={cn(
              'max-w-[80%] rounded-[var(--radius-xl)] px-3.5 py-2',
              'bg-[var(--bubble-user)] text-[var(--text)]',
              'whitespace-pre-wrap break-anywhere text-[length:var(--text-md)] leading-[var(--leading-cjk)]',
            )}
          >
            <MessageImages attachments={message.attachments} />
            <AttachmentChips attachments={message.attachments} />
            {message.content}
          </div>
          {actions}
        </div>
      </article>
    );
  }

  // Assistant.
  return (
    <article data-testid="message-assistant" data-role="assistant" className="group px-[var(--thread-pad)] py-1.5">
      <div className="mx-auto w-full max-w-[var(--thread-max)]">
        <AuthorChip role="assistant" identity={identity} />
        <ReasoningPanel
          text={message.reasoning}
          mode={reasoningMode}
          streaming={streaming}
          durationMs={durationMs}
        />
        {toolCalls.length ? (
          <ToolCallBlock
            calls={toolCalls}
            root={toolRoot}
            results={toolResults}
            pending={!streaming}
            onApprove={onApproveTool}
            onDeny={onDenyTool}
          />
        ) : null}
        {message.content ? (
          <MessageBoundary fallbackText={message.content}>
            <Markdown text={message.content} className={streaming && !message.content ? 'opacity-60' : undefined} />
          </MessageBoundary>
        ) : streaming ? (
          <div className="flex items-center gap-1 py-1" aria-live="polite" aria-label={translate('chat.sending')}>
            <span className="size-1.5 animate-bounce rounded-full bg-[var(--text-quaternary)] [animation-delay:-0.3s]" />
            <span className="size-1.5 animate-bounce rounded-full bg-[var(--text-quaternary)] [animation-delay:-0.15s]" />
            <span className="size-1.5 animate-bounce rounded-full bg-[var(--text-quaternary)]" />
          </div>
        ) : null}
        {sources.length ? <Citations sources={sources} /> : null}
        {/*
          Per-message statistics, restored from 1.0: tokens · tok/s · elapsed.
          The keys already existed in both dictionaries (`chat.statsTokens`,
          `chat.statsTime`) with no call site, and the values were already being
          persisted into `stats` on every reply — the only missing piece was this
          line. Only rendered when a number is actually present, so an aborted or
          usage-less reply does not show "0 tokens · — tok/s".
        */}
        <MessageStats
          stats={message.stats}
          streaming={streaming}
          show={showStats}
          createdAt={message.created_at}
          showTimestamp={showTimestamp}
        />
        {actions}
      </div>
    </article>
  );
});

function MessageStats({
  stats,
  streaming,
  show = true,
  createdAt,
  showTimestamp = false,
}: {
  stats?: Record<string, unknown>;
  streaming?: boolean;
  /** `chat_show_stats === 'never'` hides the whole row. */
  show?: boolean;
  createdAt?: number | string | null;
  showTimestamp?: boolean;
}) {
  if (streaming) return null;
  const number = (key: string): number | null => {
    const value = stats?.[key];
    return typeof value === 'number' && Number.isFinite(value) ? value : null;
  };

  const parts: string[] = [];
  if (show && stats) {
    const total = number('total_tokens');
    const completion = number('completion_tokens');
    const speed = number('predicted_per_second');
    const elapsed = number('elapsed_ms');
    const tokens = total ?? completion;
    if (tokens !== null) {
      parts.push(translate('chat.statsTokens', { count: Math.round(tokens) }));
    }
    if (speed !== null) parts.push(`${speed.toFixed(1)} tok/s`);
    if (elapsed !== null) {
      const seconds = Math.max(0, elapsed / 1000);
      parts.push(translate('chat.statsTime', { seconds: seconds.toFixed(1) }));
    }
  }

  // The timestamp honours its own setting independently: someone who hides token
  // counts may still want "when was this said".
  if (showTimestamp) {
    const seconds =
      typeof createdAt === 'number'
        ? createdAt
        : typeof createdAt === 'string' && createdAt
          ? Number(createdAt)
          : Number.NaN;
    if (Number.isFinite(seconds) && seconds > 0) {
      parts.push(new Date(seconds * 1000).toLocaleString());
    }
  }

  if (!parts.length) return null;

  return (
    <p className="mt-1 text-[length:var(--text-2xs)] tnum text-[var(--text-quaternary)]" data-testid="message-stats">
      {parts.join(' · ')}
    </p>
  );
}

export { Markdown };
