import { useCallback, useRef, useState } from 'react';
import type { Citation, ChatToolCall } from '@/lib/api/types';
import { streamChatCompletion } from '@/lib/api';
import {
  accumulateToolCalls,
  buildChatStats,
  createSseParser,
  extractChunk,
  parseToolArguments,
  type NormalizedChunk,
} from '@/lib/ported/chatStream';
import { buildMessagePayloadContent } from '@/lib/ported/attachments';
import { chatToolDeclarations } from '@/lib/ported/chatTools';
import { thinkingTemplateArgs } from '@/features/chat/settings';
import { chatApi } from '@/lib/api';
import type { PendingAttachment } from '@/components/chat/Composer';

/**
 * Streaming orchestration for one conversation.
 *
 * This is the imperative seam between React state and a long-lived SSE response.
 * It is a hook rather than a store because a stream belongs to exactly one mounted
 * conversation: navigating away must abort it, and a store would outlive the page.
 *
 * Why the accumulated text is kept in a ref *and* mirrored into state: React must
 * re-render to show progress, but reading the buffer from state inside the read
 * loop would see a stale closure value on every chunk. The ref is the source of
 * truth during the stream; state is a rendering mirror, throttled to one update
 * per animation frame so a fast token stream does not schedule hundreds of renders
 * per second.
 */

export interface StreamStats {
  elapsedMs: number;
  promptTokens?: number;
  completionTokens?: number;
  totalTokens?: number;
  predictedPerSecond?: number;
}

export interface StreamOutcome {
  content: string;
  reasoning: string;
  toolCalls: ChatToolCall[];
  stats: Record<string, unknown>;
  citations: Citation[];
  knowledge: 'hit' | 'miss' | null;
  droppedTurns: number;
  stopped: boolean;
  error: string | null;
  /** True when reasoning consumed the whole completion budget and content is empty. */
  thinkingExhausted: boolean;
}

export interface StreamRequest {
  conversationId: string;
  instanceId: string;
  messages: Array<{
    role: string;
    content: string | unknown;
    reasoning?: string;
    tool_call_id?: string;
    stats?: Record<string, unknown>;
  }>;
  attachments: PendingAttachment[];
  params: Record<string, unknown>;
  systemPrompt?: string;
  knowledgeBaseId?: string | null;
  toolRoot?: string | null;
  declaredTools: string[];
  supportsVision: boolean;
  thinking: 'on' | 'off' | 'auto';
  locale: string;
  /** Offer write/command tools for this turn (conversation-level opt-in). */
  agentMode?: boolean;
  /** Offer `web_search`. Requires the backend setting as well. */
  webSearchEnabled?: boolean;
  webSearchConfigured?: boolean;
  /** Inject this conversation's saved memory into the system prompt. */
  memory?: boolean;
  /** Which memory scope to read (`personal` or `project:<id>`). */
  memoryScope?: string;
  /** Pinned skill id. The backend resolves it and narrows the tool set. */
  skillId?: string | null;
}

interface LiveStream {
  content: string;
  reasoning: string;
  toolCalls: ChatToolCall[];
  usage: Record<string, number> | null;
  timings: Record<string, number> | null;
  finishReason: string | null;
}

function decodeSources(header: string | null): Citation[] {
  if (!header) return [];
  try {
    // base64url → JSON. `atob` needs standard base64 and padding restored.
    const normalized = header.replace(/-/g, '+').replace(/_/g, '/');
    const padded = normalized.padEnd(Math.ceil(normalized.length / 4) * 4, '=');
    const binary = atob(padded);
    const bytes = Uint8Array.from(binary, (char) => char.charCodeAt(0));
    const json = new TextDecoder('utf-8').decode(bytes);
    const parsed: unknown = JSON.parse(json);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter((item): item is Record<string, unknown> => Boolean(item) && typeof item === 'object')
      .map((item, index) => ({
        index: typeof item.index === 'number' ? item.index : index + 1,
        title: String(item.title ?? item.document_name ?? ''),
        page: typeof item.page === 'number' ? item.page : null,
        heading: typeof item.heading === 'string' ? item.heading : null,
        snippet: String(item.snippet ?? item.text ?? ''),
        ocr: Boolean(item.ocr),
      }));
  } catch {
    // A malformed header must not break the turn: the answer is still valid.
    return [];
  }
}

