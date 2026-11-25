import { useEffect, useMemo, useRef, useState } from 'react';
import { MessageSquarePlus, Server, Sparkles } from 'lucide-react';
import type { ChatMessage, ChatToolCall, Instance } from '@/lib/api/types';
import { translate } from '@/lib/i18n';
import { cn } from '@/lib/utils';
import { contextUsage } from '@/lib/ported/chatStream';
import { MessageRow, type ChatIdentity } from '@/components/chat/MessageRow';
import { Composer, type ComposerHandle, type PendingAttachment } from '@/components/chat/Composer';
import { EmptyState, LoadingState } from '@/components/ui/Feedback';

interface LiveStreamShape {
  content: string;
  reasoning: string;
  toolCalls: unknown[];
}

/** Chat background: a data URL plus the two knobs that keep text readable. */
export interface ChatBackground {
  image: string;
  opacity: number;
  blur: number;
}

/**
 * The background layer.
 *
 * Two stacked elements instead of one: the image is blurred and scaled slightly
 * up (so the blur has no transparent edge), and a theme-coloured scrim sits on
 * top. That order matters — putting the scrim under a blurred image would blur
 * the scrim too and produce a halo. `pointer-events: none` keeps every click on
 * the transcript below.
 */
function BackgroundLayer({ background }: { background: ChatBackground }) {
  if (!background.image) return null;
  return (
    <>
      <div
        aria-hidden="true"
        data-testid="chat-background"
        className="pointer-events-none absolute inset-0 -z-10"
        style={{
          backgroundImage: `url("${background.image}")`,
          backgroundSize: 'cover',
          backgroundPosition: 'center',
          filter: background.blur > 0 ? `blur(${background.blur}px)` : undefined,
          transform: background.blur > 0 ? 'scale(1.04)' : undefined,
        }}
      />
      <div
        aria-hidden="true"
        className="pointer-events-none absolute inset-0 -z-10"
        style={{ backgroundColor: 'var(--canvas)', opacity: background.opacity }}
      />
    </>
  );
}

/**
 * The transcript column.
 *
 * Scrolling policy, which is the whole reason this is its own component:
 * - autoscroll follows the stream only while the reader is **at the bottom**.
 *   Scrolling up during generation must not be yanked back — that is the single
 *   most complained-about behaviour in chat UIs, and "always scroll to bottom on
 *   chunk" is the usual cause.
 * - when the reader has scrolled up, a "jump to latest" button appears.
 * - switching conversations resets to the bottom instantly (no smooth scroll from
 *   the previous conversation's position).
 */
