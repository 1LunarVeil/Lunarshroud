import type { Conversation } from '@/lib/api/types';

/**
 * Per-conversation generation settings, resolved from the stored conversation.
 *
 * The backend persists only a small allow-list of chat parameters
 * (`chat_store.PARAM_KEYS`), so the UI resolves a complete settings object from
 * that partial record rather than scattering `??` fallbacks through components.
 *
 * `thinking` is a three-state control with a subtlety worth spelling out: it is a
 * **per-request** template switch (`chat_template_kwargs.enable_thinking`), not the
 * process-level `--reasoning` flag. `auto` sends nothing at all, letting the
 * model's own template decide.
 */
export type ThinkingMode = 'on' | 'off' | 'auto';

export interface ResolvedChatSettings {
  instanceId: string | null;
  model: string | null;
  knowledgeBaseId: string | null;
  systemPrompt: string;
  thinking: ThinkingMode;
  params: Record<string, unknown>;
}

/** Defaults that match what a fresh conversation should send. */
export const CHAT_PARAM_DEFAULTS: Record<string, unknown> = {
  temperature: 0.8,
  top_p: 0.95,
  max_tokens: 2048,
};

export function resolveChatSettings(
  conversation: Pick<Conversation, 'instance_id' | 'model' | 'knowledge_base_id' | 'params'> | null,
): ResolvedChatSettings {
  const params = conversation?.params ?? {};
  const thinkingValue = params.thinking;
  const thinking: ThinkingMode =
    thinkingValue === 'on' || thinkingValue === 'off' ? thinkingValue : 'auto';

  // `system` is the key `chat_store.PARAM_KEYS` accepts. A previous version read
  // and wrote `system_prompt`, which the allow-list silently dropped — the panel
  // said "saved", the prompt never reached the model, and nothing failed. Accept
  // the legacy spelling on read so a conversation saved by that build still opens
  // with its prompt visible.
  const systemPrompt =
    typeof params.system === 'string'
      ? params.system
      : typeof (params as Record<string, unknown>).system_prompt === 'string'
        ? String((params as Record<string, unknown>).system_prompt)
        : '';

  // Drop the UI-only keys before sending them upstream: the backend rejects
  // unknown fields by dropping them silently, which would hide a typo. The
  // system prompt travels as `messages[0]` (see `useChatStream`), never as a
  // sampling parameter.
  const {
    thinking: _thinking,
    system: _system,
    system_prompt: _legacySystem,
    ...rest
  } = params as Record<string, unknown>;

  return {
    instanceId: conversation?.instance_id ?? null,
    model: conversation?.model ?? null,
    knowledgeBaseId: conversation?.knowledge_base_id ?? null,
    systemPrompt,
    thinking,
    params: { ...CHAT_PARAM_DEFAULTS, ...rest },
  };
}

/** True when this conversation opted into write/command tools. */
export function agentModeEnabled(params: Record<string, unknown> | null | undefined): boolean {
  return params?.agent_mode === true || params?.agent_mode === 'on';
}

/** Turn the three-state control into the `chat_template_kwargs` fragment. */
export function thinkingTemplateArgs(thinking: ThinkingMode): Record<string, unknown> {
  if (thinking === 'auto') return {};
  return { chat_template_kwargs: { enable_thinking: thinking === 'on' } };
}