export function useChatStream() {
  const [streaming, setStreaming] = useState(false);
  const [live, setLive] = useState<LiveStream>({
    content: '',
    reasoning: '',
    toolCalls: [],
    usage: null,
    timings: null,
    finishReason: null,
  });
  const [elapsed, setElapsed] = useState(0);
  const abortRef = useRef<AbortController | null>(null);
  const stopRequested = useRef(false);

  const stop = useCallback(() => {
    stopRequested.current = true;
    abortRef.current?.abort();
  }, []);

  const reset = useCallback(() => {
    setLive({ content: '', reasoning: '', toolCalls: [], usage: null, timings: null, finishReason: null });
    setElapsed(0);
  }, []);

  const run = useCallback(
    async (request: StreamRequest): Promise<StreamOutcome> => {
      reset();
      stopRequested.current = false;
      setStreaming(true);

      const controller = new AbortController();
      abortRef.current = controller;
      const startedAt = performance.now();

      // Build the request body. Only the last message carries attachments; earlier
      // turns keep their text so the model sees a consistent transcript.
      const messages = request.messages.map((message, index) => {
        const isLast = index === request.messages.length - 1;
        if (isLast && request.attachments.length && message.role === 'user') {
          return {
            ...message,
            content: buildMessagePayloadContent({
              content: message.content as string,
              attachments: request.attachments.map((attachment) => ({
                name: attachment.name,
                content: attachment.content,
                kind: attachment.kind,
              })),
            }),
          };
        }
        return message;
      });

      const declarations = chatToolDeclarations({
        declaredTools: request.declaredTools,
        toolRoot: request.toolRoot,
        agentMode: request.agentMode,
        webSearchEnabled: request.webSearchEnabled,
        webSearchConfigured: request.webSearchConfigured,
      });
      const body: Record<string, unknown> = {
        ...request.params,
        messages: request.systemPrompt
          ? [{ role: 'system', content: request.systemPrompt }, ...messages]
          : messages,
        stream: true,
        stream_options: { include_usage: true },
      };
      // The three-state thinking control has to become `chat_template_kwargs`;
      // llama-server does not accept a bare `thinking` field (the chat proxy's
      // allow-list drops it). `thinkingTemplateArgs` was written and unit-tested
      // in 2.0 but had **no caller**, so choosing "关闭思考" in the inspector
      // changed nothing on the wire — the model kept thinking.
      const templateArgs = thinkingTemplateArgs(request.thinking);
      if (Object.keys(templateArgs).length) {
        body.chat_template_kwargs = {
          ...(typeof request.params.chat_template_kwargs === 'object' && request.params.chat_template_kwargs
            ? (request.params.chat_template_kwargs as Record<string, unknown>)
            : {}),
          ...(templateArgs.chat_template_kwargs as Record<string, unknown>),
        };
      }
      if (declarations.length) {
        body.tools = declarations;
        body.tool_choice = 'auto';
      }
      if (request.knowledgeBaseId) {
        body.knowledge_base_id = request.knowledgeBaseId;
        const lastUser = [...messages].reverse().find((message) => message.role === 'user');
        if (lastUser && typeof lastUser.content === 'string') {
          body.knowledge_query = lastUser.content.slice(0, 2000);
        }
        // The backend reads `locale` (not `knowledge_locale`) to pick the
        // retrieval prompt's language.
        body.locale = request.locale;
      }
      if (request.memory) {
        // Opt-in per conversation. The backend merges the notes into the single
        // leading system message, so no second system turn is created here.
        body.memory = true;
        if (request.memoryScope) body.memory_scope = request.memoryScope;
      }
      if (request.skillId) {
        // The backend resolves the skill, injects its instructions and narrows the
        // tool set. Passing the id (not the instructions) keeps the text in one
        // place: a client that assembled the prompt itself could disagree with the
        // executor about which tools the skill allows.
        body.skill_id = request.skillId;
      }

      let content = '';
      let reasoning = '';
      const toolCalls: ChatToolCall[] = [];
      let usage: Record<string, number> | null = null;
      let timings: Record<string, number> | null = null;
      let finishReason: string | null = null;
      let errorMessage: string | null = null;
      let citations: Citation[] = [];
      let knowledge: 'hit' | 'miss' | null = null;
      let droppedTurns = 0;

      // One render per frame instead of one per token.
      let frame: number | null = null;
      const scheduleRender = () => {
        if (frame !== null) return;
        frame = requestAnimationFrame(() => {
          frame = null;
          setLive({
            content,
            reasoning,
            toolCalls: [...toolCalls],
            usage,
            timings,
            finishReason,
          });
          setElapsed(performance.now() - startedAt);
        });
      };

      try {
        const response = await streamChatCompletion(request.instanceId, body, {
          signal: controller.signal,
        });

        citations = decodeSources(response.headers.get('x-lunarshroud-sources'));
        const knowledgeHeader = response.headers.get('x-lunarshroud-knowledge');
        knowledge = knowledgeHeader === 'hit' || knowledgeHeader === 'miss' ? knowledgeHeader : null;
        const dropped = Number(response.headers.get('x-lunarshroud-dropped-turns') ?? 0);
        droppedTurns = Number.isFinite(dropped) ? dropped : 0;

        const reader = response.body?.getReader();
        if (!reader) throw new Error('response has no body');
        const decoder = new TextDecoder();
        const parser = createSseParser();

        for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          const text = decoder.decode(value, { stream: true });
          for (const event of parser.push(text)) {
            if ('done' in event) continue;
            if ('raw' in event) continue;
            const chunk: NormalizedChunk | null = extractChunk(event.data);
            if (!chunk) continue;
            if (chunk.error) errorMessage = chunk.error;
            if (chunk.content) content += chunk.content;
            if (chunk.reasoning) reasoning += chunk.reasoning;
            if (chunk.toolCalls.length) accumulateToolCalls(toolCalls, chunk.toolCalls);
            if (chunk.usage) usage = chunk.usage;
            if (chunk.timings) timings = chunk.timings;
            if (chunk.finishReason) finishReason = chunk.finishReason;
            scheduleRender();
          }
        }
        // Anything left in the buffer that lacked a trailing blank line.
        for (const event of parser.flush()) {
          if ('data' in event) {
            const chunk = extractChunk(event.data);
            if (chunk?.content) content += chunk.content;
            if (chunk?.reasoning) reasoning += chunk.reasoning;
            if (chunk?.usage) usage = chunk.usage;
          }
        }
      } catch (error) {
        if (!stopRequested.current) {
          errorMessage =
            error instanceof Error ? error.message : 'The stream ended unexpectedly';
        }
      } finally {
        if (frame !== null) cancelAnimationFrame(frame);
        abortRef.current = null;
        setStreaming(false);
      }

      const elapsedMs = performance.now() - startedAt;
      const stats = buildChatStats({
        usage,
        timings,
        elapsedMs,
        content,
        reasoningChars: reasoning.length,
      });

      // Reasoning consumed the entire budget: the answer is empty and
      // finish_reason says "length". Surface it rather than showing a blank reply.
      const thinkingExhausted =
        Boolean(reasoning) && !content.trim() && finishReason === 'length';

      return {
        content,
        reasoning,
        toolCalls,
        stats: { ...stats, ...(toolCalls.length ? { tool_calls: toolCalls } : {}), ...(citations.length ? { sources: citations } : {}) },
        citations,
        knowledge,
        droppedTurns,
        stopped: stopRequested.current,
        error: errorMessage,
        thinkingExhausted,
      };
    },
    [reset],
  );

  const executeTool = useCallback(
    async (call: ChatToolCall, root: string) => {
      // Client-side pre-flight. It is *not* the security boundary (the backend
      // resolves paths again), but it turns an obvious escape into an immediate
      // readable sentence instead of a round trip that ends in a 400.
      const args = parseToolArguments(call.function.arguments) ?? {};
      const name = call.function.name;
      const needsRoot = ['read_file', 'file_glob_search', 'grep_search', 'write_file', 'edit_file', 'run_command'].includes(name);
      if (needsRoot && !root.trim()) {
        return { ok: false as const, content: 'no authorized folder is set for this conversation' };
      }
      try {
        const result = await chatApi.executeTool({
          name,
          arguments: args,
          root,
        });
        return { ok: result.ok, content: String(result.result ?? result.error ?? '') };
      } catch (error) {
        return {
          ok: false as const,
          content: error instanceof Error ? error.message : 'tool execution failed',
        };
      }
    },
    [],
  );

  return { streaming, live, elapsed, run, stop, reset, executeTool };
}

export { chatToolDeclarations };