export function ChatThread({
  messages,
  live,
  streaming,
  loading,
  instance,
  reasoningMode,
  toolRoot,
  toolResults,
  identity,
  background,
  draft,
  onDraftChange,
  onSend,
  onStop,
  composerRef,
  onNewConversation,
  onNavigate,
  onDeleteMessage,
  onBranchMessage,
  onEditMessage,
  onRegenerateMessage,
  onOpenPrompts,
  onApproveTool,
  onDenyTool,
  onChooseToolRoot,
  onClearToolRoot,
  sendKey = 'enter',
  showStats = true,
  showTimestamps = false,
}: {
  messages: ChatMessage[];
  live: LiveStreamShape;
  streaming: boolean;
  loading: boolean;
  instance: Instance | null;
  reasoningMode: 'auto' | 'expand' | 'collapse' | string;
  /** Which key combo sends. See `AppConfig.chat_send_key`. */
  sendKey?: 'enter' | 'ctrl-enter';
  /** Show the per-message stats row. */
  showStats?: boolean;
  showTimestamps?: boolean;
  toolRoot: string | null;
  toolResults: Record<string, { ok: boolean; content: string }>;
  identity?: ChatIdentity;
  background?: ChatBackground;
  draft: string;
  onDraftChange: (value: string) => void;
  onSend: (attachments: PendingAttachment[]) => void | Promise<void>;
  onStop: () => void;
  composerRef: React.RefObject<ComposerHandle | null>;
  onNewConversation: () => void;
  onNavigate: (page: 'params' | 'dashboard') => void;
  onDeleteMessage: (message: ChatMessage) => void | Promise<void>;
  onBranchMessage: (message: ChatMessage) => void | Promise<void>;
  onEditMessage: (message: ChatMessage, content: string) => void | Promise<void>;
  onRegenerateMessage: () => void | Promise<void>;
  onOpenPrompts: () => void;
  onApproveTool: (call: ChatToolCall) => void | Promise<void>;
  onDenyTool: (call: ChatToolCall) => void | Promise<void>;
  onChooseToolRoot: () => void | Promise<void>;
  onClearToolRoot: () => void | Promise<void>;
}) {
  const scrollerRef = useRef<HTMLDivElement | null>(null);
  const stickToBottom = useRef(true);
  /** Which message is currently being edited in place, if any. */
  const [editingId, setEditingId] = useState<string | number | null>(null);

  const usage = useMemo(
    () => contextUsage(messages as never, instance?.context_size ?? 0),
    [messages, instance?.context_size],
  );

  // Track whether the reader is at the bottom, with a small tolerance so a 1px
  // rounding difference does not disable autoscroll.
  const onScroll = () => {
    const node = scrollerRef.current;
    if (!node) return;
    const distance = node.scrollHeight - node.scrollTop - node.clientHeight;
    stickToBottom.current = distance < 48;
  };

  // Follow the stream.
  useEffect(() => {
    const node = scrollerRef.current;
    if (!node || !stickToBottom.current) return;
    node.scrollTop = node.scrollHeight;
  }, [messages.length, live.content, live.reasoning]);

  // A conversation switch always lands at the bottom.
  useEffect(() => {
    const node = scrollerRef.current;
    if (!node) return;
    stickToBottom.current = true;
    node.scrollTop = node.scrollHeight;
  }, [messages]);

  const lastAssistantIndex = useMemo(() => {
    for (let index = messages.length - 1; index >= 0; index -= 1) {
      if (messages[index]?.role === 'assistant') return index;
    }
    return -1;
  }, [messages]);

  const toolNamesByCallId = useMemo(() => {
    const map: Record<string, string> = {};
    for (const message of messages) {
      const calls = Array.isArray(message.stats?.tool_calls)
        ? (message.stats.tool_calls as Array<{ id: string; function: { name: string } }>)
        : [];
      for (const call of calls) {
        if (call.id) map[call.id] = call.function?.name ?? '';
      }
    }
    return map;
  }, [messages]);

  /**
   * Which calls already have a result.
   *
   * In-memory results cover the current session; persisted `role: "tool"` rows cover
   * everything from before a reload. Both are needed: without the persisted half, a
   * conversation reopened tomorrow shows an "Allow" button for a call that already
   * ran, and pressing it executes the read a second time (and appends a second
   * result row that no longer pairs cleanly). Measured as a follow-on of the H5 fix.
   */
  const resolvedToolResults = useMemo(() => {
    const map: Record<string, { ok: boolean; content: string }> = { ...toolResults };
    for (const message of messages) {
      if (message.role !== 'tool' || !message.tool_call_id) continue;
      if (message.tool_call_id in map) continue;
      map[message.tool_call_id] = { ok: true, content: message.content };
    }
    return map;
  }, [messages, toolResults]);

  const showEmpty = !loading && messages.length === 0 && !streaming;

  return (
    <div className="relative flex min-h-0 flex-1 flex-col">
      {background ? <BackgroundLayer background={background} /> : null}
      <div
        ref={scrollerRef}
        onScroll={onScroll}
        data-testid="chat-thread"
        className="min-h-0 flex-1 overflow-y-auto"
      >
        {loading ? (
          <LoadingState />
        ) : showEmpty ? (
          <div className="flex h-full items-center justify-center">
            {instance ? (
              <EmptyState
                icon={<MessageSquarePlus size={20} />}
                title={translate('chat.newChat')}
                description={translate('chat.emptyHint2')}
                action={
                  <button
                    type="button"
                    onClick={onNewConversation}
                    className="inline-flex h-[var(--control-h)] items-center gap-1.5 rounded-[var(--radius-md)] fill-primary px-3 text-[length:var(--text-sm)] font-medium"
                  >
                    <Sparkles size={14} aria-hidden="true" />
                    {translate('chat.newChat')}
                  </button>
                }
              />
            ) : (
              // "No instance yet" is the **normal** first-run state, not an error:
              // a red warning triangle on a fresh install tells a new user their
              // app is broken. It offers the one action that fixes it.
              <EmptyState
                icon={<Server size={20} />}
                title={translate('chat.emptyTitle')}
                description={translate('chat.emptyHint')}
                action={
                  <div className="flex flex-wrap justify-center gap-2">
                    <button
                      type="button"
                      data-testid="chat-empty-load-model"
                      onClick={() => onNavigate('params')}
                      className="inline-flex h-[var(--control-h)] items-center gap-1.5 rounded-[var(--radius-md)] fill-primary px-3 text-[length:var(--text-sm)] font-medium"
                    >
                      {translate('app.pickModel')}
                    </button>
                    <button
                      type="button"
                      onClick={() => onNavigate('dashboard')}
                      className="inline-flex h-[var(--control-h)] items-center gap-1.5 rounded-[var(--radius-md)] border border-[var(--border-control)] px-3 text-[length:var(--text-sm)] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)]"
                    >
                      {translate('nav.dashboard')}
                    </button>
                  </div>
                }
              />
            )}
          </div>
        ) : (
          <div className="pb-2 pt-4">
            {messages.map((message, index) => (
              <MessageRow
                key={message.id}
                message={message}
                reasoningMode={reasoningMode}
                toolRoot={toolRoot}
                toolResults={resolvedToolResults}
                identity={identity}
                toolNamesByCallId={toolNamesByCallId}
                showStats={showStats}
                showTimestamp={showTimestamps}
                canRegenerate={index === lastAssistantIndex && !streaming}
                onDelete={(item) => void onDeleteMessage(item)}
                onBranch={(item) => void onBranchMessage(item)}
                onEdit={(item) => setEditingId(item.id)}
                onRegenerate={() => void onRegenerateMessage()}
                onApproveTool={(call) => void onApproveTool(call)}
                onDenyTool={(call) => void onDenyTool(call)}
                editing={editingId === message.id}
                onEditCancel={() => setEditingId(null)}
                onEditSubmit={(content) => {
                  setEditingId(null);
                  void onEditMessage(message, content);
                }}
              />
            ))}

            {streaming ? (
              <MessageRow
                message={{
                  id: 'streaming',
                  conversation_id: '',
                  role: 'assistant',
                  content: live.content,
                  reasoning: live.reasoning,
                  tool_call_id: null,
                  stats: {},
                  created_at: Date.now() / 1000,
                  attachments: [],
                }}
                streaming
                reasoningMode={reasoningMode}
                toolRoot={toolRoot}
                toolResults={resolvedToolResults}
                identity={identity}
              />
            ) : null}
          </div>
        )}
      </div>

      <Composer
        ref={composerRef}
        value={draft}
        onValueChange={onDraftChange}
        onSend={(attachments) => void onSend(attachments)}
        onStop={onStop}
        streaming={streaming}
        disabled={!instance}
        supportsVision={Boolean(instance?.supports_vision)}
        contextLabel={translate('chat.contextMeter', {
          percent: Math.round(usage.ratio * 100),
        })}
        contextPercent={usage.used === null ? null : Math.round(usage.ratio * 100)}
        sendKey={sendKey}
      />

      <ToolFolderBar
        instance={instance}
        toolRoot={toolRoot}
        onChoose={onChooseToolRoot}
        onClear={onClearToolRoot}
        onOpenPrompts={onOpenPrompts}
      />

      {/* Reasoning-mode hint: the inspector owns the full control; this states which
          mode is active so the value is visible from the transcript too. */}
      <span className="sr-only" data-testid="chat-mode">
        {reasoningMode}
      </span>
    </div>
  );
}

