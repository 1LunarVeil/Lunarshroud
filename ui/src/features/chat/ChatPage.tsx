import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { toast } from 'sonner';
import { createPortal } from 'react-dom';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { useUiStore } from '@/lib/stores/ui';
import { translate, localizeBackendError } from '@/lib/i18n';
import { chatsApi, systemApi } from '@/lib/api';
import { pickPath } from '@/lib/api/client';
import type { ChatMessage, ChatToolCall, Conversation } from '@/lib/api/types';
import { ChatSidebarList } from '@/features/chat/ChatSidebarList';
import { ChatThread } from '@/features/chat/ChatThread';
import { resolveChatSettings, agentModeEnabled, type ResolvedChatSettings } from '@/features/chat/settings';
import { ChatInspector } from '@/features/chat/ChatInspector';
import { PromptLibraryDialog } from '@/features/chat/PromptLibraryDialog';
import { useChatStream } from '@/features/chat/useChatStream';
import { useChatInstances } from '@/features/chat/queries';

/**
 * The chat page.
 *
 * Layout: the conversation list lives in the sidebar (mounted through the sidebar
 * portal), the transcript and composer own the main pane, and the inspector is an
 * optional right column.
 *
 * State ownership:
 * - `activeId` — which conversation. Session state, not persisted: reopening the
 *   app lands on the most recent conversation, which is what "continue where I
 *   left off" means.
 * - `messages` — the transcript of the active conversation. Held here (not in a
 *   query cache) because streaming mutates it on every frame; a cache write per
 *   token would thrash every subscriber.
 * - everything else — derived.
 */