/**
 * The tool-folder control.
 *
 * 2.0 shipped the whole read-only tool path — declarations, approval bar, backend
 * executor — but no way to authorize a folder, so `tool_root` was always empty, no
 * tools were ever declared, and the feature was unreachable (the same class of
 * defect as 1.0's "the filter could never match"). This bar is the missing
 * entrance, and it states the two states that actually matter:
 *
 * - instance started with `--tools`: llama-server runs its own tools server-side;
 *   a folder here would be a second, weaker implementation, so the bar explains
 *   rather than offering a picker.
 * - otherwise: pick a folder, and the read-only tools become available under
 *   per-call approval.
 */
function ToolFolderBar({
  instance,
  toolRoot,
  onChoose,
  onClear,
  onOpenPrompts,
}: {
  instance: Instance | null;
  toolRoot: string | null;
  onChoose: () => void | Promise<void>;
  onClear: () => void | Promise<void>;
  onOpenPrompts: () => void;
}) {
  if (!instance) return null;
  const nativeTools = instance.declared_tools ?? [];
  return (
    <div
      data-testid="tool-folder-bar"
      className="flex flex-wrap items-center gap-2 border-t border-[var(--border)] bg-[var(--panel)] px-3 py-1.5 text-[length:var(--text-2xs)]"
    >
      {/*
        Prompt library lives here rather than in the composer: it is a text
        utility, and the composer's row is already dense with attach / send /
        context meter. This bar exists for exactly this kind of secondary action.
      */}
      <button
        type="button"
        data-testid="open-prompt-library"
        onClick={onOpenPrompts}
        className="h-6 rounded-[var(--radius-sm)] border border-[var(--border-control)] px-2 text-[length:var(--text-2xs)] text-[var(--text-secondary)] hover:bg-[var(--hover)]"
      >
        {translate('chat.prompts')}
      </button>
      <span aria-hidden="true" className="text-[var(--border-strong)]">|</span>
      {nativeTools.length ? (
        <span className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
          {translate('chat.toolNativeActive', { list: nativeTools.join(', ') })}
        </span>
      ) : (
        <>
          <span className="text-[length:var(--text-xs)] text-[var(--text-tertiary)]">
            {toolRoot ? translate('chat.toolRootNote', { root: toolRoot }) : translate('chat.toolNoRoot')}
          </span>
          <button
            type="button"
            data-testid="choose-tool-root"
            onClick={() => void onChoose()}
            className="h-6 rounded-[var(--radius-sm)] border border-[var(--border-control)] px-2 text-[length:var(--text-2xs)] text-[length:var(--text-sm)] text-[var(--text-secondary)] hover:bg-[var(--hover)]"
          >
            {translate('chat.toolRoot')}
          </button>
          {toolRoot ? (
            <button
              type="button"
              data-testid="clear-tool-root"
              onClick={() => void onClear()}
              className="h-6 rounded-[var(--radius-sm)] px-2 text-[length:var(--text-2xs)] text-[length:var(--text-xs)] text-[var(--text-tertiary)] hover:bg-[var(--hover)]"
            >
              {translate('chat.toolRootClear')}
            </button>
          ) : null}
        </>
      )}
    </div>
  );
}

export { elapsedDuration };
function elapsedDuration(ms: number): string {
  const seconds = Math.max(0, Math.round(ms / 1000));
  if (seconds < 60) return `${seconds}s`;
  return `${Math.floor(seconds / 60)}m${seconds % 60}s`;
}

export { cn };