export function ChatPage() {
  const instances = useChatInstances();
  const client = useQueryClient();
  // The reasoning panel's default open/close policy is a user setting
  // (`config.chat_reasoning`). `ChatThread` used to hard-code "auto" for every
  // row, so changing the setting had no visible effect anywhere.
  const config = useQuery({ queryKey: ['system', 'config'], queryFn: systemApi.getConfig });
  const reasoningMode =
    config.data?.chat_reasoning === 'expand' || config.data?.chat_reasoning === 'collapse'
      ? config.data.chat_reasoning
      : 'auto';
  // Display identity and background come from the same config read, so a settings
  // save repaints the transcript without a reload (the 1.0 lesson: page modules
  // read this once at init and never again).
  const identity = useMemo(
    () => ({
      userName: config.data?.chat_user_name ?? '',
      assistantName: config.data?.chat_assistant_name ?? '',
      userAvatar: config.data?.chat_user_avatar ?? '',
      assistantAvatar: config.data?.chat_assistant_avatar ?? '',
    }),
    [config.data],
  );
  const background = useMemo(
    () => ({
      image: config.data?.chat_background_image ?? '',
      opacity: config.data?.chat_background_opacity ?? 0.35,
      blur: config.data?.chat_background_blur ?? 0,
    }),
    [config.data],
  );
  const [activeId, setActiveId] = useState<string | null>(null);
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [settings, setSettings] = useState<ResolvedChatSettings>(() => resolveChatSettings(null));
  const [toolResults, setToolResults] = useState<Record<string, { ok: boolean; content: string }>>({});
  const [toolRoot, setToolRoot] = useState<string | null>(null);
  /** Full conversation row, needed by the inspector (params, instance, knowledge). */
  const [activeConversation, setActiveConversation] = useState<Conversation | null>(null);
  const [promptLibraryOpen, setPromptLibraryOpen] = useState(false);
  const inspectorOpen = useUiStore((state) => state.inspectorOpen);
  const setInspectorOpen = useUiStore((state) => state.setInspectorOpen);
  const navigate = useUiStore((state) => state.navigate);
  const composerRef = useRef<{ focus: () => void; clear: () => void; insertText: (text: string) => void } | null>(null);
  const { streaming, live, run, stop, executeTool } = useChatStream();

  const instanceList = instances.data ?? [];
  const activeInstance = useMemo(() => {
    // Prefer the conversation's bound instance; fall back to the first running one.
    const bound = settings.instanceId
      ? instanceList.find((instance) => instance.id === settings.instanceId)
      : undefined;
    return bound ?? instanceList[0] ?? null;
  }, [instanceList, settings.instanceId]);

  // Load a conversation's transcript.
  const loadConversation = useCallback(async (id: string) => {
    setLoadingConversation(true);
    try {
      const detail = await chatsApi.detail(id);
      setMessages(detail.messages ?? []);
      setSettings(resolveChatSettings(detail));
      setActiveConversation(detail);
      setToolRoot(detail.tool_root ?? null);
      setToolResults({});
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
      setMessages([]);
    } finally {
      setLoadingConversation(false);
    }
  }, []);

  useEffect(() => {
    if (activeId) void loadConversation(activeId);
    else setMessages([]);
  }, [activeId, loadConversation]);

  // Expose the sidebar list through a portal target inside the sidebar.
  const [sidebarHost, setSidebarHost] = useState<HTMLElement | null>(null);
  useEffect(() => {
    setSidebarHost(document.getElementById('sidebar-list-slot'));
  }, []);

  const createConversation = useCallback(
    async (): Promise<Conversation | null> => {
      try {
        const conversation = await chatsApi.create({
          title: '',
          instance_id: activeInstance?.id ?? null,
          model: activeInstance?.model_path ?? null,
          model_key: activeInstance?.model_key ?? null,
        });
        // The sidebar reads its own query; without this the brand-new conversation
        // exists on the server and the sidebar still says "No conversations yet"
        // until a navigation happens to refetch. `useCreateConversation` already
        // does this — this path creates directly, so it has to do the same. The
        // key carries the limit, so invalidate by the shared prefix.
        void client.invalidateQueries({ queryKey: ['chats', 'list'] });
        setActiveId(conversation.id);
        setMessages([]);
        setSettings(resolveChatSettings(conversation));
        composerRef.current?.focus();
        return conversation;
      } catch (error) {
        toast.error(localizeBackendError(error instanceof Error ? error.message : error));
        return null;
      }
    },
    [activeInstance, client],
  );

  /**
   * One streaming turn: ask the model, then persist whatever came back.
   *
   * Shared by "send" and by "continue after a tool result". The transcript is
   * passed in rather than read from state so a caller that just appended a tool
   * message can send the exact list it wrote, without waiting a render.
   *
   * `attachments` is empty on a follow-up: the pictures already live in the user
   * message that carried them, and re-sending them would double the vision payload
   * on every tool round trip.
   */
  const streamTurn = useCallback(
    async (
      conversationId: string,
      transcript: ChatMessage[],
    ) => {
      // `stats.tool_calls` is what makes a tool result pairable: llama-server's
      // template matches `role: "tool"` rows to the call id from the assistant
      // message that requested them. Dropping it (2.0 did until this fix) sends a
      // result with no originating call, and the model sees an unexplained blob of
      // file content instead of a completed call.
      const history = transcript.map((message) => {
        const calls = Array.isArray(message.stats?.tool_calls)
          ? (message.stats.tool_calls as ChatToolCall[])
          : [];
        return {
          role: message.role,
          content: message.content,
          ...(message.reasoning ? { reasoning: message.reasoning } : {}),
          ...(message.tool_call_id ? { tool_call_id: message.tool_call_id } : {}),
          ...(calls.length ? { tool_calls: calls } : {}),
        };
      });

      const outcome = await run({
        conversationId,
        instanceId: activeInstance?.id ?? '',
        messages: history,
        attachments: [],
        params: settings.params,
        systemPrompt: settings.systemPrompt,
        knowledgeBaseId: settings.knowledgeBaseId,
        toolRoot,
        declaredTools: activeInstance?.declared_tools ?? [],
        supportsVision: Boolean(activeInstance?.supports_vision),
        thinking: settings.thinking,
        locale: useUiStore.getState().locale,
        // Write/command tools are a per-conversation opt-in; web search needs the
        // **global** setting (which supplies the endpoint) *and* the per-conversation
        // checkbox. Reading only the global flag made the inspector's checkbox do
        // nothing: the tool was declared whether or not the user ticked it.
        agentMode: agentModeEnabled(settings.params),
        webSearchEnabled:
          Boolean(config.data?.web_search_enabled) && settings.params.web_search === true,
        webSearchConfigured: Boolean(String(config.data?.web_search_endpoint ?? '').trim()),
        memory: settings.params.memory === true,
        memoryScope:
          typeof settings.params.memory_scope === 'string'
            ? settings.params.memory_scope
            : undefined,
        skillId:
          typeof settings.params.skill_id === 'string' ? settings.params.skill_id : null,
      });

      if (outcome.error) toast.error(localizeBackendError(outcome.error));
      if (outcome.droppedTurns > 0) {
        toast.warning(translate('chat.droppedTurns', { count: outcome.droppedTurns }));
      }
      if (outcome.thinkingExhausted) {
        toast.warning(translate('chat.thinkingExhaustedShort'));
      }

      // Persist whatever was produced, even a partial answer after Stop.
      if (outcome.content || outcome.reasoning || outcome.toolCalls.length) {
        try {
          const assistant = await chatsApi.appendMessage(conversationId, {
            role: 'assistant',
            content: outcome.content,
            reasoning: outcome.reasoning,
            stats: outcome.stats,
          });
          setMessages((current) => [...current, assistant]);
        } catch (error) {
          toast.error(localizeBackendError(error instanceof Error ? error.message : error));
        }
      }
      return outcome;
    },
    [activeInstance, run, settings, toolRoot],
  );

  const send = useCallback(
    async (attachments: Parameters<typeof run>[0]['attachments']) => {
      if (!activeInstance) {
        toast.error(translate('chat.newChatNeedsInstance'));
        return;
      }

      let conversationId = activeId;
      if (!conversationId) {
        const created = await createConversation();
        if (!created) return;
        conversationId = created.id;
      }

      const text = draft.trim();
      setDraft('');

      // Persist the user turn first so a crash mid-stream keeps the question.
      let userMessage: ChatMessage;
      try {
        userMessage = await chatsApi.appendMessage(conversationId, {
          role: 'user',
          content: text,
          ...(attachments.length
            ? {
                attachments: attachments.map((attachment) => ({
                  name: attachment.name,
                  content: attachment.content,
                })),
              }
            : {}),
        });
      } catch (error) {
        toast.error(localizeBackendError(error instanceof Error ? error.message : error));
        setDraft(text);
        return;
      }
      setMessages((current) => [...current, userMessage]);

      // Attachments travel with this turn only: they are already inside the user
      // message the model receives, so the stream request carries none of its own.
      return streamTurn(conversationId, [...messages, userMessage]);
    },
    [activeInstance, createConversation, draft, messages, streamTurn],
  );

  /**
   * Run one tool call after the user pressed "Allow" in the transcript.
   *
   * Separate from `send` because the flow happens on a human timescale: the user
   * may read the arguments, switch conversations, and come back. The result is
   * persisted as a `role: "tool"` message so it survives a reload and stays
   * pairable with its call id.
   *
   * **This is the only path that executes a chat tool.** 2.0 ran every requested
   * call inline right after the reply, which made the approval bar decoration: a
   * model could read any file in the authorized folder without a human seeing the
   * call. Audit finding H5, 2026-11-23.
   */
  const approveToolCall = useCallback(
    async (call: ChatToolCall) => {
      if (!activeId) return;
      if (!toolRoot) {
        toast.error(translate('chat.toolNoRoot'));
        return;
      }
      const result = await executeTool(call, toolRoot);
      if (call.id) setToolResults((current) => ({ ...current, [call.id]: result }));

      let nextMessages = messages;
      try {
        const saved = await chatsApi.appendMessage(activeId, {
          role: 'tool',
          content: result.content,
          tool_call_id: call.id,
        });
        nextMessages = [...messages, saved];
        setMessages(nextMessages);
      } catch (error) {
        // A persistence failure is not an execution failure: the tool did run, and
        // telling the user it failed would make them approve it again.
        toast.error(
          translate('chat.toolSaveFailed') +
            ': ' +
            localizeBackendError(error instanceof Error ? error.message : error),
        );
      }
      // Keep the answer going: the model asked for this result in order to
      // continue, and making the user press send again reads as "approve did
      // nothing".
      await streamTurn(activeId, nextMessages);
    },
    [activeId, executeTool, messages, streamTurn, toolRoot],
  );

  /** A denial is persisted too, so the model can choose another approach. */
  const denyToolCall = useCallback(
    async (call: ChatToolCall) => {
      if (!activeId || !call.id) return;
      const content = translate('chat.toolDeniedResult');
      setToolResults((current) => ({ ...current, [call.id]: { ok: false, content } }));
      let nextMessages = messages;
      try {
        const saved = await chatsApi.appendMessage(activeId, {
          role: 'tool',
          content,
          tool_call_id: call.id,
        });
        nextMessages = [...messages, saved];
        setMessages(nextMessages);
      } catch (error) {
        toast.error(localizeBackendError(error instanceof Error ? error.message : error));
      }
      await streamTurn(activeId, nextMessages);
    },
    [activeId, messages, streamTurn],
  );

  const abort = useCallback(() => stop(), [stop]);

  /**
   * Edit a user turn, drop everything after it, and regenerate from there.
   *
   * `truncate_after: true` is deliberate: keeping the later assistant reply would
   * leave a transcript that answers a question nobody asked. The backend flag
   * exists so an assistant-only fix can skip the truncation, but the *user* edit
   * path in this UI always regenerates — that is what editing a prompt means.
   *
   * The edit is persisted **before** the stream starts, so a failed generation
   * leaves the corrected question in place instead of silently reverting the fix.
   */
  const editMessage = useCallback(
    async (message: ChatMessage, content: string) => {
      if (!activeId) return;
      try {
        await chatsApi.updateMessage(activeId, message.id, {
          content,
          truncate_after: true,
          // The stored reasoning belonged to the old prompt; keeping it would
          // contradict the new one (the backend documents this exact case).
          reasoning: '',
        });
        const detail = await chatsApi.detail(activeId);
        const transcript = detail.messages ?? [];
        setMessages(transcript);
        await streamTurn(activeId, transcript);
      } catch (error) {
        toast.error(localizeBackendError(error instanceof Error ? error.message : error));
      }
    },
    [activeId, streamTurn],
  );

  /**
   * Regenerate the last assistant reply.
   *
   * Editing a message and regenerating are the same operation from two directions:
   * drop the assistant turn, then re-run the transcript that precedes it. Doing
   * it as "delete + insert" instead would leave an empty row if the model failed,
   * whereas this leaves the previous answer in the database until a new one lands.
   */
  const regenerate = useCallback(async () => {
    if (!activeId) return;
    try {
      const detail = await chatsApi.detail(activeId);
      const transcript = detail.messages ?? [];
      let lastUser = -1;
      for (let index = transcript.length - 1; index >= 0; index -= 1) {
        if (transcript[index]?.role === 'user') {
          lastUser = index;
          break;
        }
      }
      if (lastUser < 0) {
        toast.error(translate('chat.noInstance'));
        return;
      }
      // Re-send up to and including the last user turn; everything after it is the
      // answer being replaced.
      const prefix = transcript.slice(0, lastUser + 1);
      await streamTurn(activeId, prefix);
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  }, [activeId, streamTurn]);

  /**
   * Authorize (or revoke) the folder the read-only tools may read.
   *
   * Persisted on the conversation rather than held in a component: the authorization
   * is a property of "what this chat may touch", and losing it on a page switch
   * would silently disable the tools mid-conversation. The dialog is the Tauri
   * directory picker; in browser dev mode it falls back to a typed path, which is
   * what makes this testable without a packaged build.
   */
  const chooseToolRoot = useCallback(async () => {
    if (!activeId) {
      toast.error(translate('chat.toolNeedConversation'));
      return;
    }
    const path = await pickPath('dir', {
      current: toolRoot ?? undefined,
      prompt: translate('chat.toolRootTitle'),
    });
    if (!path) return;
    try {
      const updated = await chatsApi.update(activeId, { tool_root: path });
      setToolRoot(updated.tool_root ?? path);
      toast.success(translate('chat.toolRootSet'));
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  }, [activeId, toolRoot]);

  const clearToolRoot = useCallback(async () => {
    if (!activeId) return;
    try {
      await chatsApi.update(activeId, { tool_root: '' });
      setToolRoot(null);
    } catch (error) {
      toast.error(localizeBackendError(error instanceof Error ? error.message : error));
    }
  }, [activeId]);

  return (
    <div className="flex min-h-0 flex-1 overflow-hidden bg-[var(--chat-well)]">
      {/*
        The chat page's subject is the conversation, so a visible "Chat" heading would be
        chrome above a reading surface. It still needs a rank-1 heading for the document
        outline (WAI structure guidance; axe's `page-has-heading-one`), so it is here and
        visually hidden rather than omitted.
      */}
      <h1 className="sr-only">{translate('nav.chat')}</h1>
      <div className="flex min-w-0 flex-1 flex-col">
        <ChatThread
          messages={messages}
          live={live}
          streaming={streaming}
          loading={loadingConversation}
          instance={activeInstance}
          reasoningMode={reasoningMode}
          toolRoot={toolRoot}
          toolResults={toolResults}
          identity={identity}
          background={background}
          draft={draft}
          onDraftChange={setDraft}
          onSend={async (attachments) => {
            // `send` returns the outcome for tests and for the tool round trip; the
            // composer's contract is `void`, so the value is dropped here.
            await send(attachments);
          }}
          onStop={abort}
          composerRef={composerRef}
          onNewConversation={() => void createConversation()}
          onNavigate={(page) => navigate(page)}
          onDeleteMessage={async (message) => {
            if (!activeId) return;
            try {
              await chatsApi.deleteMessage(activeId, message.id);
              setMessages((current) => current.filter((item) => item.id !== message.id));
            } catch (error) {
              toast.error(localizeBackendError(error instanceof Error ? error.message : error));
            }
          }}
          onApproveTool={approveToolCall}
          onDenyTool={denyToolCall}
          onChooseToolRoot={chooseToolRoot}
          onClearToolRoot={clearToolRoot}
          onBranchMessage={async (message) => {
            if (!activeId) return;
            try {
              const branch = await chatsApi.branch(activeId, { message_id: message.id });
              setActiveId(branch.id);
              toast.success(translate('chat.branchCreated'));
            } catch (error) {
              toast.error(localizeBackendError(error instanceof Error ? error.message : error));
            }
          }}
          onEditMessage={editMessage}
          onRegenerateMessage={regenerate}
          onOpenPrompts={() => setPromptLibraryOpen(true)}
        />
      </div>

      {sidebarHost
        ? createPortal(
            <ChatSidebarList
              activeId={activeId}
              onSelect={(id) => {
                setActiveId(id);
              }}
              onNew={() => void createConversation()}
            />,
            sidebarHost,
          )
        : null}

      {inspectorOpen ? (
        <ChatInspector
          conversation={activeConversation}
          instances={instanceList}
          activeInstanceId={activeInstance?.id ?? null}
          toolRoot={toolRoot}
          onClose={() => setInspectorOpen(false)}
          onSaved={(next) => {
            setSettings(next);
            if (activeId) void loadConversation(activeId);
          }}
        />
      ) : null}

      <PromptLibraryDialog
        open={promptLibraryOpen}
        onClose={() => setPromptLibraryOpen(false)}
        draft={draft}
        onInsert={(text) => {
          // Insert at the caret rather than replacing: a saved template is usually
          // combined with typed context, and wiping the draft would be a surprise.
          composerRef.current?.insertText(text);
        }}
      />
    </div>
  );
}

export default ChatPage;
